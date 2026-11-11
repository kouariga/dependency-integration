#!/usr/bin/env python3
"""Scan the images Cloud Run serves and runs, without changing services or executing jobs."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from maintenance import Language

SCANNER = "aquasec/trivy:0.76.1@sha256:790fd66d1697395d8693d1acaa1ea702406a20f109b6fc7bac8db623eca6c06c"
GROUP_LEVELS = {"fix-available": "attention", "no-fix": "warning"}
SCAN_ERRORS = (subprocess.CalledProcessError, KeyError, ValueError, OSError, TypeError)

MESSAGES = {
    Language.EN: {
        "fix-available": "fixed version available",
        "no-fix": "no fixed version yet",
        "vulnerable": "Deployed {resource} has HIGH/CRITICAL vulnerabilities ({description}): {ids}",
        "unscanned": "Only the deployed digests were listed. No vulnerability scan ran.",
        "unavailable": "The deployed image check did not complete: {error}",
    },
    Language.JA: {
        "fix-available": "修正版あり",
        "no-fix": "修正版の情報なし",
        "vulnerable": "本番配備イメージのHigh/Critical指摘（{description}）{resource} {ids}",
        "unscanned": "本番digest一覧のみ確認しました。脆弱性スキャンは未実施です。",
        "unavailable": "本番イメージの確認が未完了です。{error}",
    },
}


def gcloud(project, region, *args):
    return json.loads(subprocess.check_output(
        ["gcloud", "run", *args, "--project", project, "--region", region, "--format=json"], text=True))


def serving_revisions(service):
    revisions = set()
    for target in service.get("status", {}).get("traffic", []):
        if target.get("percent", 0) > 0 or target.get("tag"):
            name = target.get("revisionName")
            if not name:
                raise ValueError("Serving traffic has no resolved revision")
            revisions.add(name)
    if not revisions:
        raise ValueError("Service has no observed serving revision")
    return sorted(revisions)


def immutable(image):
    if not isinstance(image, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9./:_-]*@sha256:[0-9a-f]{64}", image):
        raise ValueError("Expected immutable deployed image digest")
    return image


def inventory(cloud, project, region):
    resources = []
    services = cloud(project, region, "services", "list")
    jobs = cloud(project, region, "jobs", "list")
    if not services:
        raise ValueError("Expected at least one Cloud Run service")
    for service in services:
        service_name = service["metadata"]["name"]
        for revision_name in serving_revisions(service):
            revision = cloud(project, region, "revisions", "describe", revision_name)
            containers = revision["spec"]["containers"]
            if len(containers) != 1:
                raise ValueError("Multiple service containers require explicit digest inventory")
            resources.append({"kind": "serving-revision", "name": service_name + "/" + revision_name,
                              "image": immutable(revision["status"]["imageDigest"])})
    for job in jobs:
        name = job["metadata"]["name"]
        body = cloud(project, region, "jobs", "describe", name)
        containers = body["spec"]["template"]["spec"]["template"]["spec"]["containers"]
        if not containers:
            raise ValueError("Job has no configured containers")
        for index, container in enumerate(containers):
            resources.append({"kind": "configured-job", "name": name + "/" + str(index),
                              "image": immutable(container["image"])})
    return sorted(resources, key=lambda resource: (resource["kind"], resource["name"]))


def vulnerability_groups(scan):
    os = scan["Metadata"]["OS"]
    if (scan.get("SchemaVersion") != 2 or scan.get("ArtifactType") != "container_image"
            or not os["Family"] or not os["Name"] or os.get("EOSL", False) is not False):
        raise ValueError("Scanner did not identify a supported container OS")
    results = scan["Results"]
    if not isinstance(results, list) or any(not isinstance(result, dict) for result in results):
        raise ValueError("Scanner returned invalid inventory results")
    if not any(
        result.get("Class") == "os-pkgs" and result.get("Type") == os["Family"]
        and isinstance(result.get("Packages"), list) and result["Packages"]
        for result in results
    ):
        raise ValueError("Scanner returned no OS package inventory")

    groups = {group: set() for group in GROUP_LEVELS}
    for result in results:
        vulnerabilities = result.get("Vulnerabilities", [])
        if vulnerabilities is None:
            continue
        if not isinstance(vulnerabilities, list):
            raise ValueError("Scanner returned invalid vulnerabilities")
        for item in vulnerabilities:
            if item["Severity"] in ("HIGH", "CRITICAL"):
                group = "fix-available" if item.get("FixedVersion") else "no-fix"
                groups[group].add(item["VulnerabilityID"])
    return {group: sorted(ids) for group, ids in groups.items()}


def scan_image(image, destination, run=subprocess.run):
    run(["docker", "pull", image], check=True)
    with TemporaryDirectory() as directory:
        archive = Path(directory) / "image.tar"
        run(["docker", "image", "save", "--output", str(archive), image], check=True)
        common = ["docker", "run", "--rm", "--mount", f"type=bind,source={archive},target=/image.tar,readonly",
                  SCANNER, "image", "--input", "/image.tar"]
        with (destination / "scan.json").open("w") as report:
            run([*common, "--scanners", "vuln", "--list-all-pkgs", "--format", "json"], stdout=report, check=True)
        groups = vulnerability_groups(json.loads((destination / "scan.json").read_text()))
        with (destination / "sbom.cdx.json").open("w") as report:
            run([*common, "--format", "cyclonedx"], stdout=report, check=True)
    return groups


def resource_name(resource):
    return resource["name"].split("/", 1)[0]


def resource_findings(resources, groups_by_image, run_url, language: Language):
    """One finding per service or job and group, so a redeploy with a new digest keeps the same issue."""
    merged = {}
    for resource in resources:
        ids = merged.setdefault(resource_name(resource), {group: set() for group in GROUP_LEVELS})
        for group, found in groups_by_image[resource["image"]].items():
            ids[group].update(found)
    text = MESSAGES[language]
    findings = []
    for name, groups in sorted(merged.items()):
        for group, level in GROUP_LEVELS.items():
            if groups[group]:
                summary = text["vulnerable"].format(description=text[group], resource=name,
                                                    ids=", ".join(sorted(groups[group])))
                findings.append({"key": f"deployed:{group}:{name}", "level": level, "summary": summary, "url": run_url})
    return findings


def annotation(item):
    message = item["summary"].replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    return "::warning::" + message


def scan_project(cloud, scan, project, region, output: Path, run_url, language: Language, inventory_only=False):
    text = MESSAGES[language]
    report = {"checked_at": datetime.now(timezone.utc).isoformat(), "project": project,
              "scope": "serving revisions and configured job templates", "resources": [], "findings": []}
    groups_by_image = {}
    try:
        report["resources"] = inventory(cloud, project, region)
        (output / "inventory.json").write_text(json.dumps(report["resources"], indent=2) + "\n")
        if inventory_only:
            report["findings"].append({"key": "deployed:unscanned", "level": "attention",
                                       "summary": text["unscanned"], "url": run_url})
            return report
        for image in sorted({resource["image"] for resource in report["resources"]}):
            destination = output / hashlib.sha256(image.encode()).hexdigest()[:16]
            destination.mkdir(exist_ok=True)
            (destination / "image.json").write_text(json.dumps({"image": image, "scanner": SCANNER}) + "\n")
            groups_by_image[image] = scan(image, destination)
        if inventory(cloud, project, region) != report["resources"]:
            raise ValueError("Deployed image inventory changed during the scan")
    except SCAN_ERRORS as error:
        report["findings"].append({"key": "deployed:unavailable", "level": "error",
                                   "summary": text["unavailable"].format(error=type(error).__name__), "url": run_url})
    scanned = [resource for resource in report["resources"] if resource["image"] in groups_by_image]
    report["findings"] = resource_findings(scanned, groups_by_image, run_url, language) + report["findings"]
    return report


def main(argv=None, cloud=gcloud, scan=scan_image):
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--inventory-only", action="store_true")
    parser.add_argument("--run-url", default="")
    parser.add_argument("--language", choices=[language.value for language in Language], default=Language.EN.value)
    args = parser.parse_args(argv)
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    report = scan_project(cloud, scan, args.project, args.region, output, args.run_url, Language(args.language),
                          args.inventory_only)
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    for item in report["findings"]:
        if item["key"].startswith(tuple(f"deployed:{group}:" for group in GROUP_LEVELS)):
            print(annotation(item))
    return int(any(item["level"] == "error" for item in report["findings"]))


if __name__ == "__main__":
    raise SystemExit(main())

import contextlib
import io
import json
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import support  # noqa: F401  (puts the repository root on sys.path)

import cloud_run_scan as security
from maintenance import Language

API_IMAGE = "registry.example/app@sha256:" + "a" * 64
JOB_IMAGE = "registry.example/job@sha256:" + "b" * 64
PREVIEW_IMAGE = "registry.example/app@sha256:" + "c" * 64


def scan_report():
    return {"SchemaVersion": 2, "ArtifactType": "container_image", "Metadata": {
        "OS": {"Family": "debian", "Name": "13.2"}},
        "Results": [{"Target": "/image.tar (debian 13.2)", "Class": "os-pkgs", "Type": "debian",
                     "Packages": [{"Name": "openssl", "Version": "3.5.4"}]},
                    {"Target": "package-lock.json", "Class": "lang-pkgs", "Type": "npm"}]}


class FakeCloud:
    """Answers gcloud run reads for one service with two serving revisions and one job."""

    def __init__(self):
        self.revisions = {"live": API_IMAGE, "preview": PREVIEW_IMAGE}

    def __call__(self, project, region, *args):
        if args == ("services", "list"):
            return [{"metadata": {"name": "api"}, "status": {"traffic": [
                {"revisionName": "live", "percent": 100}, {"revisionName": "preview", "tag": "preview"}]}}]
        if args == ("jobs", "list"):
            return [{"metadata": {"name": "batch"}}]
        if args[:2] == ("revisions", "describe"):
            return {"spec": {"containers": [{}]}, "status": {"imageDigest": self.revisions[args[2]]}}
        if args == ("jobs", "describe", "batch"):
            return {"spec": {"template": {"spec": {"template": {"spec": {"containers": [{"image": JOB_IMAGE}]}}}}}}
        raise AssertionError(args)


GROUPS = {
    API_IMAGE: {"fix-available": ["CVE-2026-1002"], "no-fix": ["CVE-2026-1001"]},
    PREVIEW_IMAGE: {"fix-available": ["CVE-2026-1004"], "no-fix": []},
    JOB_IMAGE: {"fix-available": [], "no-fix": ["CVE-2026-1003"]},
}


def fake_scan(image, destination):
    return GROUPS[image]


class DeployedSecurityTests(unittest.TestCase):
    def test_inventory_includes_split_traffic_and_tagged_revisions(self):
        service = {"status": {"traffic": [{"revisionName": "old", "percent": 10},
                    {"revisionName": "new", "percent": 90}, {"revisionName": "preview", "tag": "preview"},
                    {"revisionName": "inactive", "percent": 0}]}}
        self.assertEqual(security.serving_revisions(service), ["new", "old", "preview"])

    def test_missing_traffic_is_not_treated_as_healthy(self):
        for traffic in ([], [{"percent": 100}]):
            with self.assertRaises(ValueError):
                security.serving_revisions({"status": {"traffic": traffic}})

    def test_refuses_mutable_tags(self):
        with self.assertRaises(ValueError):
            security.immutable("registry.example/app:latest")
        self.assertEqual(security.immutable(API_IMAGE), API_IMAGE)

    def test_uses_actual_serving_revision_digest_and_job_configuration(self):
        responses = iter([
            [{"metadata": {"name": "api"}, "status": {"latestReadyRevisionName": "unused", "traffic": [{"revisionName": "live", "percent": 100}]}}],
            [{"metadata": {"name": "batch"}}],
            {"spec": {"containers": [{"image": "registry.example/app:tag"}]}, "status": {"imageDigest": API_IMAGE}},
            {"spec": {"template": {"spec": {"template": {"spec": {"containers": [{"image": JOB_IMAGE}]}}}}}},
        ])
        actual = security.inventory(lambda *args: next(responses), "project", "region")
        self.assertEqual(actual, [{"kind": "configured-job", "name": "batch/0", "image": JOB_IMAGE},
                                  {"kind": "serving-revision", "name": "api/live", "image": API_IMAGE}])

    def test_partial_or_unsupported_os_inventory_fails(self):
        for incomplete in ("unknown-os", "eol-os", "language-only", "empty-os-inventory"):
            report = scan_report()
            if incomplete == "unknown-os":
                report["Metadata"]["OS"] = {"Family": "", "Name": ""}
            elif incomplete == "eol-os":
                report["Metadata"]["OS"]["EOSL"] = True
            elif incomplete == "language-only":
                report["Results"].pop(0)
            else:
                report["Results"][0]["Packages"] = []
            with self.subTest(incomplete=incomplete), self.assertRaises(ValueError):
                security.vulnerability_groups(report)

    def test_scan_reads_a_readonly_archive_and_groups_high_critical_findings(self):
        report = scan_report()
        report["Results"][0]["Vulnerabilities"] = [
            {"VulnerabilityID": "CVE-2026-1002", "Severity": "HIGH", "FixedVersion": "3.5.5"},
            {"VulnerabilityID": "CVE-2026-1001", "Severity": "CRITICAL"},
            {"VulnerabilityID": "CVE-2026-1000", "Severity": "MEDIUM", "FixedVersion": "3.5.5"},
        ]
        report["Results"][1]["Vulnerabilities"] = [
            {"VulnerabilityID": "CVE-2026-1003", "Severity": "HIGH", "FixedVersion": ""},
        ]
        archives = []
        formats = []

        def docker(command, *, check, stdout=None):
            if command[:2] == ["docker", "pull"]:
                self.assertEqual(command[-1], API_IMAGE)
            elif command[:3] == ["docker", "image", "save"]:
                self.assertEqual(command[-1], API_IMAGE)
                archive = Path(command[command.index("--output") + 1])
                archives.append(archive)
                archive.write_bytes(b"image archive")
            else:
                self.assertNotIn("docker.sock", " ".join(command))
                self.assertIn(security.SCANNER, command)
                self.assertRegex(security.SCANNER, r"@sha256:[0-9a-f]{64}$")
                self.assertEqual(command[command.index("--mount") + 1],
                                 f"type=bind,source={archives[0]},target=/image.tar,readonly")
                self.assertEqual(command[command.index("--input") + 1], "/image.tar")
                self.assertEqual(archives[0].read_bytes(), b"image archive")
                output_format = command[command.index("--format") + 1]
                formats.append(output_format)
                if output_format == "json":
                    self.assertIn("--list-all-pkgs", command)
                json.dump(report if output_format == "json" else {"bomFormat": "CycloneDX"}, stdout)

        with TemporaryDirectory() as directory:
            self.assertEqual(security.scan_image(API_IMAGE, Path(directory), run=docker),
                             {"fix-available": ["CVE-2026-1002"], "no-fix": ["CVE-2026-1001", "CVE-2026-1003"]})
            self.assertEqual(formats, ["json", "cyclonedx"])
            self.assertFalse(archives[0].exists())
            self.assertEqual(json.loads((Path(directory) / "sbom.cdx.json").read_text())["bomFormat"], "CycloneDX")


class ReportTests(unittest.TestCase):
    def run_main(self, *extra, cloud=None, scan=fake_scan):
        with TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()) as stdout:
            argv = ["--project", "example-project", "--region", "example-region", "--output", directory,
                    "--run-url", "run", *extra]
            code = security.main(argv, cloud=cloud or FakeCloud(), scan=scan)
            report = json.loads((Path(directory) / "report.json").read_text())
        return code, report, stdout.getvalue()

    def test_findings_are_keyed_by_resource_with_policy_levels(self):
        code, report, _ = self.run_main()
        self.assertEqual([(item["key"], item["level"]) for item in report["findings"]], [
            ("deployed:no-fix:job/batch", "warning"),
            ("deployed:fix-available:service/api", "attention"),
            ("deployed:no-fix:service/api", "warning"),
        ])
        self.assertEqual(report["findings"][1]["summary"],
                         "Deployed service/api has HIGH/CRITICAL vulnerabilities (fixed version available): "
                         "CVE-2026-1002, CVE-2026-1004")
        self.assertEqual({item["url"] for item in report["findings"]}, {"run"})
        self.assertEqual(code, 0)

    def test_vulnerabilities_print_one_warning_per_resource_and_group(self):
        _, report, stdout = self.run_main()
        self.assertEqual(stdout.splitlines(), ["::warning::" + item["summary"] for item in report["findings"]])

    def test_language_selects_summary_wording(self):
        _, report, _ = self.run_main("--language", "ja")
        self.assertEqual(report["findings"][0]["summary"], "本番配備イメージのHigh/Critical指摘（修正版の情報なし）job/batch CVE-2026-1003")

    def test_scanner_failure_is_an_error(self):
        def broken(image, destination):
            raise subprocess.CalledProcessError(1, "docker")

        code, report, stdout = self.run_main(scan=broken)
        self.assertEqual(code, 1)
        self.assertEqual([(item["key"], item["level"]) for item in report["findings"]],
                         [("deployed:unavailable", "error")])
        self.assertIn("CalledProcessError", report["findings"][0]["summary"])
        self.assertEqual(stdout, "")

    def test_inventory_change_during_scan_is_an_error_but_keeps_findings(self):
        cloud = FakeCloud()

        def scan(image, destination):
            cloud.revisions["live"] = PREVIEW_IMAGE
            return GROUPS[image]

        code, report, _ = self.run_main(cloud=cloud, scan=scan)
        self.assertEqual(code, 1)
        self.assertEqual(report["findings"][-1]["key"], "deployed:unavailable")
        self.assertIn("deployed:fix-available:service/api", [item["key"] for item in report["findings"]])

    def test_inventory_only_is_reported_as_unscanned(self):
        code, report, _ = self.run_main("--inventory-only")
        self.assertEqual(code, 0)
        self.assertEqual([item["key"] for item in report["findings"]], ["deployed:unscanned"])

    def test_raw_scans_are_kept_per_image(self):
        with TemporaryDirectory() as directory:
            security.main(["--project", "example-project", "--region", "example-region", "--output", directory],
                          cloud=FakeCloud(), scan=fake_scan)
            images = sorted(json.loads(p.read_text())["image"] for p in Path(directory).glob("*/image.json"))
            self.assertEqual(images, sorted(GROUPS))
            self.assertTrue((Path(directory) / "inventory.json").exists())


def refuse_cloud(*args):
    raise AssertionError("discovery must not run with an inventory file")


class InventoryFileTests(unittest.TestCase):
    def run_file(self, entries, *extra, scan=fake_scan):
        with TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()):
            path = Path(directory) / "listed.json"
            path.write_text(entries if isinstance(entries, str) else json.dumps(entries))
            argv = ["--region", "example-region", "--output", directory, "--run-url", "run",
                    "--inventory-file", str(path), *extra]
            code = security.main(argv, cloud=refuse_cloud, scan=scan)
            report = json.loads((Path(directory) / "report.json").read_text())
        return code, report

    def test_listed_images_are_scanned_under_logical_names_without_project(self):
        listed = [{"kind": "service", "name": "gateway", "image": API_IMAGE},
                  {"kind": "job", "name": "drive-sync", "image": JOB_IMAGE}]
        scanned = []

        def scan(image, destination):
            scanned.append(image)
            return GROUPS[image]

        code, report = self.run_file(listed, scan=scan)
        self.assertEqual(code, 0)
        self.assertEqual(sorted(scanned), sorted([API_IMAGE, JOB_IMAGE]))
        self.assertEqual([item["key"] for item in report["findings"]], [
            "deployed:no-fix:job/drive-sync",
            "deployed:fix-available:service/gateway",
            "deployed:no-fix:service/gateway",
        ])

    def test_keys_do_not_change_when_a_release_changes_the_image(self):
        _, first = self.run_file([{"kind": "job", "name": "drive-sync", "image": API_IMAGE}])
        _, second = self.run_file([{"kind": "job", "name": "drive-sync", "image": PREVIEW_IMAGE}])
        self.assertEqual([item["key"] for item in first["findings"]],
                         ["deployed:fix-available:job/drive-sync", "deployed:no-fix:job/drive-sync"])
        self.assertEqual([item["key"] for item in second["findings"]], ["deployed:fix-available:job/drive-sync"])

    def test_invalid_inventory_files_are_an_error_finding(self):
        valid = {"kind": "job", "name": "drive-sync", "image": JOB_IMAGE}
        cases = {
            "empty list": [],
            "not a list": {"jobs": [valid]},
            "unknown kind": [{**valid, "kind": "revision"}],
            "tag instead of digest": [{**valid, "image": "registry.example/job:latest"}],
            "duplicate resource": [valid, {**valid, "image": API_IMAGE}],
            "missing field": [{"kind": "job", "name": "drive-sync"}],
            "extra field": [{**valid, "revision": "r1"}],
            "name with a slash": [{**valid, "name": "drive-sync/0"}],
            "not json": "[",
        }
        for case, entries in cases.items():
            with self.subTest(case=case):
                code, report = self.run_file(entries)
                self.assertEqual(code, 1)
                self.assertEqual([(item["key"], item["level"]) for item in report["findings"]],
                                 [("deployed:unavailable", "error")])

    def test_missing_inventory_file_is_an_error_finding(self):
        with TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()):
            code = security.main(["--region", "r", "--output", directory, "--inventory-file",
                                  str(Path(directory) / "absent.json")], cloud=refuse_cloud, scan=fake_scan)
            report = json.loads((Path(directory) / "report.json").read_text())
        self.assertEqual(code, 1)
        self.assertEqual([item["key"] for item in report["findings"]], ["deployed:unavailable"])

    def test_discovery_still_requires_a_project(self):
        with TemporaryDirectory() as directory, contextlib.redirect_stderr(io.StringIO()), \
                self.assertRaises(SystemExit):
            security.main(["--region", "r", "--output", directory], cloud=FakeCloud(), scan=fake_scan)


if __name__ == "__main__":
    unittest.main()

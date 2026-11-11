#!/usr/bin/env python3
"""Collect dependency maintenance evidence and keep one GitHub issue open per actionable finding."""

from __future__ import annotations

import argparse
import dataclasses
import enum
import fnmatch
import hashlib
import json
import os
import re
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

CLOSE_ONLY_WHEN_OBSERVED = ("workflow:", "schedule-outcome:", "updater:", "job:")
READ_ERRORS = (subprocess.CalledProcessError, KeyError, ValueError)


class Language(enum.Enum):
    EN = "en"
    JA = "ja"


MESSAGES = {
    Language.EN: {
        "title_prefix": "[Maintenance] ",
        "workflow_failed": "The latest completed run of {name} failed.",
        "schedule_missing": "No scheduled run of {name} is confirmed to have completed in the last 36 hours.",
        "schedule_failed": "The latest scheduled run of {name} failed: {conclusion}",
        "updater_failed": "A Dependabot update job is failing: {name}",
        "pull_stale": "A Dependabot PR has been open for 7 days or more: {title}",
        "read_history": "Cannot read the run history of {name}.",
        "read_dependencies": "Cannot read the Dependabot update runs or the open PRs.",
        "checked_at": "Checked at {checked_at}",
        "checked_by": "Checked by {run_url}",
        "evidence": " [evidence]({url})",
        "nothing": "Nothing needs attention within the checked scope. This does not vouch for anything outside it.",
        "footer": "Nothing is deployed or waived automatically. A finding closes automatically once a complete "
                  "check confirms it is resolved.",
        "resolved": "A complete check confirmed that this finding is resolved.\n\nChecked at {checked_at}\n"
                    "Evidence {run_url}\n\n{marker}",
        "extra_missing": "The {name} report is missing.",
        "extra_invalid": "The {name} report was not retrieved or is invalid.",
        "job_result": "{name} finished this run as {conclusion}.",
        "unknown": "unknown",
        "local_run": "local read-only check",
    },
    Language.JA: {
        "title_prefix": "[保守] ",
        "workflow_failed": "{name} の直近完了実行が失敗しています。",
        "schedule_missing": "{name} の定期実行が過去36時間に完了したことを確認できません。",
        "schedule_failed": "{name} の直近の定期実行が失敗しています。{conclusion}",
        "updater_failed": "Dependabot の更新処理が失敗しています。{name}",
        "pull_stale": "Dependabot PR が7日以上未統合です。{title}",
        "read_history": "{name} の実行履歴を取得できません。",
        "read_dependencies": "依存更新の実行履歴またはPR一覧を取得できません。",
        "checked_at": "確認時刻 {checked_at}",
        "checked_by": "確認実行 {run_url}",
        "evidence": " [根拠]({url})",
        "nothing": "今回の確認範囲に要対応事項はありません。本番の機能受入や未検査対象の安全性は保証しません。",
        "footer": "本番配備や例外延長は自動では行いません。検査が正常に完了し解消を確認した指摘は自動で閉じます。",
        "resolved": "正常に完了した検査で、この指摘の解消を確認しました。\n\n確認時刻 {checked_at}\n根拠 {run_url}\n\n{marker}",
        "extra_missing": "{name} の確認結果がありません。",
        "extra_invalid": "{name} の確認結果が未取得または不正です。",
        "job_result": "{name} の今回の実行結果は {conclusion} です。",
        "unknown": "不明",
        "local_run": "ローカルでの読み取り確認",
    },
}

MARKER_PREFIX = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")


@dataclasses.dataclass(frozen=True)
class Settings:
    """Per-repository wording and the marker prefix that ties an open issue to its finding key."""

    marker_prefix: str = "maintenance"
    title_prefix: str | None = None
    language: Language = Language.EN

    def __post_init__(self):
        if not MARKER_PREFIX.fullmatch(self.marker_prefix):
            raise ValueError(f"Invalid marker prefix: {self.marker_prefix!r}")

    def text(self, message: str, /, **values: str) -> str:
        return MESSAGES[self.language][message].format(**values)

    def title(self, summary: str) -> str:
        prefix = self.text("title_prefix") if self.title_prefix is None else self.title_prefix
        return (prefix + summary)[:250]

    @property
    def start(self) -> str:
        return f"<!-- {self.marker_prefix}:start -->"

    @property
    def end(self) -> str:
        return f"<!-- {self.marker_prefix}:end -->"

    def fingerprint_marker(self, digest: str) -> str:
        return f"<!-- {self.marker_prefix}-fingerprint:{digest} -->"

    def incident_marker(self, key: str) -> str:
        return f"<!-- {self.marker_prefix}-incident:{json.dumps(key, ensure_ascii=False)} -->"

    def resolved_marker(self, episode: str) -> str:
        return f"<!-- {self.marker_prefix}-resolved:{episode} -->"

    @property
    def fingerprint_pattern(self) -> re.Pattern:
        return re.compile(rf"<!-- {re.escape(self.marker_prefix)}-fingerprint:([0-9a-f]{{64}}) -->")

    @property
    def incident_pattern(self) -> re.Pattern:
        return re.compile(rf'^<!-- {re.escape(self.marker_prefix)}-incident:("(?:[^"\\\r\n]|\\.)*") -->\r?$',
                          re.MULTILINE)


Api = Callable[..., object]


def gh_api(path, method="GET", data=None, paginate=False):
    command = ["gh", "api", "--method", method, path]
    if paginate:
        command += ["--paginate", "--slurp"]
    if data is not None:
        command += ["--input", "-"]
    result = subprocess.run(command, input=json.dumps(data) if data is not None else None,
                            text=True, capture_output=True, check=True)
    return json.loads(result.stdout) if result.stdout.strip() else None


def gh_download(repo: str, run_id: str, name: str, directory: Path) -> None:
    subprocess.run(["gh", "run", "download", run_id, "--repo", repo, "--name", name, "--dir", str(directory)],
                   check=True)


def timestamp(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def finding(key, summary, url="", level="attention"):
    return {"key": key, "level": level, "summary": summary, "url": url}


def has_result(run):
    return run["status"] == "completed" and run["conclusion"] not in (None, "skipped", "cancelled")


def is_dependabot_update(run):
    return (run.get("event") == "dynamic" and run.get("path") == "dynamic/dependabot/dependabot-updates"
            and (run.get("actor") or {}).get("login") == "dependabot[bot]")


def updater_name(run):
    return re.sub(r" - Update #\d+$", "", run["name"])


def completed_results(api: Api, base, runs):
    results = []
    for run in runs:
        attempt_number = run["run_attempt"]
        run_id = run["id"]
        # The run list exposes only the current attempt, even during a rerun.
        while not has_result(run) and attempt_number > 1:
            attempt_number -= 1
            run = api(f"{base}/actions/runs/{run_id}/attempts/{attempt_number}")
        if has_result(run):
            results.append(run)
    return results


def assess(snapshot, now, settings: Settings):
    findings = []
    for name, runs in snapshot["workflows"].items():
        completed = [run for run in runs if has_result(run)]
        if completed and completed[0]["conclusion"] != "success":
            findings.append(finding("workflow:" + name, settings.text("workflow_failed", name=name),
                                    completed[0]["html_url"]))
    for name, runs in snapshot["schedules"].items():
        latest = max((run for run in runs if has_result(run)),
                     key=lambda run: timestamp(run["updated_at"]), default=None)
        if latest is None or now - timestamp(latest["updated_at"]) > timedelta(hours=36):
            findings.append(finding("schedule:" + name, settings.text("schedule_missing", name=name)))
        if latest is not None and latest["conclusion"] != "success":
            findings.append(finding("schedule-outcome:" + name,
                                    settings.text("schedule_failed", name=name, conclusion=latest["conclusion"]),
                                    latest["html_url"]))
    latest_updates = {}
    for run in snapshot["updates"]:
        if not is_dependabot_update(run) or not has_result(run):
            continue
        latest_updates.setdefault(updater_name(run), run)
    for name, run in latest_updates.items():
        if run["conclusion"] != "success":
            findings.append(finding("updater:" + name, settings.text("updater_failed", name=name), run["html_url"]))
    for pull in snapshot["pulls"]:
        if pull["user"]["login"] == "dependabot[bot]" and now - timestamp(pull["created_at"]) >= timedelta(days=7):
            findings.append(finding("pull:" + str(pull["number"]), settings.text("pull_stale", title=pull["title"]),
                                    pull["html_url"]))
    return findings


def collect(api: Api, config, now, settings: Settings):
    base = "repos/" + config["repository"]
    snapshot = {"workflows": {}, "schedules": {}, "updates": [], "pulls": []}
    errors = []
    branch = None
    for section, names in (("workflows", config["workflows"]), ("schedules", config["scheduled_workflows"])):
        for name in names:
            suffix = "&event=schedule" if section == "schedules" else ""
            try:
                branch = branch or api(base)["default_branch"]
                runs = api(base + "/actions/workflows/" + name + "/runs?branch=" + branch + "&per_page=20" + suffix)
                snapshot[section][name] = completed_results(api, base, runs["workflow_runs"])
            except READ_ERRORS:
                errors.append(finding("read:" + section + ":" + name, settings.text("read_history", name=name),
                                      level="error"))
    try:
        since = (now - timedelta(days=30)).strftime("%Y-%m-%d")
        pages = api(base + "/actions/runs?event=dynamic&per_page=100&created=>=" + since, paginate=True)
        runs = (run for page in pages for run in page["workflow_runs"] if is_dependabot_update(run))
        snapshot["updates"] = completed_results(api, base, runs)
        pages = api(base + "/pulls?state=open&per_page=100", paginate=True)
        snapshot["pulls"] = [pull for page in pages for pull in page]
    except READ_ERRORS:
        errors.append(finding("read:dependencies", settings.text("read_dependencies"), level="error"))
    observed = ["workflow:" + name for name, runs in snapshot["workflows"].items() if any(has_result(run) for run in runs)]
    observed += ["schedule-outcome:" + name for name, runs in snapshot["schedules"].items() if runs]
    observed += ["updater:" + updater_name(run) for run in snapshot["updates"] if is_dependabot_update(run)]
    return {"checked_at": now.isoformat(), "observed_keys": observed, "findings": assess(snapshot, now, settings) + errors}


def fingerprint(findings):
    values = sorted((item["key"], item["level"], item["summary"]) for item in findings)
    return hashlib.sha256(json.dumps(values, ensure_ascii=False).encode()).hexdigest()


def render(report, run_url, settings: Settings):
    digest = fingerprint(report["findings"])
    lines = [settings.start, settings.fingerprint_marker(digest),
             settings.text("checked_at", checked_at=report["checked_at"]), "",
             settings.text("checked_by", run_url=run_url), ""]
    if report["findings"]:
        for item in sorted(report["findings"], key=lambda x: x["key"]):
            lines.append("- " + item["summary"] + (settings.text("evidence", url=item["url"]) if item["url"] else ""))
    else:
        lines.append(settings.text("nothing"))
    lines += ["", settings.text("footer"), settings.end]
    return "\n".join(lines), digest


def managed_block_span(body, settings: Settings):
    start, end = settings.start, settings.end
    if body.count(start) != 1 or body.count(end) != 1 or body.index(start) >= body.index(end):
        raise ValueError("Expected exactly one managed maintenance block")
    return body.index(start), body.index(end) + len(end)


def reconcile(body, report, run_url, settings: Settings):
    start, end = managed_block_span(body, settings)
    block, digest = render(report, run_url, settings)
    previous = settings.fingerprint_pattern.search(body[start:end])
    changed = previous is None or previous.group(1) != digest
    updated = body[:start] + block + body[end:]
    return updated, changed, digest


def publish(api: Api, config, report, run_url, settings: Settings):
    base = "repos/" + config["repository"]
    pages = api(base + "/issues?state=open&per_page=100", paginate=True)
    existing = {}
    for issue in (row for page in pages for row in page):
        if "pull_request" in issue:
            continue
        markers = settings.incident_pattern.findall(issue.get("body") or "")
        if not markers:
            continue
        if len(markers) != 1:
            raise ValueError(f"Ambiguous maintenance incident marker in issue #{issue['number']}")
        key = json.loads(markers[0])
        if key in existing:
            raise ValueError("Duplicate open maintenance incident: " + key)
        existing[key] = issue
    current = {item["key"]: item for item in report["findings"]}
    if len(current) != len(report["findings"]):
        raise ValueError("Duplicate finding keys")
    for key, item in sorted(current.items()):
        single = {**report, "findings": [item]}
        title = settings.title(item["summary"])
        if key not in existing:
            block, _ = render(single, run_url, settings)
            api(base + "/issues", "POST", {"title": title, "body": settings.incident_marker(key) + "\n" + block})
            continue
        issue = existing[key]
        updated, _, _ = reconcile(issue["body"], single, run_url, settings)
        if updated != issue["body"] or title != issue["title"]:
            api(base + "/issues/" + str(issue["number"]), "PATCH", {"title": title, "body": updated})
    # Absence is not recovery if any source is missing or failed.
    if not report.get("complete"):
        return
    for key, issue in existing.items():
        if key in current:
            continue
        if key.startswith(CLOSE_ONLY_WHEN_OBSERVED) and key not in report.get("observed_keys", []):
            continue
        if key.startswith("pull:"):
            pull = api(base + "/pulls/" + key.split(":", 1)[1])
            if pull["state"] != "closed":
                continue
        path = base + "/issues/" + str(issue["number"])
        body = issue["body"]
        start, end = managed_block_span(body, settings)
        episode = hashlib.sha256(body[start:end].replace("\r\n", "\n").encode()).hexdigest()
        marker = settings.resolved_marker(episode)
        comments = api(path + "/comments?per_page=100", paginate=True)
        if not any(marker in row["body"] for page in comments for row in page):
            api(path + "/comments", "POST", {"body": settings.text(
                "resolved", checked_at=report["checked_at"], run_url=run_url, marker=marker)})
        api(path, "PATCH", {"state": "closed", "state_reason": "completed"})


def newest_artifact(artifacts, pattern):
    """The most recently created unexpired artifact whose name matches the glob, so a rerun's upload wins."""
    matches = [a for a in artifacts if not a["expired"] and fnmatch.fnmatchcase(a["name"], pattern)]
    return max(matches, key=lambda a: (a["created_at"], a["id"]), default=None)


def fetch_artifacts(api: Api, download, repo, run_id, patterns, directory: Path):
    """Best effort: a missing artifact surfaces later as a missing or invalid extra report."""
    if not patterns:
        return
    try:
        pages = api(f"repos/{repo}/actions/runs/{run_id}/artifacts?per_page=100", paginate=True)
        artifacts = [artifact for page in pages for artifact in page["artifacts"]]
    except READ_ERRORS:
        print("::warning::Cannot list the artifacts of this run.")
        return
    for pattern in patterns:
        artifact = newest_artifact(artifacts, pattern)
        if artifact is None:
            print(f"::warning::No unexpired artifact in this run matches {pattern}.")
            continue
        try:
            download(repo, run_id, artifact["name"], directory)
        except (subprocess.CalledProcessError, OSError):
            print(f"::warning::Cannot download the artifact {artifact['name']}.")


def main(argv=None, api: Api = gh_api, download=gh_download):
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--extra", action="append", default=[])
    parser.add_argument("--job-result", action="append", default=[], metavar="NAME=CONCLUSION")
    parser.add_argument("--output", required=True)
    parser.add_argument("--publish", action="store_true")
    parser.add_argument("--run-url")
    parser.add_argument("--marker-prefix", default=Settings.marker_prefix)
    parser.add_argument("--title-prefix")
    parser.add_argument("--language", choices=[language.value for language in Language], default=Language.EN.value)
    parser.add_argument("--artifact-pattern", action="append", default=[])
    parser.add_argument("--artifact-dir", type=Path, default=Path("."))
    args = parser.parse_args(argv)
    settings = Settings(args.marker_prefix, args.title_prefix, Language(args.language))
    run_url = args.run_url or settings.text("local_run")
    config = {"repository": os.environ.get("GITHUB_REPOSITORY", ""), **json.loads(Path(args.config).read_text())}
    fetch_artifacts(api, download, config["repository"], os.environ.get("GITHUB_RUN_ID", ""),
                    args.artifact_pattern, args.artifact_dir)
    now = datetime.now(timezone.utc)
    report = collect(api, config, now, settings)
    provided = {Path(file).name for file in args.extra}
    for name in set(config.get("required_reports", [])) - provided:
        report["findings"].append(finding("read:extra:" + name, settings.text("extra_missing", name=name),
                                          level="error"))
    for file in args.extra:
        try:
            extra = json.loads(Path(file).read_text())
            if not isinstance(extra, dict) or not isinstance(extra.get("findings"), list):
                raise ValueError("Invalid maintenance report")
            for item in extra["findings"]:
                if not isinstance(item, dict) or not all(isinstance(item.get(key), str) for key in ("key", "level", "summary", "url")):
                    raise ValueError("Invalid maintenance finding")
            report["findings"].extend(extra["findings"])
        except (OSError, ValueError, KeyError, TypeError):
            report["findings"].append(finding("read:extra:" + Path(file).name,
                                              settings.text("extra_invalid", name=Path(file).name), level="error"))
    for job_result in args.job_result:
        name, separator, conclusion = job_result.partition("=")
        if not name or not separator:
            parser.error("--job-result requires NAME=CONCLUSION")
        key = "job:" + name
        report["observed_keys"].append(key)
        if conclusion != "success":
            report["findings"].append(finding(key, settings.text(
                "job_result", name=name, conclusion=conclusion or settings.text("unknown")), run_url, level="error"))
    report["complete"] = bool(config.get("required_reports")) and not any(
        item["level"] == "error" or item["key"] == "deployed:unscanned" for item in report["findings"])
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    if args.publish:
        publish(api, config, report, run_url, settings)
    print(json.dumps({"findings": len(report["findings"]), "published": args.publish}))
    return int(any(item["level"] == "error" for item in report["findings"]))


if __name__ == "__main__":
    raise SystemExit(main())

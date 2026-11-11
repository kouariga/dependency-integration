import json
import subprocess
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock
from unittest.mock import patch

import support  # noqa: F401  (puts the repository root on sys.path)

import maintenance
from maintenance import Language, Settings

JA = Settings(language=Language.JA)
BRANCH = {"default_branch": "main"}


def dependabot_run(name, status, conclusion, url):
    return {"name": name, "status": status, "conclusion": conclusion, "html_url": url,
            "event": "dynamic", "path": "dynamic/dependabot/dependabot-updates",
            "actor": {"login": "dependabot[bot]"}}


class MaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 11, 1, tzinfo=timezone.utc)
        self.report = {"checked_at": self.now.isoformat(), "findings": [maintenance.finding("ffmpeg", "新版があります。", "https://example.com/release")]}
        self.body = "Operator notes\n" + JA.start + "\nWaiting\n" + JA.end + "\nKeep this"

    def test_reconcile_preserves_operator_notes_and_deduplicates_changed_links(self):
        body, changed, _ = maintenance.reconcile(self.body, self.report, "run1", JA)
        self.assertTrue(changed)
        self.assertTrue(body.startswith("Operator notes\n"))
        self.assertTrue(body.endswith("\nKeep this"))
        self.report["findings"][0]["url"] = "https://example.com/new-run"
        updated, changed, _ = maintenance.reconcile(body, self.report, "run2", JA)
        self.assertFalse(changed)
        self.assertIn("new-run", updated)

    def test_recovery_and_recurrence_notify(self):
        body, _, _ = maintenance.reconcile(self.body, self.report, "run1", JA)
        recovery = {**self.report, "findings": []}
        body, changed, _ = maintenance.reconcile(body, recovery, "run2", JA)
        self.assertTrue(changed)
        _, changed, _ = maintenance.reconcile(body, self.report, "run3", JA)
        self.assertTrue(changed)

    def test_refuses_missing_or_ambiguous_managed_block(self):
        for body in ("human issue", self.body + self.body, JA.end + JA.start):
            with self.subTest(body=body), self.assertRaises(ValueError):
                maintenance.reconcile(body, self.report, "run", JA)

    def test_incident_lifecycle_and_retry(self):
        issues = []
        comments = {}
        writes = []
        fail_close = [False]

        def api(path, method="GET", data=None, paginate=False):
            if method == "GET":
                if "/comments?" in path:
                    return [comments.get(int(path.split("/issues/")[1].split("/")[0]), [])]
                return [[dict(i) for i in issues if i["state"] == "open"]]
            writes.append((path, method, data))
            if path.endswith("/issues"):
                issues.append({**data, "number": len(issues) + 1, "state": "open"})
                return issues[-1]
            number = int(path.split("/issues/")[1].split("/")[0])
            if path.endswith("/comments"):
                comments.setdefault(number, []).append(data)
            else:
                if fail_close[0] and data.get("state") == "closed":
                    fail_close[0] = False
                    raise RuntimeError("transient close failure")
                issues[number - 1].update(data)
            return {}

        config = {"repository": "owner/repo"}
        healthy = {**self.report, "findings": [], "complete": True}
        maintenance.publish(api, config, healthy, "run0", JA)
        self.assertEqual(writes, [])
        maintenance.publish(api, config, self.report, "run1", JA)
        self.assertEqual(len(issues), 1)
        issues[0]["body"] = "Operator handoff\n\n" + issues[0]["body"] + "\nOperator note"
        maintenance.publish(api, config, self.report, "run2", JA)
        self.assertEqual(len(issues), 1)
        self.assertTrue(issues[0]["body"].startswith("Operator handoff\n\n"))
        self.assertIn("Operator note", issues[0]["body"])
        self.assertEqual(comments, {})
        maintenance.publish(api, config, {**healthy, "complete": False}, "failed-run", JA)
        self.assertEqual(issues[0]["state"], "open")
        fail_close[0] = True
        with self.assertRaises(RuntimeError):
            maintenance.publish(api, config, healthy, "run3", JA)
        issues[0]["body"] += "\nNote added after closure failed"
        maintenance.publish(api, config, healthy, "run3-next-attempt", JA)
        self.assertEqual(len(comments[1]), 1)
        self.assertIn("run3", comments[1][0]["body"])
        self.assertEqual(issues[0]["state"], "closed")
        issues[0]["state"] = "open"
        maintenance.publish(api, config, self.report, "reopened-with-finding", JA)
        maintenance.publish(api, config, healthy, "reopened-recovery", JA)
        self.assertEqual(len(comments[1]), 2)
        self.assertIn("reopened-recovery", comments[1][1]["body"])
        self.assertEqual(issues[0]["state"], "closed")
        maintenance.publish(api, config, self.report, "recurrence", JA)
        self.assertEqual(len(issues), 2)
        self.assertEqual(issues[1]["state"], "open")

    def test_damaged_managed_block_cannot_be_closed(self):
        issue = {"number": 9, "title": "old finding", "body": '<!-- rise-maintenance-incident:"ffmpeg" -->\n'
                 + JA.start + "\nMissing end"}
        healthy = {**self.report, "findings": [], "complete": True}
        api = mock.Mock(return_value=[[issue]])
        with self.assertRaises(ValueError):
            maintenance.publish(api, {"repository": "owner/repo"}, healthy, "run", JA)
        self.assertEqual(api.call_count, 1)

    def test_missing_history_does_not_resolve_expired_updater(self):
        key = "updater:old security job"
        report = {**self.report, "findings": [], "complete": True, "observed_keys": []}
        issue = {"number": 9, "title": "old", "body": '<!-- rise-maintenance-incident:' + json.dumps(key) + ' -->\n'}
        api = mock.Mock(return_value=[[issue]])
        maintenance.publish(api, {"repository": "owner/repo"}, report, "run", JA)
        self.assertEqual(api.call_count, 1)

    def test_scheduled_failure_recovers_only_with_observed_scheduled_success(self):
        key = "schedule-outcome:refresh.yml"
        issue = {"number": 9, "title": "failed schedule", "body": '<!-- rise-maintenance-incident:' + json.dumps(key) + ' -->\n'
                 + JA.start + "\nPrior failure\n" + JA.end}
        config = {"repository": "owner/repo", "workflows": [], "scheduled_workflows": ["refresh.yml"]}
        runs = [{"id": run, "run_attempt": 1, "status": "completed", "conclusion": "cancelled"}
                for run in range(20)]
        writes = []

        def api(path, method="GET", data=None, paginate=False):
            if method != "GET":
                writes.append((path, method, data))
            elif path == "repos/owner/repo":
                return BRANCH
            elif "/workflows/refresh.yml/runs?" in path:
                return {"workflow_runs": runs}
            elif "/issues?state=open" in path:
                return [[issue]]
            return []

        report = {**maintenance.collect(api, config, self.now, JA), "complete": True}
        maintenance.publish(api, config, report, "missing-run", JA)
        self.assertEqual([(path, method) for path, method, _ in writes], [("repos/owner/repo/issues", "POST")])
        writes.clear()
        runs[0].update(conclusion="success", updated_at="2026-11-11T00:00:00Z")
        report = {**maintenance.collect(api, config, self.now, JA), "complete": True}
        maintenance.publish(api, config, report, "successful-run", JA)
        self.assertEqual([(path, method) for path, method, _ in writes],
                         [("repos/owner/repo/issues/9/comments", "POST"), ("repos/owner/repo/issues/9", "PATCH")])
        self.assertEqual(writes[-1][2], {"state": "closed", "state_reason": "completed"})

    def test_pending_dependabot_rerun_preserves_failure_despite_older_success(self):
        key = "updater:npm_and_yarn in /. for glob"
        issue = {"number": 9, "title": "failed update", "body": '<!-- rise-maintenance-incident:' + json.dumps(key) + ' -->\n'
                 + JA.start + "\nPrior failure\n" + JA.end}
        config = {"repository": "owner/repo", "workflows": [], "scheduled_workflows": []}
        older = {**dependabot_run("npm_and_yarn in /. for glob - Update #1694355430", "completed", "success", "older"),
                 "id": 123, "run_attempt": 1}
        failed = {**dependabot_run("npm_and_yarn in /. for glob - Update #1694355431", "completed", "failure", "failed"),
                  "id": 124, "run_attempt": 1}
        retry = {**failed, "run_attempt": 2, "status": "in_progress", "conclusion": None, "html_url": "retry"}
        writes = []

        def api(path, method="GET", data=None, paginate=False):
            if method != "GET":
                writes.append((path, method, data))
            elif "/actions/runs?event=dynamic" in path:
                return [{"workflow_runs": [retry, older]}]
            elif path.endswith("/runs/124/attempts/1"):
                return failed
            elif "/issues?state=open" in path:
                return [[issue]]
            return []

        report = {**maintenance.collect(api, config, self.now, JA), "complete": True}
        self.assertEqual([(item["key"], item["url"]) for item in report["findings"]], [(key, "failed")])
        maintenance.publish(api, config, report, "running", JA)
        self.assertEqual([(path, method) for path, method, _ in writes], [("repos/owner/repo/issues/9", "PATCH")])
        self.assertNotIn("state", writes[0][2])
        writes.clear()
        retry.update(status="completed", conclusion="success")
        report = {**maintenance.collect(api, config, self.now, JA), "complete": True}
        maintenance.publish(api, config, report, "successful-retry", JA)
        self.assertEqual([(path, method) for path, method, _ in writes],
                         [("repos/owner/repo/issues/9/comments", "POST"), ("repos/owner/repo/issues/9", "PATCH")])
        self.assertEqual(writes[-1][2], {"state": "closed", "state_reason": "completed"})

    def test_inventory_only_and_missing_reports_cannot_close_incidents(self):
        with TemporaryDirectory() as directory:
            directory = Path(directory)
            config, extra, output = (directory / name for name in ("config.json", "report.json", "output.json"))
            config.write_text(json.dumps({"required_reports": ["report.json"]}))
            extra.write_text(json.dumps({"findings": [maintenance.finding("deployed:unscanned", "Not scanned")]}))
            for additional in ([], ["--extra", str(extra)]):
                with patch.object(maintenance, "collect", return_value={"findings": []}):
                    maintenance.main(["--config", str(config), "--output", str(output), *additional])
                    self.assertFalse(json.loads(output.read_text())["complete"])

    def test_detects_missing_schedule_stale_pr_and_unresolved_updater(self):
        snapshot = {"workflows": {}, "schedules": {"refresh.yml": []}, "pulls": [
            {"number": 1, "user": {"login": "dependabot[bot]"}, "created_at": "2026-11-01T00:00:00Z", "title": "Bump", "html_url": "pr"},
            {"number": 2, "user": {"login": "human"}, "created_at": "2026-11-01T00:00:00Z", "title": "Feature", "html_url": "pr"}],
            "updates": [dependabot_run("npm_and_yarn in /. - Update #2", "completed", "success", "new"),
                        dependabot_run("npm_and_yarn in /. - Update #1", "completed", "failure", "old"),
                        dependabot_run("uv in /. - Update #3", "completed", "failure", "failed")]}
        keys = {item["key"] for item in maintenance.assess(snapshot, self.now, JA)}
        self.assertEqual(keys, {"schedule:refresh.yml", "pull:1", "updater:uv in /."})

    def test_preserves_distinct_security_jobs_and_filters_other_dynamic_runs(self):
        version = dependabot_run("npm_and_yarn in /. - Update #1693493263", "completed", "success", "version")
        glob = dependabot_run("npm_and_yarn in /. for glob - Update #1694355431", "completed", "failure", "glob")
        minimatch = dependabot_run("npm_and_yarn in /. for minimatch - Update #1694355442",
                                  "completed", "failure", "minimatch")
        graph = {**glob, "name": "Configured Graph Update: uv in /. #1694461382",
                 "path": "dynamic/dependabot/update-graph", "html_url": "graph"}
        other_actor = {**glob, "actor": {"login": "other[bot]"}, "html_url": "other"}
        snapshot = {"workflows": {}, "schedules": {}, "pulls": [],
                    "updates": [other_actor, version, graph, glob, minimatch]}
        self.assertEqual({item["key"]: item["url"] for item in maintenance.assess(snapshot, self.now, JA)},
                         {"updater:npm_and_yarn in /. for glob": "glob",
                          "updater:npm_and_yarn in /. for minimatch": "minimatch"})

    def test_running_retry_does_not_clear_terminal_updater_errors(self):
        for conclusion in ("failure", "timed_out", "action_required", "startup_failure"):
            with self.subTest(conclusion=conclusion):
                runs = [dependabot_run("npm_and_yarn in /. - Update #3", "in_progress", None, "running"),
                        dependabot_run("npm_and_yarn in /. - Update #2", "completed", conclusion, "failed")]
                snapshot = {"workflows": {}, "schedules": {}, "pulls": [], "updates": runs}
                self.assertEqual([item["url"] for item in maintenance.assess(snapshot, self.now, JA)], ["failed"])
                runs.insert(0, dependabot_run("npm_and_yarn in /. - Update #4", "completed", "success", "fixed"))
                self.assertEqual(maintenance.assess(snapshot, self.now, JA), [])

    def test_schedule_requires_a_recent_completed_result(self):
        runs = [{"status": "completed", "conclusion": "success", "created_at": "2026-11-08T00:00:00Z",
                 "updated_at": "2026-11-09T12:59:59Z", "html_url": "prior"}]
        for status, conclusion in (("queued", None), ("in_progress", None), ("completed", "cancelled"),
                                   ("completed", "skipped")):
            runs.insert(0, {"status": status, "conclusion": conclusion, "created_at": "2026-11-11T00:00:00Z",
                            "updated_at": "2026-11-11T00:01:00Z", "html_url": "recent"})
        snapshot = {"workflows": {}, "schedules": {"refresh.yml": runs}, "pulls": [], "updates": []}
        self.assertEqual([item["key"] for item in maintenance.assess(snapshot, self.now, JA)], ["schedule:refresh.yml"])
        runs[-1]["updated_at"] = "2026-11-09T13:00:00Z"
        self.assertEqual(maintenance.assess(snapshot, self.now, JA), [])

    def test_same_run_retry_preserves_completed_failure(self):
        older = {"id": 123, "run_attempt": 1, "status": "completed", "conclusion": "success",
                 "updated_at": "2026-11-10T00:00:00Z", "html_url": "prior"}
        failed = {"id": 124, "run_attempt": 1, "status": "completed", "conclusion": "failure",
                  "updated_at": "2026-11-10T23:00:00Z", "html_url": "failed"}
        retry = {**failed, "run_attempt": 2, "status": "in_progress", "conclusion": None}
        config = {"repository": "owner/repo", "workflows": ["refresh.yml"], "scheduled_workflows": ["refresh.yml"]}

        def api(path, **kwargs):
            if path == "repos/owner/repo":
                return BRANCH
            if "/workflows/refresh.yml/runs?" in path:
                return {"workflow_runs": [retry, older]}
            if path.endswith("/runs/124/attempts/1"):
                return failed
            return []

        findings = maintenance.collect(api, config, self.now, JA)["findings"]
        self.assertEqual([(item["key"], item["url"]) for item in findings],
                         [("workflow:refresh.yml", "failed"), ("schedule-outcome:refresh.yml", "failed")])
        retry.update(status="completed", conclusion="success")
        self.assertEqual(maintenance.collect(api, config, self.now, JA)["findings"], [])

    def test_current_job_result_overrides_prior_healthy_artifact(self):
        with TemporaryDirectory() as directory:
            directory = Path(directory)
            config, extra, output = (directory / name for name in ("config.json", "old.json", "report.json"))
            config.write_text("{}")
            extra.write_text('{"findings": []}')
            command = ["--config", str(config), "--extra", str(extra), "--output", str(output), "--job-result"]
            for conclusion in ("failure", "cancelled", "skipped", "", "success"):
                with self.subTest(conclusion=conclusion), \
                        patch.object(maintenance, "collect", return_value={"findings": [], "observed_keys": []}):
                    success = conclusion == "success"
                    self.assertEqual(maintenance.main([*command, "scan=" + conclusion]), 0 if success else 1)
                    findings = json.loads(output.read_text())["findings"]
                    self.assertEqual([(item["key"], item["level"]) for item in findings],
                                     [] if success else [("job:scan", "error")])

    def test_job_incident_closes_only_after_observed_success(self):
        issue = {"number": 9, "title": "failed scan", "body": '<!-- rise-maintenance-incident:"job:scan" -->\n'
                 + JA.start + "\nPrior failure\n" + JA.end}
        writes = []

        def api(path, method="GET", data=None, paginate=False):
            if method != "GET":
                writes.append((path, method, data))
            elif "/issues?state=open" in path:
                return [[issue]]
            return []

        with TemporaryDirectory() as directory:
            directory = Path(directory)
            config, extra, output = (directory / name for name in ("config.json", "scan.json", "report.json"))
            config.write_text(json.dumps({"repository": "owner/repo", "required_reports": ["scan.json"]}))
            extra.write_text('{"findings": []}')
            command = ["--config", str(config), "--extra", str(extra), "--output", str(output), "--publish"]
            healthy = {"checked_at": self.now.isoformat(), "findings": [], "observed_keys": []}
            with patch.object(maintenance, "collect", return_value=healthy):
                self.assertEqual(maintenance.main(command, api=api), 0)
                self.assertTrue(json.loads(output.read_text())["complete"])
                self.assertEqual(writes, [])
                self.assertEqual(maintenance.main([*command, "--job-result", "scan=success"], api=api), 0)
        self.assertEqual([(path, method) for path, method, _ in writes],
                         [("repos/owner/repo/issues/9/comments", "POST"), ("repos/owner/repo/issues/9", "PATCH")])
        self.assertEqual(writes[-1][2], {"state": "closed", "state_reason": "completed"})

    def test_malformed_extra_report_is_published_as_error(self):
        with TemporaryDirectory() as directory:
            directory = Path(directory)
            config, extra, output = (directory / name for name in ("config.json", "bad.json", "report.json"))
            config.write_text("{}")
            for payload in ([], {"findings": None}, {"findings": ["invalid"]}):
                extra.write_text(json.dumps(payload))
                with self.subTest(payload=payload), \
                        patch.object(maintenance, "collect", return_value={"findings": []}), \
                        patch.object(maintenance, "publish") as publish:
                    command = ["--config", str(config), "--extra", str(extra), "--output", str(output), "--publish"]
                    self.assertEqual(maintenance.main(command), 1)
                    self.assertEqual(json.loads(output.read_text())["findings"][0]["level"], "error")
                    publish.assert_called_once()

    def test_unreadable_history_is_error_not_healthy(self):
        config = {"repository": "owner/repo", "workflows": ["ci.yml"], "scheduled_workflows": []}
        api = mock.Mock(side_effect=subprocess.CalledProcessError(1, "gh"))
        report = maintenance.collect(api, config, self.now, JA)
        self.assertEqual({item["level"] for item in report["findings"]}, {"error"})
        self.assertEqual(len(report["findings"]), 2)


class ConfigurationTests(unittest.TestCase):
    def test_default_marker_prefix_keeps_existing_issue_markers(self):
        settings = Settings()
        self.assertEqual(settings.start, "<!-- rise-maintenance:start -->")
        self.assertEqual(settings.end, "<!-- rise-maintenance:end -->")
        self.assertEqual(settings.incident_marker("job:scan"), '<!-- rise-maintenance-incident:"job:scan" -->')
        self.assertEqual(settings.fingerprint_marker("a" * 64), "<!-- rise-maintenance-fingerprint:" + "a" * 64 + " -->")
        self.assertEqual(settings.resolved_marker("b" * 64), "<!-- rise-maintenance-resolved:" + "b" * 64 + " -->")

    def test_marker_prefix_selects_which_issues_are_managed(self):
        body = '<!-- rise-maintenance-incident:"job:scan" -->\n' + JA.start + "\nPrior failure\n" + JA.end
        issue = {"number": 9, "title": "failed scan", "body": body}
        report = {"checked_at": "2026-11-11T01:00:00+00:00", "findings": [], "complete": True,
                  "observed_keys": ["job:scan"]}
        for prefix, closed in (("rise-maintenance", True), ("other-maintenance", False)):
            writes = []

            def api(path, method="GET", data=None, paginate=False):
                if method != "GET":
                    writes.append((path, method))
                return [[issue]] if "/issues?state=open" in path else []

            with self.subTest(prefix=prefix):
                maintenance.publish(api, {"repository": "owner/repo"}, report, "run", Settings(prefix, None, Language.JA))
                self.assertEqual(("repos/owner/repo/issues/9", "PATCH") in writes, closed)

    def test_marker_prefix_must_be_safe_inside_an_html_comment(self):
        for prefix in ("", "a -->", "-lead", "a b"):
            with self.subTest(prefix=prefix), self.assertRaises(ValueError):
                Settings(prefix)

    def test_every_language_defines_every_message(self):
        keys = {language: set(table) for language, table in maintenance.MESSAGES.items()}
        self.assertEqual(set(keys), set(Language))
        self.assertEqual(keys[Language.EN], keys[Language.JA])

    def test_language_selects_wording_and_default_title_prefix(self):
        snapshot = {"workflows": {"ci.yml": [{"status": "completed", "conclusion": "failure", "html_url": "run"}]},
                    "schedules": {}, "pulls": [], "updates": []}
        now = datetime(2026, 11, 11, tzinfo=timezone.utc)
        english = maintenance.assess(snapshot, now, Settings())[0]["summary"]
        japanese = maintenance.assess(snapshot, now, JA)[0]["summary"]
        self.assertEqual(english, "The latest completed run of ci.yml failed.")
        self.assertEqual(japanese, "ci.yml の直近完了実行が失敗しています。")
        self.assertEqual(Settings().title(english), "[Maintenance] " + english)
        self.assertEqual(JA.title(japanese), "[保守] " + japanese)
        self.assertEqual(Settings(title_prefix="").title(english), english)

    def test_japanese_block_matches_the_existing_issue_format(self):
        report = {"checked_at": "2026-11-11T01:00:00+00:00",
                  "findings": [maintenance.finding("pull:7", "Dependabot PR が7日以上未統合です。Bump", "https://example.com/pr")]}
        block, digest = maintenance.render(report, "https://example.com/run", JA)
        self.assertEqual(block, "\n".join([
            "<!-- rise-maintenance:start -->",
            "<!-- rise-maintenance-fingerprint:" + digest + " -->",
            "確認時刻 2026-11-11T01:00:00+00:00", "",
            "確認実行 https://example.com/run", "",
            "- Dependabot PR が7日以上未統合です。Bump [根拠](https://example.com/pr)", "",
            "本番配備や例外延長は自動では行いません。検査が正常に完了し解消を確認した指摘は自動で閉じます。",
            "<!-- rise-maintenance:end -->",
        ]))

    def test_workflow_history_is_read_from_the_default_branch(self):
        requested = []

        def api(path, method="GET", data=None, paginate=False):
            requested.append(path)
            if path == "repos/owner/repo":
                return {"default_branch": "trunk"}
            if "/workflows/" in path:
                return {"workflow_runs": []}
            return [{"workflow_runs": []}] if "/actions/runs?" in path else [[]]

        config = {"repository": "owner/repo", "workflows": ["ci.yml"], "scheduled_workflows": ["daily.yml"]}
        maintenance.collect(api, config, datetime(2026, 11, 11, tzinfo=timezone.utc), Settings())
        self.assertIn("repos/owner/repo/actions/workflows/ci.yml/runs?branch=trunk&per_page=20", requested)
        self.assertIn("repos/owner/repo/actions/workflows/daily.yml/runs?branch=trunk&per_page=20&event=schedule",
                      requested)
        self.assertEqual(requested.count("repos/owner/repo"), 1)

    def test_vulnerability_findings_never_fail_the_report(self):
        with TemporaryDirectory() as directory:
            directory = Path(directory)
            config, extra, output = (directory / name for name in ("config.json", "report.json", "output.json"))
            config.write_text(json.dumps({"required_reports": ["report.json"]}))
            extra.write_text(json.dumps({"findings": [
                maintenance.finding("deployed:fix-available:api", "fixable", level="attention"),
                maintenance.finding("deployed:no-fix:api", "not fixable", level="warning")]}))
            with patch.object(maintenance, "collect", return_value={"findings": [], "observed_keys": []}):
                command = ["--config", str(config), "--extra", str(extra), "--output", str(output),
                           "--job-result", "scan=success"]
                self.assertEqual(maintenance.main(command), 0)
            report = json.loads(output.read_text())
            self.assertTrue(report["complete"])
            self.assertEqual([item["level"] for item in report["findings"]], ["attention", "warning"])


class ArtifactTests(unittest.TestCase):
    ARTIFACTS = [
        {"id": 1, "name": "deployed-security-1", "expired": False, "created_at": "2026-11-11T01:00:00Z"},
        {"id": 3, "name": "deployed-security-2", "expired": False, "created_at": "2026-11-11T03:00:00Z"},
        {"id": 4, "name": "deployed-security-3", "expired": True, "created_at": "2026-11-11T04:00:00Z"},
        {"id": 2, "name": "maintenance-report-1", "expired": False, "created_at": "2026-11-11T05:00:00Z"},
    ]

    def test_newest_unexpired_match_wins(self):
        self.assertEqual(maintenance.newest_artifact(self.ARTIFACTS, "deployed-security-*")["name"],
                         "deployed-security-2")
        self.assertIsNone(maintenance.newest_artifact(self.ARTIFACTS, "upstream-*"))

    def test_attempt_ten_is_newer_than_attempt_nine(self):
        artifacts = [{"id": 9, "name": "scan-9", "expired": False, "created_at": "2026-11-11T01:00:00Z"},
                     {"id": 10, "name": "scan-10", "expired": False, "created_at": "2026-11-11T02:00:00Z"}]
        self.assertEqual(maintenance.newest_artifact(artifacts, "scan-*")["name"], "scan-10")

    def test_downloads_each_pattern_and_tolerates_missing_artifacts(self):
        downloads = []
        api = mock.Mock(return_value=[{"artifacts": self.ARTIFACTS}])
        maintenance.fetch_artifacts(api, lambda *args: downloads.append(args), "owner/repo", "42",
                                    ["deployed-security-*", "upstream-*"], Path("out"))
        api.assert_called_once_with("repos/owner/repo/actions/runs/42/artifacts?per_page=100", paginate=True)
        self.assertEqual(downloads, [("owner/repo", "42", "deployed-security-2", Path("out"))])

    def test_failed_download_surfaces_as_missing_required_report(self):
        def download(*args):
            raise subprocess.CalledProcessError(1, "gh")

        with TemporaryDirectory() as directory:
            directory = Path(directory)
            config, output = directory / "config.json", directory / "output.json"
            config.write_text(json.dumps({"required_reports": ["report.json"]}))
            api = mock.Mock(return_value=[{"artifacts": self.ARTIFACTS}])
            with patch.object(maintenance, "collect", return_value={"findings": [], "observed_keys": []}):
                command = ["--config", str(config), "--output", str(output), "--extra", str(directory / "report.json"),
                           "--artifact-pattern", "deployed-security-*", "--artifact-dir", str(directory)]
                self.assertEqual(maintenance.main(command, api=api, download=download), 1)
            self.assertEqual([item["key"] for item in json.loads(output.read_text())["findings"]],
                             ["read:extra:report.json"])


if __name__ == "__main__":
    unittest.main()

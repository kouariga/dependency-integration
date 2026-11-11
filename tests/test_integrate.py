import copy
import datetime
import tempfile
import unittest
from pathlib import Path

from support import REPO, FakeGitHub, fixture, pull

import integrate
from integrate import Candidate, Decision, Outcome, Summary

IGNORED = integrate.parse_ignore_checks("codex-cloud-review")
SHA = "1111111111111111111111111111111111111111"


def candidate(number: int = 532, draft: bool = False) -> Candidate:
    return Candidate(number, f"Bump package-{number}", SHA, f"https://github.com/{REPO}/pull/{number}", draft)


def real_runs() -> list[dict]:
    return copy.deepcopy(fixture("check_runs_dependabot.json")["check_runs"])


def with_ci(status: str, conclusion: str | None) -> list[dict]:
    runs = real_runs()
    ci = next(r for r in runs if r["name"] == "ci")
    ci["status"], ci["conclusion"] = status, conclusion
    return runs


class EligibilityTest(unittest.TestCase):
    def test_real_payload_with_failed_ci_is_failing(self):
        self.assertIs(integrate.decide(candidate(), real_runs(), [], IGNORED), Decision.FAILING)

    def test_ignored_check_failure_does_not_block(self):
        self.assertIs(integrate.decide(candidate(), with_ci("completed", "success"), [], IGNORED), Decision.ELIGIBLE)

    def test_without_ignore_list_the_codex_gate_blocks(self):
        runs = with_ci("completed", "success")
        self.assertIs(integrate.decide(candidate(), runs, [], frozenset()), Decision.FAILING)

    def test_in_progress_check_is_pending(self):
        self.assertIs(integrate.decide(candidate(), with_ci("in_progress", None), [], IGNORED), Decision.PENDING)

    def test_neutral_and_skipped_count_as_passing(self):
        for conclusion in ("neutral", "skipped"):
            with self.subTest(conclusion=conclusion):
                runs = with_ci("completed", conclusion)
                self.assertIs(integrate.decide(candidate(), runs, [], IGNORED), Decision.ELIGIBLE)

    def test_other_conclusions_are_failing(self):
        for conclusion in ("cancelled", "timed_out", "action_required", "stale", "startup_failure"):
            with self.subTest(conclusion=conclusion):
                runs = with_ci("completed", conclusion)
                self.assertIs(integrate.decide(candidate(), runs, [], IGNORED), Decision.FAILING)

    def test_failure_outranks_pending(self):
        runs = with_ci("queued", None) + [{"name": "lint", "status": "completed", "conclusion": "failure"}]
        self.assertIs(integrate.decide(candidate(), runs, [], IGNORED), Decision.FAILING)

    def test_commit_statuses_count(self):
        passing = with_ci("completed", "success")
        cases = {"success": Decision.ELIGIBLE, "pending": Decision.PENDING,
                 "failure": Decision.FAILING, "error": Decision.FAILING}
        for state, expected in cases.items():
            with self.subTest(state=state):
                statuses = [{"context": "vercel", "state": state}]
                self.assertIs(integrate.decide(candidate(), passing, statuses, IGNORED), expected)

    def test_ignored_status_context_does_not_block(self):
        statuses = [{"context": "codex-cloud-review", "state": "failure"}]
        self.assertIs(integrate.decide(candidate(), with_ci("completed", "success"), statuses, IGNORED), Decision.ELIGIBLE)

    def test_empty_combined_status_does_not_block(self):
        statuses = fixture("status_empty.json")["statuses"]
        self.assertIs(integrate.decide(candidate(), with_ci("completed", "success"), statuses, IGNORED), Decision.ELIGIBLE)

    def test_head_with_no_checks_yet_is_pending(self):
        self.assertIs(integrate.decide(candidate(), [], [], IGNORED), Decision.PENDING)

    def test_draft_is_never_eligible(self):
        self.assertIs(integrate.decide(candidate(draft=True), with_ci("completed", "success"), [], IGNORED), Decision.DRAFT)


class IgnoreChecksTest(unittest.TestCase):
    def test_commas_newlines_and_blanks(self):
        parsed = integrate.parse_ignore_checks("codex-cloud-review, review / observe\n\n  lint ,")
        self.assertEqual(parsed, {"codex-cloud-review", "review / observe", "lint"})

    def test_empty_input_ignores_nothing(self):
        self.assertEqual(integrate.parse_ignore_checks(" \n"), frozenset())


class BranchNameTest(unittest.TestCase):
    def test_uses_utc_date_and_run_id(self):
        tokyo = datetime.timezone(datetime.timedelta(hours=9))
        now = datetime.datetime(2026, 11, 12, 8, 30, tzinfo=tokyo)
        self.assertEqual(integrate.branch_name("deps/integration-", now, "4242"), "deps/integration-20261111-4242")


def summary(*outcomes: Outcome) -> Summary:
    return Summary(REPO, "main", "a" * 40, "deps/integration-", "deps/integration-20261111-1", outcomes)


class BodyTest(unittest.TestCase):
    def test_lists_included_skipped_base_and_review_notice(self):
        s = summary(Outcome(candidate(512), Decision.ELIGIBLE), Outcome(candidate(513), Decision.CONFLICT),
                    Outcome(candidate(514), Decision.PENDING))
        body = integrate.render_body(s)
        self.assertIn(f"- [#512](https://github.com/{REPO}/pull/512) Bump package-512", body)
        self.assertIn(f"- [#513](https://github.com/{REPO}/pull/513) Bump package-513: conflict", body)
        self.assertIn("Bump package-514: pending", body)
        self.assertIn("a" * 40, body)
        self.assertIn("must be reviewed and merged by a maintainer", body)
        self.assertEqual(integrate.pr_title(s), "build(deps): integrate Dependabot updates (1 PR)")

    def test_no_skipped_section_when_everything_merged(self):
        s = summary(Outcome(candidate(1), Decision.ELIGIBLE), Outcome(candidate(2), Decision.ELIGIBLE))
        self.assertNotIn("Skipped", integrate.render_body(s))
        self.assertEqual(integrate.pr_title(s), "build(deps): integrate Dependabot updates (2 PRs)")

    def test_summary_round_trips_through_json(self):
        s = summary(Outcome(candidate(1), Decision.ELIGIBLE), Outcome(candidate(2, draft=True), Decision.DRAFT))
        self.assertEqual(Summary.from_json(s.to_json()), s)


class IdempotencyTest(unittest.TestCase):
    def test_open_integration_pr_means_nothing_is_built(self):
        github = FakeGitHub([pull(700, "b" * 40, author="kouariga", ref="deps/integration-20261110-9"),
                             pull(512, "c" * 40)])
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            git = integrate.Git(Path(tmp) / "not-a-repo")
            result = integrate.build(github, git, REPO, "deps/integration-", IGNORED, "1",
                                     datetime.datetime.now(datetime.timezone.utc), out)
            self.assertIsNone(result)
            self.assertFalse(out.exists())
        self.assertFalse(any("check-runs" in path for path in github.requested))


if __name__ == "__main__":
    unittest.main()

import copy
import dataclasses
import datetime
import json
import unittest
from pathlib import Path
from unittest import mock

from support import REPO, fixture, pull

import integrate
from integrate import Candidate, Commit, Decision, Mode, OpenPull, Outcome, Plan, Summary

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
    return Summary(REPO, "main", "a" * 40, "deps/integration-", "deps/integration-20261111-1", outcomes, Plan(Mode.CREATE))


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
        updating = dataclasses.replace(s, plan=Plan(Mode.UPDATE, 700, "b" * 40))
        self.assertEqual(Summary.from_json(json.loads(json.dumps(updating.to_json()))), updating)

    def test_body_records_the_included_set(self):
        s = summary(Outcome(candidate(2), Decision.ELIGIBLE), Outcome(candidate(3), Decision.CONFLICT))
        self.assertEqual(integrate.parse_marker(integrate.render_body(s)), ((2, SHA),))

    def test_notice_lists_waiting_updates_with_a_set_marker(self):
        s = summary(Outcome(candidate(2), Decision.ELIGIBLE))
        notice = integrate.render_notice(s)
        self.assertIn("Newer Dependabot updates are waiting", notice)
        self.assertIn(f"- [#2](https://github.com/{REPO}/pull/2) Bump package-2", notice)
        self.assertIn(integrate.notice_marker(((2, SHA),)), notice)
        self.assertNotEqual(integrate.notice_marker(((2, SHA),)), integrate.notice_marker(((2, "2" * 40),)))


class TokenTest(unittest.TestCase):
    def test_publish_with_empty_token_fails_with_a_clear_message(self):
        env = {"GITHUB_REPOSITORY": REPO, "GH_TOKEN": ""}
        with mock.patch.dict("os.environ", env), self.assertRaisesRegex(SystemExit, "publish token is empty"):
            integrate.main(["publish", "--artifact-dir", "unused"])


OTHER = "2222222222222222222222222222222222222222"


class MarkerTest(unittest.TestCase):
    def test_round_trip_sorts_the_set(self):
        marker = integrate.render_marker(((12, OTHER), (10, SHA)))
        self.assertEqual(integrate.parse_marker(f"Body\r\n\r\n{marker}\r\n"), ((10, SHA), (12, OTHER)))

    def test_empty_set_round_trips(self):
        self.assertEqual(integrate.parse_marker(integrate.render_marker(())), ())

    def test_rejects_anything_but_one_well_formed_marker(self):
        good = integrate.render_marker(((10, SHA),))
        bad = {
            "missing": "Included Dependabot PRs:",
            "twice": good + "\n" + good,
            "not json": "<!-- dependency-integration: {nope} -->",
            "wrong version": '<!-- dependency-integration: {"version":2,"included":[]} -->',
            "extra key": '<!-- dependency-integration: {"version":1,"included":[],"base":"x"} -->',
            "short sha": '<!-- dependency-integration: {"version":1,"included":[[10,"abc"]]} -->',
            "string number": f'<!-- dependency-integration: {{"version":1,"included":[["10","{SHA}"]]}} -->',
            "bool number": f'<!-- dependency-integration: {{"version":1,"included":[[true,"{SHA}"]]}} -->',
            "triple": f'<!-- dependency-integration: {{"version":1,"included":[[10,"{SHA}",1]]}} -->',
            "duplicate PR": f'<!-- dependency-integration: {{"version":1,"included":[[10,"{SHA}"],[10,"{OTHER}"]]}} -->',
        }
        for name, body in bad.items():
            with self.subTest(name):
                self.assertIsNone(integrate.parse_marker(body))


class IncludedSetTest(unittest.TestCase):
    def test_order_does_not_matter(self):
        a, b = Candidate(1, "a", SHA, "u"), Candidate(2, "b", OTHER, "u")
        self.assertEqual(integrate.included_set([b, a]), integrate.included_set([a, b]))

    def test_new_head_for_the_same_pr_differs(self):
        self.assertNotEqual(integrate.included_set([Candidate(1, "a", SHA, "u")]),
                            integrate.included_set([Candidate(1, "a", OTHER, "u")]))


def merge(author: tuple[str, str], *, second_parent_on_default: bool = False, parents: int = 2) -> Commit:
    return Commit("c" * 40, tuple(str(i) * 40 for i in range(parents)), *author, second_parent_on_default)


BOT = (integrate.BOT_NAME, integrate.BOT_EMAIL)
HUMAN = ("Maintainer", "maintainer@example.com")


class ProvenanceTest(unittest.TestCase):
    def test_bot_merge_is_automation(self):
        self.assertTrue(integrate.made_by_automation(merge(BOT)))

    def test_update_branch_merge_is_automation(self):
        self.assertTrue(integrate.made_by_automation(merge(HUMAN, second_parent_on_default=True)))

    def test_human_merge_of_another_branch_is_not(self):
        self.assertFalse(integrate.made_by_automation(merge(HUMAN)))

    def test_bot_name_with_another_email_is_not(self):
        self.assertFalse(integrate.made_by_automation(merge((integrate.BOT_NAME, "someone@example.com"))))

    def test_non_merge_commit_is_not_even_from_the_bot(self):
        self.assertFalse(integrate.made_by_automation(merge(BOT, parents=1)))
        self.assertFalse(integrate.made_by_automation(merge(HUMAN, second_parent_on_default=True, parents=1)))


def open_pull(recorded=((10, SHA),)) -> OpenPull:
    return OpenPull(700, "deps/integration-20261110-9", "b" * 40, recorded)


class PlanTest(unittest.TestCase):
    def test_no_open_pr_creates(self):
        self.assertEqual(integrate.choose_plan(None, ((10, SHA),), False), Plan(Mode.CREATE))

    def test_no_open_pr_and_nothing_merged_does_nothing(self):
        self.assertIsInstance(integrate.choose_plan(None, (), False), str)

    def test_unchanged_set_does_nothing(self):
        self.assertIsInstance(integrate.choose_plan(open_pull(), ((10, SHA),), True), str)
        self.assertIsInstance(integrate.choose_plan(open_pull(), ((10, SHA),), False), str)

    def test_changed_set_updates_the_open_pr_at_the_observed_head(self):
        plan = integrate.choose_plan(open_pull(), ((10, SHA), (11, OTHER)), True)
        self.assertEqual(plan, Plan(Mode.UPDATE, 700, "b" * 40))

    def test_human_modified_branch_notifies(self):
        plan = integrate.choose_plan(open_pull(), ((10, OTHER),), False)
        self.assertEqual(plan, Plan(Mode.NOTIFY, 700, "b" * 40))

    def test_missing_marker_counts_as_human_modified(self):
        plan = integrate.choose_plan(open_pull(recorded=None), ((10, SHA),), True)
        self.assertEqual(plan, Plan(Mode.NOTIFY, 700, "b" * 40))

    def test_empty_eligible_set_leaves_the_open_pr_alone(self):
        self.assertIsInstance(integrate.choose_plan(open_pull(), (), True), str)

    def test_two_open_prs_fail(self):
        with self.assertRaisesRegex(SystemExit, "2 open integration PRs \\(#700, #701\\)"):
            integrate.sole_open_pull([open_pull(), dataclasses.replace(open_pull(), number=701)])
        self.assertIsNone(integrate.sole_open_pull([]))

    def test_plan_targets_exactly_when_it_is_not_create(self):
        for bad in (lambda: Plan(Mode.CREATE, 1, SHA), lambda: Plan(Mode.UPDATE), lambda: Plan(Mode.NOTIFY, 1)):
            with self.assertRaises(ValueError):
                bad()

    def test_open_pull_reads_the_body_marker(self):
        body = integrate.render_marker(((10, SHA),))
        parsed = OpenPull.from_pull(pull(700, "b" * 40, ref="deps/integration-1", body=body))
        self.assertEqual(parsed.recorded, ((10, SHA),))
        self.assertIsNone(OpenPull.from_pull(pull(700, "b" * 40, ref="deps/integration-1")).recorded)


if __name__ == "__main__":
    unittest.main()

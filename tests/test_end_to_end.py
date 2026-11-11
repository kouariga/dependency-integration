import datetime
import json
import os
import subprocess
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from support import REPO, FakeGitHub, RecordingPulls, pull

import integrate

IDENTITY = {
    "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.com",
    "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
}


def git(cwd: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, env=os.environ | IDENTITY,
                            check=True, text=True, capture_output=True, input="")
    return result.stdout.strip()


def commit(work: Path, branch: str, start: str, path: str, content: str) -> str:
    git(work, "switch", "--quiet", "--force-create", branch, start)
    (work / path).write_text(content)
    git(work, "add", path)
    git(work, "commit", "--quiet", "-m", f"{branch}: {path}")
    return git(work, "rev-parse", "HEAD")


class EndToEndTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        isolated = mock.patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"})
        isolated.start()
        self.addCleanup(isolated.stop)

    def make_origin(self) -> tuple[Path, str, dict[str, str]]:
        origin = self.tmp / "origin.git"
        git(self.tmp, "init", "--quiet", "--bare", "--initial-branch=main", str(origin))
        # GitHub serves any reachable commit by SHA; a local bare repo must opt in.
        git(origin, "config", "uploadpack.allowReachableSHA1InWant", "true")
        seed = self.tmp / "seed"
        git(self.tmp, "init", "--quiet", "--initial-branch=main", str(seed))
        (seed / "versions.txt").write_text("left-pad 1.0.0\n")
        git(seed, "add", ".")
        git(seed, "commit", "--quiet", "-m", "initial")
        base = git(seed, "rev-parse", "HEAD")
        heads = {
            "clean-a": commit(seed, "clean-a", base, "versions.txt", "left-pad 1.1.0\n"),
            "clean-b": commit(seed, "clean-b", base, "lockfile.txt", "is-even 2.0.0\n"),
            "conflict": commit(seed, "conflict", base, "versions.txt", "left-pad 2.0.0\n"),
        }
        git(seed, "push", "--quiet", str(origin), "main", *heads)
        return origin, base, heads

    def test_build_bundles_clean_merges_and_publish_pushes_them(self):
        origin, base, heads = self.make_origin()
        checkout = self.tmp / "checkout"
        git(self.tmp, "clone", "--quiet", str(origin), str(checkout))
        github = FakeGitHub([pull(10, heads["clean-a"]), pull(11, heads["conflict"]), pull(12, heads["clean-b"])])
        out = self.tmp / "artifact"

        summary = integrate.build(github, integrate.Git(checkout), REPO, "deps/integration-",
                                  frozenset({"codex-cloud-review"}), "777",
                                  datetime.datetime(2026, 11, 11, tzinfo=datetime.timezone.utc), out)

        self.assertIsNotNone(summary)
        written = json.loads((out / integrate.SUMMARY).read_text())
        self.assertEqual(written["base_sha"], base)
        self.assertEqual(written["branch"], "deps/integration-20261111-777")
        self.assertEqual([p["number"] for p in written["included"]], [10, 12])
        self.assertEqual([(p["number"], p["reason"]) for p in written["skipped"]], [(11, "conflict")])

        reader = self.tmp / "reader"
        git(self.tmp, "clone", "--quiet", str(origin), str(reader))
        ref = "refs/heads/" + written["branch"]
        git(reader, "fetch", "--quiet", str(out / integrate.BUNDLE), f"{ref}:{ref}")
        merges = git(reader, "rev-list", "--merges", f"{base}..{ref}").split()
        self.assertEqual(len(merges), 2)
        merged_heads = {git(reader, "rev-parse", f"{m}^2") for m in merges}
        self.assertEqual(merged_heads, {heads["clean-a"], heads["clean-b"]})
        self.assertEqual(git(reader, "rev-list", "--count", f"{base}..{ref}"), "4")
        self.assertEqual(git(reader, "log", "-1", "--format=%an <%ae>", ref),
                         f"{integrate.BOT_NAME} <{integrate.BOT_EMAIL}>")

        remote = self.tmp / "publish-remote.git"
        git(self.tmp, "init", "--quiet", "--bare", "--initial-branch=main", str(remote))
        git(origin, "push", "--quiet", str(remote), "main")
        workdir = self.tmp / "publish-work"
        workdir.mkdir()
        pulls = RecordingPulls()
        integrate.publish(FakeGitHub([pull(12, heads["clean-b"])]), integrate.Git(workdir), out, str(remote), pulls)

        self.assertEqual(git(remote, "rev-parse", ref), git(reader, "rev-parse", ref))
        self.assertEqual([(call, s.branch) for call, s in pulls.calls], [("create", written["branch"])])
        self.assertEqual(list(workdir.glob("*.txt")), [], "publish never materializes repository files")

    def test_publish_refuses_when_base_left_the_default_branch(self):
        origin, base, heads = self.make_origin()
        checkout = self.tmp / "checkout"
        git(self.tmp, "clone", "--quiet", str(origin), str(checkout))
        out = self.tmp / "artifact"
        integrate.build(FakeGitHub([pull(10, heads["clean-a"])]), integrate.Git(checkout), REPO,
                        "deps/integration-", frozenset(), "1", datetime.datetime.now(datetime.timezone.utc), out)

        remote = self.tmp / "rewritten.git"
        git(self.tmp, "init", "--quiet", "--bare", "--initial-branch=main", str(remote))
        empty_tree = git(remote, "mktree")
        unrelated = git(remote, "commit-tree", empty_tree, "-m", "force-pushed history")
        git(remote, "update-ref", "refs/heads/main", unrelated)
        workdir = self.tmp / "publish-work"
        workdir.mkdir()
        with self.assertRaisesRegex(SystemExit, "no longer an ancestor"):
            integrate.publish(FakeGitHub([]), integrate.Git(workdir), out, str(remote), RecordingPulls())

    def test_publish_stops_when_an_integration_pr_opened_meanwhile(self):
        origin, base, heads = self.make_origin()
        checkout = self.tmp / "checkout"
        git(self.tmp, "clone", "--quiet", str(origin), str(checkout))
        out = self.tmp / "artifact"
        integrate.build(FakeGitHub([pull(10, heads["clean-a"])]), integrate.Git(checkout), REPO,
                        "deps/integration-", frozenset(), "1", datetime.datetime.now(datetime.timezone.utc), out)

        workdir = self.tmp / "publish-work"
        workdir.mkdir()
        other = FakeGitHub([pull(99, "f" * 40, author="kouariga", ref="deps/integration-20261111-0")])
        pulls = RecordingPulls()
        integrate.publish(other, integrate.Git(workdir), out, str(origin), pulls)
        self.assertEqual(git(origin, "branch", "--list", "deps/*"), "")
        self.assertEqual(pulls.calls, [])


class OpenIntegrationPrTest(unittest.TestCase):
    """A first build and publish open the integration PR; later builds act on it."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        isolated = mock.patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"})
        isolated.start()
        self.addCleanup(isolated.stop)
        self.origin, self.base, self.heads = EndToEndTest.make_origin(self)
        self.work = self.tmp / "work"
        git(self.tmp, "clone", "--quiet", str(self.origin), str(self.work))
        self.dependabot = [pull(10, self.heads["clean-a"]), pull(12, self.heads["clean-b"])]
        self.builds = 0
        first, out = self.build([])
        self.publish([], out)
        self.branch = first.branch
        self.ref = f"refs/heads/{first.branch}"
        self.first_head = git(self.origin, "rev-parse", self.ref)
        self.first_body = integrate.render_body(first)

    def build(self, integration: list[dict], comments: dict | None = None):
        self.builds += 1
        checkout = self.tmp / f"checkout-{self.builds}"
        git(self.tmp, "clone", "--quiet", str(self.origin), str(checkout))
        out = self.tmp / f"artifact-{self.builds}"
        github = FakeGitHub(integration + self.dependabot, comments=comments)
        summary = integrate.build(github, integrate.Git(checkout), REPO, "deps/integration-", frozenset(), "777",
                                  datetime.datetime(2026, 11, 11, tzinfo=datetime.timezone.utc), out)
        return summary, out

    def publish(self, integration: list[dict], out: Path, comments: dict | None = None) -> RecordingPulls:
        workdir = Path(tempfile.mkdtemp(dir=self.tmp, prefix="publish-"))
        pulls = RecordingPulls()
        integrate.publish(FakeGitHub(integration, comments=comments), integrate.Git(workdir), out, str(self.origin), pulls)
        return pulls

    def integration_pull(self, head: str | None = None) -> dict:
        return pull(700, head or git(self.origin, "rev-parse", self.ref), author="kouariga",
                    ref=self.branch, body=self.first_body)

    def push(self, branch: str, path: str, content: str) -> str:
        git(self.work, "fetch", "--quiet", "origin")
        head = commit(self.work, "scratch", f"origin/{branch}", path, content)
        git(self.work, "push", "--quiet", "origin", f"HEAD:refs/heads/{branch}")
        return head

    def add_dependabot(self, number: int, path: str) -> str:
        git(self.work, "fetch", "--quiet", "origin")
        head = commit(self.work, f"dependabot-{number}", self.base, path, f"update {number}\n")
        git(self.work, "push", "--quiet", "origin", f"HEAD:refs/heads/dependabot-{number}")
        self.dependabot.append(pull(number, head))
        return head

    def test_moved_default_branch_alone_does_not_rebuild(self):
        self.push("main", "readme.txt", "docs\n")
        summary, out = self.build([self.integration_pull()])
        self.assertIsNone(summary)
        self.assertFalse(out.exists())

    def test_new_update_rebuilds_the_same_branch_on_the_new_default_tip(self):
        new_tip = self.push("main", "readme.txt", "docs\n")
        added = self.add_dependabot(13, "extra.txt")
        summary, out = self.build([self.integration_pull()])

        self.assertEqual(summary.plan, integrate.Plan(integrate.Mode.UPDATE, 700, self.first_head))
        self.assertEqual(summary.branch, self.branch)
        pulls = self.publish([self.integration_pull()], out)

        head = git(self.origin, "rev-parse", self.ref)
        self.assertEqual([call for call, _ in pulls.calls], ["edit"])
        self.assertEqual(integrate.parse_marker(integrate.render_body(pulls.calls[0][1])),
                         ((10, self.heads["clean-a"]), (12, self.heads["clean-b"]), (13, added)))
        self.assertEqual(git(self.origin, "rev-list", "--first-parent", "-1", "--skip=3", self.ref), new_tip)
        merged = {git(self.origin, "rev-parse", f"{m}^2")
                  for m in git(self.origin, "rev-list", "--first-parent", f"{new_tip}..{self.ref}").split()}
        self.assertEqual(merged, {self.heads["clean-a"], self.heads["clean-b"], added})
        moved = subprocess.run(["git", "merge-base", "--is-ancestor", self.first_head, head], cwd=self.origin)
        self.assertNotEqual(moved.returncode, 0, "the branch was replaced, not extended")

    def test_update_branch_merge_does_not_block_a_rebuild(self):
        self.push("main", "readme.txt", "docs\n")
        git(self.work, "fetch", "--quiet", "origin")
        git(self.work, "switch", "--quiet", "--force-create", "scratch", f"origin/{self.branch}")
        git(self.work, "merge", "--quiet", "--no-ff", "--no-edit", "origin/main")
        git(self.work, "push", "--quiet", "origin", f"HEAD:{self.ref}")
        self.add_dependabot(13, "extra.txt")
        summary, _ = self.build([self.integration_pull()])
        self.assertIs(summary.plan.mode, integrate.Mode.UPDATE)

    def test_lease_refuses_when_the_branch_moved_after_build(self):
        self.add_dependabot(13, "extra.txt")
        summary, out = self.build([self.integration_pull()])
        self.assertIs(summary.plan.mode, integrate.Mode.UPDATE)
        moved = self.push(self.branch, "late.txt", "late\n")

        with self.assertRaisesRegex(SystemExit, "rejected"):
            self.publish([self.integration_pull(moved)], out)
        self.assertEqual(git(self.origin, "rev-parse", self.ref), moved)

    def test_human_commit_yields_one_notice_and_no_push(self):
        human = self.push(self.branch, "fix.txt", "manual fix\n")
        self.add_dependabot(13, "extra.txt")
        summary, out = self.build([self.integration_pull()])

        self.assertEqual(summary.plan, integrate.Plan(integrate.Mode.NOTIFY, 700, human))
        self.assertFalse((out / integrate.BUNDLE).exists())
        pulls = self.publish([self.integration_pull()], out)
        self.assertEqual([call for call, _ in pulls.calls], ["comment"])
        self.assertEqual(git(self.origin, "rev-parse", self.ref), human)

        posted = {700: [{"body": integrate.render_notice(summary)}]}
        self.assertEqual(self.publish([self.integration_pull()], out, posted).calls, [])
        self.assertIsNone(self.build([self.integration_pull()], posted)[0])


if __name__ == "__main__":
    unittest.main()

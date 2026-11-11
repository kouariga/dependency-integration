import datetime
import json
import os
import subprocess
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from support import REPO, FakeGitHub, pull

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
    git(work, "switch", "--quiet", "--create", branch, start)
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
        created = []
        integrate.publish(FakeGitHub([pull(12, heads["clean-b"])]), integrate.Git(workdir), out, str(remote), created.append)

        self.assertEqual(git(remote, "rev-parse", ref), git(reader, "rev-parse", ref))
        self.assertEqual([s.branch for s in created], [written["branch"]])
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
            integrate.publish(FakeGitHub([]), integrate.Git(workdir), out, str(remote), self.fail)

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
        integrate.publish(other, integrate.Git(workdir), out, str(origin), self.fail)
        self.assertEqual(git(origin, "branch", "--list", "deps/*"), "")


if __name__ == "__main__":
    unittest.main()

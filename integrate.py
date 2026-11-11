#!/usr/bin/env python3
"""Assemble passing Dependabot PRs into one maintainer-reviewed integration PR."""

from __future__ import annotations

import argparse
import base64
import dataclasses
import datetime
import enum
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path
from typing import Iterable, Protocol

DEPENDABOT = "dependabot[bot]"
BOT_NAME = "github-actions[bot]"
BOT_EMAIL = "41898282+github-actions[bot]@users.noreply.github.com"
BUNDLE = "integration.bundle"
SUMMARY = "summary.json"


class Decision(enum.Enum):
    ELIGIBLE = "eligible"
    PENDING = "pending"
    FAILING = "failing"
    CONFLICT = "conflict"
    DRAFT = "draft"


class Verdict(enum.IntEnum):
    OK = 0
    PENDING = 1
    FAILING = 2


CHECK_CONCLUSIONS = {"success": Verdict.OK, "neutral": Verdict.OK, "skipped": Verdict.OK}
STATUS_STATES = {"success": Verdict.OK, "pending": Verdict.PENDING}
AGGREGATE = {Verdict.OK: Decision.ELIGIBLE, Verdict.PENDING: Decision.PENDING, Verdict.FAILING: Decision.FAILING}


@dataclasses.dataclass(frozen=True)
class Candidate:
    number: int
    title: str
    head_sha: str
    url: str
    draft: bool = False

    @classmethod
    def from_pull(cls, pull: dict) -> Candidate:
        return cls(pull["number"], pull["title"], pull["head"]["sha"], pull["html_url"], pull.get("draft", False))


@dataclasses.dataclass(frozen=True)
class Outcome:
    candidate: Candidate
    decision: Decision


IncludedSet = tuple[tuple[int, str], ...]


def included_set(candidates: Iterable[Candidate]) -> IncludedSet:
    return tuple(sorted((c.number, c.head_sha) for c in candidates))


class Mode(enum.Enum):
    CREATE = "create"
    UPDATE = "update"
    NOTIFY = "notify"


@dataclasses.dataclass(frozen=True)
class Plan:
    """What publish must do. UPDATE and NOTIFY target the open integration PR at the head build observed."""

    mode: Mode
    number: int | None = None
    head_sha: str | None = None

    def __post_init__(self):
        targeted = self.number is not None and self.head_sha is not None
        untargeted = self.number is None and self.head_sha is None
        if not (untargeted if self.mode is Mode.CREATE else targeted):
            raise ValueError(f"{self.mode.value} plan with number={self.number} head_sha={self.head_sha}")

    def to_json(self) -> dict:
        return {"mode": self.mode.value, "number": self.number, "head_sha": self.head_sha}

    @classmethod
    def from_json(cls, data: dict) -> Plan:
        return cls(Mode(data["mode"]), data["number"], data["head_sha"])


@dataclasses.dataclass(frozen=True)
class OpenPull:
    number: int
    ref: str
    head_sha: str
    recorded: IncludedSet | None

    @classmethod
    def from_pull(cls, pull: dict) -> OpenPull:
        return cls(pull["number"], pull["head"]["ref"], pull["head"]["sha"], parse_marker(pull.get("body") or ""))


@dataclasses.dataclass(frozen=True)
class Commit:
    sha: str
    parents: tuple[str, ...]
    author_name: str
    author_email: str
    second_parent_on_default: bool


def made_by_automation(commit: Commit) -> bool:
    if len(commit.parents) != 2:
        return False
    return (commit.author_name, commit.author_email) == (BOT_NAME, BOT_EMAIL) or commit.second_parent_on_default


def sole_open_pull(pulls: list[OpenPull]) -> OpenPull | None:
    if len(pulls) > 1:
        numbers = ", ".join(f"#{p.number}" for p in pulls)
        raise SystemExit(f"Found {len(pulls)} open integration PRs ({numbers}); close all but one.")
    return pulls[0] if pulls else None


def choose_plan(pull: OpenPull | None, included: IncludedSet, automation_only: bool) -> Plan | str:
    """Returns the plan, or the reason publish has nothing to do."""
    if pull is None:
        return Plan(Mode.CREATE) if included else "No Dependabot PR merged cleanly; nothing to publish."
    if not included:
        return f"No Dependabot PR is eligible; leaving integration PR #{pull.number} as it is."
    if included == pull.recorded:
        return f"Integration PR #{pull.number} already contains the eligible set; nothing to publish."
    if pull.recorded is None or not automation_only:
        return Plan(Mode.NOTIFY, pull.number, pull.head_sha)
    return Plan(Mode.UPDATE, pull.number, pull.head_sha)


MARKER = re.compile(r"<!-- dependency-integration: (\{.*?\}) -->")
SHA_FORMAT = re.compile(r"[0-9a-f]{40}")


def render_marker(included: IncludedSet) -> str:
    payload = {"version": 1, "included": [list(item) for item in included]}
    return f"<!-- dependency-integration: {json.dumps(payload, separators=(',', ':'))} -->"


def parse_marker(body: str) -> IncludedSet | None:
    found = MARKER.findall(body)
    if len(found) != 1:
        return None
    try:
        data = json.loads(found[0])
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or data.keys() != {"version", "included"} or data["version"] != 1:
        return None
    items = data["included"]
    if not isinstance(items, list) or not all(
            isinstance(i, list) and len(i) == 2 and type(i[0]) is int and isinstance(i[1], str)
            and SHA_FORMAT.fullmatch(i[1]) for i in items):
        return None
    parsed = tuple(sorted((n, sha) for n, sha in items))
    return parsed if len({n for n, _ in parsed}) == len(parsed) else None


def fingerprint(included: IncludedSet) -> str:
    return hashlib.sha256(json.dumps(included).encode()).hexdigest()[:16]


def notice_marker(included: IncludedSet) -> str:
    return f"<!-- dependency-integration-notice: {fingerprint(included)} -->"


@dataclasses.dataclass(frozen=True)
class Summary:
    repository: str
    default_branch: str
    base_sha: str
    branch_prefix: str
    branch: str
    outcomes: tuple[Outcome, ...]
    plan: Plan

    @property
    def included(self) -> list[Candidate]:
        return [o.candidate for o in self.outcomes if o.decision is Decision.ELIGIBLE]

    @property
    def skipped(self) -> list[Outcome]:
        return [o for o in self.outcomes if o.decision is not Decision.ELIGIBLE]

    def to_json(self) -> dict:
        return {
            "repository": self.repository,
            "default_branch": self.default_branch,
            "base_sha": self.base_sha,
            "branch_prefix": self.branch_prefix,
            "branch": self.branch,
            "included": [dataclasses.asdict(c) for c in self.included],
            "skipped": [dataclasses.asdict(o.candidate) | {"reason": o.decision.value} for o in self.skipped],
            "plan": self.plan.to_json(),
        }

    @classmethod
    def from_json(cls, data: dict) -> Summary:
        def candidate(item: dict) -> Candidate:
            return Candidate(item["number"], item["title"], item["head_sha"], item["url"], item.get("draft", False))

        outcomes = [Outcome(candidate(i), Decision.ELIGIBLE) for i in data["included"]]
        outcomes += [Outcome(candidate(s), Decision(s["reason"])) for s in data["skipped"]]
        return cls(data["repository"], data["default_branch"], data["base_sha"],
                   data["branch_prefix"], data["branch"], tuple(outcomes), Plan.from_json(data["plan"]))


def parse_ignore_checks(raw: str) -> frozenset[str]:
    return frozenset(name.strip() for name in re.split(r"[,\n]", raw) if name.strip())


def check_run_verdict(run: dict) -> Verdict:
    if run["status"] != "completed":
        return Verdict.PENDING
    return CHECK_CONCLUSIONS.get(run["conclusion"], Verdict.FAILING)


def status_verdict(status: dict) -> Verdict:
    return STATUS_STATES.get(status["state"], Verdict.FAILING)


def decide(candidate: Candidate, check_runs: Iterable[dict], statuses: Iterable[dict], ignored: frozenset[str]) -> Decision:
    if candidate.draft:
        return Decision.DRAFT
    verdicts = [check_run_verdict(r) for r in check_runs if r["name"] not in ignored]
    verdicts += [status_verdict(s) for s in statuses if s["context"] not in ignored]
    # A head with no reported checks yet is indistinguishable from one whose CI has not started.
    return AGGREGATE[max(verdicts, default=Verdict.PENDING)]


def branch_name(prefix: str, now: datetime.datetime, run_id: str) -> str:
    return f"{prefix}{now.astimezone(datetime.timezone.utc):%Y%m%d}-{run_id}"


def pr_title(summary: Summary) -> str:
    count = len(summary.included)
    return f"build(deps): integrate Dependabot updates ({count} PR{'' if count == 1 else 's'})"


def render_body(summary: Summary) -> str:
    lines = ["Included Dependabot PRs:", ""]
    lines += [f"- [#{c.number}]({c.url}) {c.title} (`{c.head_sha[:12]}`)" for c in summary.included]
    if summary.skipped:
        lines += ["", "Skipped Dependabot PRs:", ""]
        lines += [f"- [#{o.candidate.number}]({o.candidate.url}) {o.candidate.title}: {o.decision.value}" for o in summary.skipped]
    lines += [
        "",
        f"Base: `{summary.base_sha}` on `{summary.default_branch}`.",
        "",
        "This PR was assembled automatically and must be reviewed and merged by a maintainer.",
        "",
        render_marker(included_set(summary.included)),
    ]
    return "\n".join(lines) + "\n"


def render_notice(summary: Summary) -> str:
    lines = [
        "Newer Dependabot updates are waiting, but this branch has changes the automation did not make, "
        "so it was not rebuilt. Merge or close this PR to let the next run integrate them.",
        "",
    ]
    lines += [f"- [#{c.number}]({c.url}) {c.title} (`{c.head_sha[:12]}`)" for c in summary.included]
    lines += ["", notice_marker(included_set(summary.included))]
    return "\n".join(lines) + "\n"


class GitHub:
    def __init__(self, token: str, api_url: str = "https://api.github.com"):
        self._token = token
        self._api_url = api_url.rstrip("/")

    def _request(self, url: str) -> tuple[object, str]:
        request = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        })
        with urllib.request.urlopen(request) as response:
            return json.load(response), response.headers.get("Link", "")

    def get(self, path: str) -> dict:
        return self._request(f"{self._api_url}/{path}")[0]

    def paginate(self, path: str, key: str | None = None) -> list[dict]:
        url = f"{self._api_url}/{path}{'&' if '?' in path else '?'}per_page=100"
        items: list[dict] = []
        while url:
            page, link = self._request(url)
            items += page[key] if key else page
            match = re.search(r'<([^>]+)>;\s*rel="next"', link)
            url = match.group(1) if match else ""
        return items


def open_integration_prs(github: GitHub, repo: str, prefix: str) -> list[OpenPull]:
    pulls = github.paginate(f"repos/{repo}/pulls?state=open")
    return [OpenPull.from_pull(p) for p in pulls if p["head"]["ref"].startswith(prefix)]


def notice_posted(github: GitHub, repo: str, number: int, included: IncludedSet) -> bool:
    marker = notice_marker(included)
    return any(marker in (c.get("body") or "") for c in github.paginate(f"repos/{repo}/issues/{number}/comments"))


def dependabot_candidates(github: GitHub, repo: str, default_branch: str) -> list[Candidate]:
    pulls = github.paginate(f"repos/{repo}/pulls?state=open&base={default_branch}")
    return sorted((Candidate.from_pull(p) for p in pulls if p["user"]["login"] == DEPENDABOT), key=lambda c: c.number)


def evaluate(github: GitHub, repo: str, candidate: Candidate, ignored: frozenset[str]) -> Decision:
    sha = candidate.head_sha
    runs = github.paginate(f"repos/{repo}/commits/{sha}/check-runs?filter=latest", "check_runs")
    # The combined endpoint reports one latest status per context; its top-level state is "pending" when empty.
    statuses = github.paginate(f"repos/{repo}/commits/{sha}/status", "statuses")
    return decide(candidate, runs, statuses, ignored)


def auth_env(token: str) -> dict[str, str]:
    """Git config passed through the environment so the token never appears in argv or a URL."""
    if not token:
        return {}
    header = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::add-mask::{header}")
    return {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "http.extraheader", "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {header}"}


class Git:
    def __init__(self, cwd: Path, env: dict[str, str] | None = None):
        self.cwd = Path(cwd)
        self._hooks = tempfile.mkdtemp(prefix="no-hooks-")
        self._env = os.environ | {
            "GIT_AUTHOR_NAME": BOT_NAME, "GIT_AUTHOR_EMAIL": BOT_EMAIL,
            "GIT_COMMITTER_NAME": BOT_NAME, "GIT_COMMITTER_EMAIL": BOT_EMAIL,
            "GIT_TERMINAL_PROMPT": "0",
        } | (env or {})

    def run(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", "-c", f"core.hooksPath={self._hooks}", *args],
            cwd=self.cwd, env=self._env, check=check, text=True, capture_output=True, stdin=subprocess.DEVNULL,
        )

    def out(self, *args: str) -> str:
        return self.run(*args).stdout.strip()


def merge_candidates(git: Git, base_sha: str, branch: str, decided: list[Outcome]) -> list[Outcome]:
    git.run("switch", "--quiet", "--create", branch, base_sha)
    outcomes = []
    for outcome in decided:
        if outcome.decision is Decision.ELIGIBLE:
            git.run("fetch", "--quiet", "--no-tags", "origin", outcome.candidate.head_sha)
            merged = git.run("merge", "--no-ff", "--no-edit", outcome.candidate.head_sha, check=False)
            if merged.returncode != 0:
                git.run("merge", "--abort")
                outcome = Outcome(outcome.candidate, Decision.CONFLICT)
        outcomes.append(outcome)
    return outcomes


def branch_commits(git: Git, base_sha: str, head_sha: str) -> list[Commit]:
    """First-parent history of the PR branch, so the Dependabot commits brought in by bot merges are not listed."""
    git.run("fetch", "--quiet", "--no-tags", "origin", head_sha)
    log = git.out("log", "--first-parent", "--format=%H%x00%P%x00%an%x00%ae", f"{base_sha}..{head_sha}")
    commits = []
    for line in log.splitlines():
        sha, parents, name, email = line.split("\0")
        parents = tuple(parents.split())
        on_default = len(parents) == 2 and git.run(
            "merge-base", "--is-ancestor", parents[1], base_sha, check=False).returncode == 0
        commits.append(Commit(sha, parents, name, email, on_default))
    return commits


def build(github: GitHub, git: Git, repo: str, prefix: str, ignored: frozenset[str],
          run_id: str, now: datetime.datetime, out_dir: Path) -> Summary | None:
    pull = sole_open_pull(open_integration_prs(github, repo, prefix))
    default_branch = github.get(f"repos/{repo}")["default_branch"]
    base_sha = git.out("rev-parse", f"refs/remotes/origin/{default_branch}")
    decided = [Outcome(c, evaluate(github, repo, c, ignored)) for c in dependabot_candidates(github, repo, default_branch)]
    branch = pull.ref if pull else branch_name(prefix, now, run_id)
    outcomes = tuple(merge_candidates(git, base_sha, branch, decided))
    for outcome in outcomes:
        print(f"#{outcome.candidate.number} {outcome.decision.value}: {outcome.candidate.title}")
    included = included_set(o.candidate for o in outcomes if o.decision is Decision.ELIGIBLE)
    automation_only = pull is not None and all(made_by_automation(c) for c in branch_commits(git, base_sha, pull.head_sha))
    plan = choose_plan(pull, included, automation_only)
    if isinstance(plan, str):
        print(plan)
        return None
    if plan.mode is Mode.NOTIFY and notice_posted(github, repo, plan.number, included):
        print(f"Integration PR #{plan.number} has changes the automation did not make and was already told "
              "about these updates; nothing to publish.")
        return None
    print(f"Plan: {plan.mode.value}" + (f" integration PR #{plan.number}." if plan.number else " a new integration PR."))
    summary = Summary(repo, default_branch, base_sha, prefix, branch, outcomes, plan)
    out_dir.mkdir(parents=True, exist_ok=True)
    if plan.mode is not Mode.NOTIFY:
        git.run("bundle", "create", str(out_dir / BUNDLE), f"{base_sha}..refs/heads/{branch}")
    (out_dir / SUMMARY).write_text(json.dumps(summary.to_json(), indent=2) + "\n")
    return summary


def push_bundle(git: Git, bundle: Path, summary: Summary, remote: str) -> str:
    """Pushes the bundle head without ever checking out a working tree of the target repository."""
    git.run("init", "--quiet", "--bare")
    git.run("remote", "add", "origin", remote)
    tracking = f"refs/remotes/origin/{summary.default_branch}"
    git.run("fetch", "--quiet", "--no-tags", "origin", f"+refs/heads/{summary.default_branch}:{tracking}")
    if git.run("merge-base", "--is-ancestor", summary.base_sha, tracking, check=False).returncode != 0:
        raise SystemExit(f"Base {summary.base_sha} is no longer an ancestor of {summary.default_branch}; refusing to publish.")
    git.run("bundle", "verify", "--quiet", str(bundle))
    ref = f"refs/heads/{summary.branch}"
    git.run("fetch", "--quiet", str(bundle), f"{ref}:{ref}")
    lease = [f"--force-with-lease={ref}:{summary.plan.head_sha}"] if summary.plan.mode is Mode.UPDATE else []
    pushed = git.run("push", "--quiet", *lease, "origin", f"{ref}:{ref}", check=False)
    if pushed.returncode != 0:
        raise SystemExit(f"Push of {summary.branch} was rejected; it may have moved since build.\n{pushed.stderr}")
    return git.out("rev-parse", ref)


class PullRequests(Protocol):
    def create(self, summary: Summary) -> None: ...
    def edit(self, summary: Summary) -> None: ...
    def comment(self, summary: Summary) -> None: ...


def publish(github: GitHub, git: Git, artifact_dir: Path, remote: str, pulls: PullRequests) -> None:
    summary = Summary.from_json(json.loads((artifact_dir / SUMMARY).read_text()))
    plan = summary.plan
    open_numbers = [p.number for p in open_integration_prs(github, summary.repository, summary.branch_prefix)]
    if open_numbers != ([] if plan.mode is Mode.CREATE else [plan.number]):
        print(f"Open integration PRs changed since build (now {open_numbers or 'none'}); not publishing.")
        return
    if plan.mode is Mode.NOTIFY:
        if notice_posted(github, summary.repository, plan.number, included_set(summary.included)):
            print(f"Integration PR #{plan.number} was already told about these updates.")
        else:
            pulls.comment(summary)
        return
    head = push_bundle(git, artifact_dir / BUNDLE, summary, remote)
    print(f"Pushed {summary.branch} at {head}.")
    if plan.mode is Mode.CREATE:
        pulls.create(summary)
    else:
        pulls.edit(summary)


class GhPullRequests:
    def create(self, summary: Summary) -> None:
        self._run(["pr", "create", "--repo", summary.repository, "--base", summary.default_branch,
                   "--head", summary.branch, "--title", pr_title(summary)], render_body(summary))

    def edit(self, summary: Summary) -> None:
        self._run(["pr", "edit", str(summary.plan.number), "--repo", summary.repository,
                   "--title", pr_title(summary)], render_body(summary))

    def comment(self, summary: Summary) -> None:
        self._run(["pr", "comment", str(summary.plan.number), "--repo", summary.repository], render_notice(summary))

    @staticmethod
    def _run(args: list[str], body: str) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as file:
            file.write(body)
        subprocess.run(["gh", *args, "--body-file", file.name], check=True)


def write_output(name: str, value: str) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a") as out:
            out.write(f"{name}={value}\n")


def main(argv: list[str]) -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    build_cmd = sub.add_parser("build")
    build_cmd.add_argument("--branch-prefix", required=True)
    build_cmd.add_argument("--ignore-checks", default="")
    build_cmd.add_argument("--out-dir", type=Path, required=True)
    publish_cmd = sub.add_parser("publish")
    publish_cmd.add_argument("--artifact-dir", type=Path, required=True)
    args = parser.parse_args(argv)

    env = os.environ
    repo, server = env["GITHUB_REPOSITORY"], env.get("GITHUB_SERVER_URL", "https://github.com")
    api = env.get("GITHUB_API_URL", "https://api.github.com")
    token = env.get("GH_TOKEN", "")
    if args.command == "publish" and not token:
        raise SystemExit("The publish token is empty. Add DEPENDENCY_INTEGRATION_TOKEN to the "
                         "dependency-integration environment and pass it as the token input.")
    github = GitHub(token, api)

    if args.command == "build":
        git = Git(Path(env.get("GITHUB_WORKSPACE", ".")), auth_env(token))
        summary = build(github, git, repo, args.branch_prefix, parse_ignore_checks(args.ignore_checks),
                        env["GITHUB_RUN_ID"], datetime.datetime.now(datetime.timezone.utc), args.out_dir)
        write_output("changed", "true" if summary else "false")
    else:
        workdir = Path(tempfile.mkdtemp(prefix="publish-"))
        publish(github, Git(workdir, auth_env(token)), args.artifact_dir, f"{server}/{repo}.git", GhPullRequests())


if __name__ == "__main__":
    main(sys.argv[1:])

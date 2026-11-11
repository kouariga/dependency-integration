import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

FIXTURES = Path(__file__).resolve().parent / "fixtures"
REPO = "example/app"


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def pull(number: int, sha: str, *, author: str = "dependabot[bot]", ref: str | None = None, draft: bool = False) -> dict:
    return {
        "number": number,
        "title": f"Bump package-{number}",
        "html_url": f"https://github.com/{REPO}/pull/{number}",
        "draft": draft,
        "user": {"login": author},
        "head": {"sha": sha, "ref": ref or f"dependabot/npm_and_yarn/package-{number}"},
    }


def passing_checks(sha: str) -> dict:
    return {"total_count": 1, "check_runs": [
        {"name": "ci", "head_sha": sha, "status": "completed", "conclusion": "success"},
    ]}


class FakeGitHub:
    """Serves canned payloads by API path, ignoring query strings, like a read-only GitHub."""

    def __init__(self, pulls: list[dict], checks: dict[str, dict] | None = None,
                 statuses: dict[str, list[dict]] | None = None, default_branch: str = "main"):
        self.pulls = pulls
        self.checks = checks or {}
        self.statuses = statuses or {}
        self.default_branch = default_branch
        self.requested: list[str] = []

    def get(self, path: str) -> dict:
        self.requested.append(path)
        assert path == f"repos/{REPO}", path
        return {"default_branch": self.default_branch}

    def paginate(self, path: str, key: str | None = None) -> list[dict]:
        self.requested.append(path)
        route = path.split("?")[0]
        if route == f"repos/{REPO}/pulls":
            return self.pulls
        sha = route.split("/")[4]
        if route.endswith("/check-runs"):
            return self.checks.get(sha, passing_checks(sha))[key]
        if route.endswith("/status"):
            return self.statuses.get(sha, [])
        raise AssertionError(f"unexpected request {path}")

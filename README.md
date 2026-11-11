# dependency-integration

Some repositories require a check that Dependabot PRs can never pass, for example a review gate that accepts only maintainer-authored PRs. Dependency updates then have to land through a PR a maintainer opens. This repository automates that PR.

On a schedule, it collects every open Dependabot PR whose checks pass, merges them into one branch, and opens a single integration PR authored by the owner of a personal access token. The maintainer reviews and merges it. Nothing is merged automatically.

## What a run does

1. It looks for an open PR whose head branch starts with `deps/integration-`. If there is more than one, the run fails.
2. It lists open, Dependabot-authored PRs that target the default branch.
3. A PR is eligible when every check run and commit status on its head commit completed as `success`, `neutral`, or `skipped`. Checks named in `ignore-checks` (default `codex-cloud-review`) are not considered. A PR with a pending check, a failing check, or no checks reported yet is skipped. Drafts are skipped.
4. Starting from the default branch tip, it merges each eligible PR in ascending number order with `git merge --no-ff`. A PR that conflicts with the branch built so far is skipped.
5. If no integration PR is open and at least one PR merged, it pushes the branch `deps/integration-<UTC yyyymmdd>-<run id>` and opens `build(deps): integrate Dependabot updates (<n> PRs)`. The body lists the included PRs, the skipped PRs with their reasons, and the base commit. It also holds a hidden marker that records the included PRs and their head commits.
6. If an integration PR is open, the run follows [When an integration PR is open](#when-an-integration-pr-is-open).

The integration PR runs your normal CI, because it is pushed with a personal access token rather than `GITHUB_TOKEN`.

## When an integration PR is open

The run still builds the eligible set from the latest default branch tip, then compares it with the set recorded in the open PR's marker. A set is the included PR numbers with their head commits, so a Dependabot rebase counts as a change.

- If the set is unchanged, nothing happens. A default branch that moved on its own does not trigger a rebuild.
- If no PR is eligible any more, nothing happens. The open PR is not emptied or closed.
- If the set changed and the branch holds only automation commits, the branch is rebuilt from the default branch tip and force-pushed to the same PR. The push uses `--force-with-lease` against the head commit the build saw, so it is refused if the branch moved in between. The PR's title and body are replaced with the new ones. Edits made to them by hand are lost.
- If the set changed but someone else changed the branch, it is not rebuilt. The run posts one comment on the PR that lists the waiting updates. It posts again only when the waiting set changes. Merge or close the PR to let the next run integrate them.

Automation commits are merge commits authored by `github-actions[bot]`, and merge commits whose second parent is on the default branch, which is what GitHub's **Update branch** button makes. Only the first-parent history from the default branch tip to the PR head is checked, because the Dependabot commits merged by the bot are second parents. Any other commit, a non-merge commit by anyone included, means a person changed the branch. A PR body whose marker is missing or malformed is treated the same way.

Each rebuild pushes new commits, so the integration PR's checks run again, including a fresh Codex review where that is set up.

## Security model

The work is split into two jobs so that no job holds a secret while it touches PR content.

- **build** runs with only `GITHUB_TOKEN` and read permissions. It fetches the Dependabot heads and merges them, and fetches the open integration PR's head to inspect its commits, but executes nothing from them: no install, no build, no scripts, and git hooks are pointed at an empty directory. Merge commits are authored as `github-actions[bot]`. The result leaves the job as a `summary.json` and, when there is a branch to push, a git bundle in a workflow artifact. The summary names the plan for publish: `create`, `update`, or `notify`.
- **publish** runs in the `dependency-integration` environment, which holds the token. It never checks out the target repository and runs no code from it. It downloads the artifact, checks that the open integration PRs are the ones build saw, checks that the bundle is valid and that its base commit is still on the default branch, pushes the branch, and calls `gh pr create` or `gh pr edit`. For `notify` it only calls `gh pr comment`. The token reaches git through an environment-provided `http.extraheader` and reaches `gh` through `GH_TOKEN`. It never appears in a URL or command line.

Both jobs run only code from this repository, pinned by commit SHA in the caller.

## Setup per repository

1. Create a fine-grained personal access token. Limit it to the repositories that use this workflow. Grant repository permissions Contents, Pull requests, and Workflows, all read and write. Workflows is needed because Dependabot updates to `.github/workflows` files are pushed as part of the integration branch.
2. In each repository, create the environment `dependency-integration`. Under deployment branches, allow only the default branch.
3. Add the token to that environment as the secret `DEPENDENCY_INTEGRATION_TOKEN`. Do not add it as a repository secret.
4. Copy [`examples/caller.yml`](examples/caller.yml) to `.github/workflows/dependency-integration.yml`.
5. Replace both `<sha>` placeholders with the full commit SHA of the release of this repository you reviewed. Do not use a branch or tag.
6. Run the workflow once with **Run workflow** to check the setup.

Inputs of the build action:

| Input | Default | Meaning |
| --- | --- | --- |
| `branch-prefix` | `deps/integration-` | Head branch prefix of integration PRs. |
| `ignore-checks` | `codex-cloud-review` | Check names to ignore, separated by commas or newlines. |
| `artifact-name` | `dependency-integration` | Artifact passed from build to publish. Must match in both actions. |

## After the integration PR merges

Dependabot notices that each included update is now on the default branch and closes its own PR. PRs that were skipped stay open. Dependabot rebases them, and a later run picks them up once their checks pass.

## Limits

- Repository-specific steps are out of scope. If an update needs a regenerated lockfile, dependency hashes, or other generated files that Dependabot did not produce, fix that on the integration branch by hand or let the PR wait for Dependabot.
- Two updates that merge cleanly as text can still break together. The integration PR's own CI is the check for that.
- A repository with a required check that only runs locally still needs that check run against the integration branch before merge.
- Only one integration PR is open at a time. Updates that arrive while it waits for review are folded into it unless someone changed its branch.

## Development

```sh
python3 -m unittest discover -s tests -v
```

The tests include an end-to-end run of the git steps against local bare repositories, with the GitHub API stubbed.

## License

MIT

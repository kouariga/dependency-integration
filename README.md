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

The repository also provides a daily [maintenance report](#maintenance-report) that tracks workflow health, Dependabot, and deployed image vulnerabilities as issues.

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

## Maintenance report

A daily check that keeps one GitHub issue open for each maintenance problem it finds, and closes the issue once a complete check confirms the problem is gone. Two composite actions share the work.

- **report** reads the repository's own health: the latest completed run of each listed workflow, whether each listed scheduled workflow completed in the last 36 hours and how it ended, the latest result of each Dependabot update job in the last 8 days, and Dependabot PRs open for 7 days or more. It merges extra reports and earlier job results into that, then opens, updates, or closes issues.
- **scan-cloud-run** lists the image digest of every serving revision of every Cloud Run service and of every Cloud Run job in one project and region, scans each image with a pinned Trivy, and writes an extra report for **report** to read. With `inventory-file` it scans the images that file lists instead.

### Policy

Vulnerability findings never fail anything. They are reported: a HIGH or CRITICAL vulnerability with a fixed version is an `attention` finding, and one without a fixed version is a `warning` finding. Only infrastructure failures are `error` findings: a scanner or gcloud failure, history that cannot be read, a missing or malformed extra report, or an earlier job that did not succeed. The report step exits with status 1 when there is an `error` finding, so the failure shows in the Actions tab. It never fails because of `attention` or `warning` findings.

An issue is closed only when the report is complete. A report is complete when `required_reports` is not empty and the report has no `error` finding and no `deployed:unscanned` finding. A workflow, scheduled outcome, or job finding closes only once a later result for it was actually observed. A failed Dependabot update job closes once a later run succeeds or once no run of it is left in the 8-day window, which is what happens when its alert is dismissed and Dependabot stops retrying; and a stale PR finding closes only once the PR is closed. Before closing, the run posts one comment with its evidence. A retry of a failed close does not post it again.

### Report contract

Every extra report is a JSON object with a `findings` list. Each finding has four string fields.

```json
{"findings": [{"key": "upstream:ffmpeg", "level": "attention", "summary": "FFmpeg 8.1 is released.", "url": "https://example.com/release"}]}
```

- `key` identifies the problem across runs. One open issue exists per key. Keep it stable and free of values that change on every run, such as digests or run IDs.
- `level` is `error`, `attention`, or `warning`. Use `error` only when the check itself could not run.
- `summary` is the issue title after the prefix and the line in the issue body. A change of summary for the same key updates the issue in place.
- `url` links to evidence, or is empty.

A report file that is missing, unreadable, or not in this shape becomes an `error` finding keyed `read:extra:<file name>`.

**scan-cloud-run** writes `report.json` in this shape into `output-dir`, next to `inventory.json` and one directory per image holding the raw Trivy JSON and a CycloneDX SBOM. Its finding keys are `deployed:fix-available:<resource>` and `deployed:no-fix:<resource>`, where the resource is `service/<name>` or `job/<name>`. There is one finding per resource and group, and its summary lists the CVE IDs, so a redeploy with a new digest updates the same issue. A failed scan or an inventory that changed during the scan is the `error` finding `deployed:unavailable`. With `inventory-file`, a missing or invalid file is the same `error` finding. The scanner runs without the Docker socket: the image is saved with `docker save` and mounted read-only into the Trivy container, which reads it with `--input`.

### Setup per repository

1. Add `.github/maintenance.json`. See [`examples/maintenance.json`](examples/maintenance.json). `workflows` lists workflow files whose latest completed run on the default branch must succeed. `scheduled_workflows` lists workflow files that must complete a scheduled run every 36 hours. `required_reports` lists the file names of extra reports every run must provide. `repository` is optional and defaults to the current repository.
2. Copy [`examples/maintenance-caller.yml`](examples/maintenance-caller.yml) to `.github/workflows/dependency-maintenance.yml` and replace each `<sha>` with the full commit SHA of the release of this repository you reviewed.
3. Set the repository variables the example reads: the Workload Identity provider, a read-only service account, the Google Cloud project, and the region.
4. Run the workflow once with **Run workflow** to check the setup.

GCP authentication stays in the caller. The caller runs `google-github-actions/auth` and `setup-gcloud` before **scan-cloud-run**, and the Workload Identity provider's attribute condition, which limits the token to your repository and default branch, is configured on the Google Cloud side. Neither action requests credentials of its own. The service account needs read access to Cloud Run and to the Artifact Registry repositories that hold the images.

Permissions per job:

| Job | Permissions | Why |
| --- | --- | --- |
| scan | `contents: read`, `id-token: write` | Checkout and the OIDC token for Workload Identity. |
| report | `contents: read`, `actions: read`, `pull-requests: read`, `issues: write` | Read workflow runs and artifacts, list Dependabot PRs, and manage issues with `github.token`. |

Inputs of the report action:

| Input | Default | Meaning |
| --- | --- | --- |
| `config` | `.github/maintenance.json` | Path to the repository's maintenance configuration. |
| `extras` | empty | Extra report paths, one per line. |
| `job-results` | empty | `NAME=CONCLUSION` per line, for example `scan=${{ needs.scan.result }}`. Anything but `success` is an `error` finding keyed `job:<name>`. |
| `artifact-pattern` | empty | Artifact name globs, one per line. For each, the newest unexpired artifact of this run whose name matches is downloaded into `artifact-dir` before the extras are read. A rerun's upload is newer than the first attempt's, so it wins. A missing artifact is not an error by itself, but its report then counts as missing. |
| `artifact-dir` | `maintenance-artifacts` | Download directory for `artifact-pattern`. All matches land in the same directory. |
| `output` | `maintenance-report.json` | Merged report written by the run. |
| `language` | `en` | Message language, `en` or `ja`. |
| `publish` | `true` | `false` writes the report without touching issues. |

Inputs of the scan-cloud-run action:

| Input | Default | Meaning |
| --- | --- | --- |
| `project` | empty | Google Cloud project ID, for example `example-project`. Required unless `inventory-file` is set. |
| `region` | required | Cloud Run region. Also the default registry host for Docker credentials. |
| `inventory-file` | empty | Path to a JSON list of resources to scan instead of discovering them. See below. |
| `output-dir` | `maintenance-scan` | Directory for `report.json`, `inventory.json`, and the raw scans. Upload it as an artifact so the report job can read it. |
| `registries` | `<region>-docker.pkg.dev` | Registry hosts to configure Docker credentials for, separated by commas. |
| `language` | `en` | Summary language, `en` or `ja`. |

Issue bodies hold a managed block between two hidden markers with the fixed prefix `maintenance`. Issues opened by other tools or older copies with a different prefix are ignored. The run rewrites only that block, so notes written above or below it are kept. An issue with a damaged block or two markers for one key stops the run instead of being guessed at.

#### Inventory file

Discovery scans every Cloud Run service and job in the project. That does not fit a project that creates a new job per release and keeps the old ones, or that shares one project between environments. Old releases and other environments are scanned too, and each release gets its own finding keys. Such a repository writes the list of what is live itself and passes it as `inventory-file`:

```json
[
  {"kind": "job", "name": "drive-sync", "image": "asia-northeast1-docker.pkg.dev/example-project/app/sync@sha256:<64 hex>"},
  {"kind": "service", "name": "search-gateway", "image": "asia-northeast1-docker.pkg.dev/example-project/app/gateway@sha256:<64 hex>"}
]
```

- `kind` is `service` or `job`.
- `name` is a logical name of lowercase letters, digits, and hyphens. Finding keys use `<kind>/<name>`, for example `deployed:no-fix:job/drive-sync`, so name a resource the same way in every release.
- `image` is an image reference pinned by `@sha256:` digest.
- Each entry has exactly these three fields, the list is not empty, and no `kind` and `name` pair appears twice.

A file that breaks any of these rules is the `error` finding `deployed:unavailable`. With a file the action does not call the Cloud Run API, so it does not check the inventory again after the scan. The caller is responsible for the file matching what is deployed.

### Adding a repository-specific report

Any job can contribute findings by writing a file in the report contract, uploading it as an artifact, and naming it in the report job. For example, a job that checks whether an upstream project shipped a release:

```yaml
  upstream:
    runs-on: ubuntu-latest
    permissions:
      contents: read
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
        with:
          persist-credentials: false
      - run: python3 scripts/check_upstream_release.py --output upstream/upstream-release.json
      - if: always()
        uses: actions/upload-artifact@cf430e030ddbb5b0abf93d22962f4752f3646cd9 # v7.0.2
        with:
          name: upstream-release-${{ github.run_attempt }}
          path: upstream/
```

Then add `upstream` to the report job's `needs`, `upstream-release-*` to `artifact-pattern`, `maintenance-artifacts/upstream-release.json` to `extras`, `upstream=${{ needs.upstream.result }}` to `job-results`, and `upstream-release.json` to `required_reports`. The script writes `{"findings": []}` when there is nothing new, so the run stays complete and earlier upstream issues can close.

## Development

```sh
python3 -m unittest discover -s tests -v
```

The tests include an end-to-end run of the git steps against local bare repositories, with the GitHub API stubbed.

## License

MIT

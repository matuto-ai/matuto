# CI runtime budget

Full PR validation must satisfy both the inclusive 150-second limit in `validate.sh` and a maximum 20% increase over the verified full-validation duration of its exact main/base commit. The threshold is `min(150000, floor(baseline_ms * 120 / 100))` milliseconds; equality passes. The percentage comparison is a CI step, not a forge dependency of local `./validate.sh`.

The 20% allowance replaces the 10% limit after repeated full-suite provider runs passed all tests but exceeded the growth gate by up to 14.28%. This is pragmatic tolerance, not proof of a regression or a statistical noise filter; a heavily loaded runner can still exceed it. Several small increases can still consume the headroom. Runner noise, reruns, and deliberate exceptions remain human review decisions, not automatic retries or exception labels.

## Stored evidence

GitHub stores the baseline as a successful `ci/runtime-baseline` commit status on the **main commit SHA**. Its description is a compact JSON record:

```json
{"v":1,"ms":114754,"tree":"<40-character tested tree hash>","profile":"ubuntu-24.04"}
```

The status target URL links to the source run/job. Each main commit has its own record; there is no mutable global “last time,” repository file to update, or runner-local cache. A documentation-only merge inherits its first parent's verified duration only after the exact merged PR's successful documentation route and a local Git diff both prove that no runtime file changed. The new status records the new commit's tree and retains the original measurement link. Once published, reading a baseline does not require retaining the source job logs. Both workflows use the same `ubuntu-24.04` hosted-runner label; runner changes require explicit remeasurement rather than assuming old timings remain comparable.

The PR workflow checks out the exact PR head, not GitHub's synthetic merge commit. It reads the status on its event's exact base SHA, verifies its tree/profile, and requires that base to remain current main and be an ancestor of the candidate. If main has advanced, update the PR and rerun validation. This check cannot prevent main moving after CI; up-to-date merge enforcement or the final review freshness check still applies.

## Post-merge promotion

`.github/workflows/ci-runtime-baseline.yaml` normally publishes metadata only on pushes to main. Full-suite PRs reuse their measured duration; documentation-only PRs inherit a verified parent duration without running tests again. A successful WQ-only PR triggers one full validation on main because partial evidence cannot supply a full-suite duration. Failed validation or invalid evidence never triggers automatic recovery.

1. Resolve the exact merged PR through GitHub's `/commits/{merge_sha}/pulls` endpoint and verify its merge SHA, target branch, and repository. Require one matching PR.
2. Find the latest `validate.yml` pull-request workflow run for that PR's exact head. Require success; a newer failure or pending run never falls back to an older success.
3. Verify the workflow, event, head SHA, completed run and latest-attempt validation job, runner label, job link, and explicit `CI_RUNTIME_TREE` checkout/tree marker in the job log.
4. Require the tested tree to equal the merge commit's tree. Publish the full-validation timing, inherit the documentation-only parent baseline after proving the diff with `agents/ci-docs-only`, or measure full validation once after a WQ-only merge. An already valid baseline makes a retry a no-op.

Two publishers can finish in either order: they write to different commit SHAs. No repository-wide “latest run” lookup participates. PR jobs have read-only forge access; only the trusted main baseline workflow requests `statuses: write`. API redirects are refused except for GitHub's job-log download response: its HTTPS storage URL is fetched without the job token, and further redirects are refused.

## Initial import and recovery

The operator authorized one initial-import exception: when the exact base is the code-free main commit `37fa4ba78e5103b53cbebd0f496d3ef7e885a13e`, a successful full suite within 150 seconds establishes the first GitHub measurement instead of comparing against a nonexistent suite. Post-merge promotion records that measurement on the imported main tree. This does not reuse timings from another runner, waive any test, or allow later missing baselines to pass.

Otherwise, when a main commit has no baseline status yet, the PR checker resolves the same exact merged-PR evidence read-only, including verified inheritance through documentation-only merges. This handles a metadata job that has not finished; it never substitutes an unrelated commit or skips the percentage gate. Both PR and promotion checkouts retain Git history for the parent/diff proof.

Missing or deleted source evidence, failed/pending validation, an untested merge combination, or malformed/incompatible metadata fails explicitly. Documentation-only and WQ-only PRs keep their fast routes; neither partial run is misrepresented as a full measurement. Documentation inheritance still requires a verified full baseline in its ancestry.

To recover, open GitHub Actions → **CI runtime baseline** → **Run workflow**, select **main**, and run it. This manually requested path runs full validation once, enforces the 150-second limit, verifies main did not advance during measurement, and publishes/replaces the baseline for that exact commit. Use it after runner changes or a reviewed decision to establish a new baseline; it is not an automatic way to bypass a failing PR percentage check. For delayed/failed metadata publication with intact PR evidence, rerun the metadata job instead of measuring again.

If GitHub rejects status publication, check the baseline workflow's `statuses: write`, `contents: read`, `actions: read`, and `pull-requests: read` permissions and repository/organization permission ceilings. Do not place a personal token in PR workflows. Actual post-merge publication still needs observation after the workflow is merged.

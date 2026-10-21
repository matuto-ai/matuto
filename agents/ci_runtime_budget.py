#!/usr/bin/env python3
"""Compare full CI runs and promote their evidence to commit-pinned main baselines."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

BASELINE_CONTEXT = "ci/runtime-baseline"
WORKFLOW = ".github/workflows/validate.yml"
PROFILE = "ubuntu-24.04"
# The operator approved establishing a baseline only for this code-free base.
IMPORT_BASE = "37fa4ba78e5103b53cbebd0f496d3ef7e885a13e"
ABSOLUTE_MS = 150_000
MAX_INCREASE_PERCENT = 20


class BudgetError(Exception):
    pass


class MeasurementRequired(BudgetError):
    """A successful WQ-only run cannot supply a full-suite baseline."""


def require(condition, message):
    if not condition:
        raise BudgetError(message)


def sha(value):
    require(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{40}", value), "Invalid commit/tree identity")
    return value


def git(*args):
    result = subprocess.run(["git", *args], capture_output=True, text=True)
    require(result.returncode == 0, "Git identity/freshness check failed; update the PR from main")
    return result.stdout.strip()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        # Never forward the job token to a redirect target.
        return None


class Forge:
    def __init__(self):
        self.api = os.environ["GITHUB_API_URL"].rstrip("/")
        self.server = os.environ["GITHUB_SERVER_URL"].rstrip("/")
        self.repo = os.environ["GITHUB_REPOSITORY"]
        self.token = os.environ["GITHUB_TOKEN"]
        require(re.fullmatch(r"[\w.-]+/[\w.-]+", self.repo), "Invalid repository identity")
        self.opener = urllib.request.build_opener(NoRedirect())

    def request(self, path, data=None, *, text=False):
        request = urllib.request.Request(
            f"{self.api}/repos/{self.repo}/{path}",
            data=None if data is None else json.dumps(data).encode(),
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json",
                     "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"},
        )
        try:
            try:
                response = self.opener.open(request, timeout=20)
            except urllib.error.HTTPError as error:
                # GitHub's job-log endpoint returns a short-lived download URL.
                if error.code != 302 or not text or not re.fullmatch(r"actions/jobs/[1-9][0-9]*/logs", path):
                    raise
                location = error.headers.get("Location", "")
                target = urllib.parse.urlsplit(location)
                require(target.scheme == "https" and target.hostname and not target.username
                        and not target.password and not target.fragment, "Invalid job-log download address")
                # The storage request carries no job token and cannot redirect again.
                response = self.opener.open(urllib.request.Request(location), timeout=20)
            with response:
                payload = response.read(16 * 1024 * 1024 + 1)
        except urllib.error.HTTPError as error:
            raise BudgetError(f"GitHub request failed (HTTP {error.code}); check job-token permissions and source evidence") from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise BudgetError("GitHub request failed; baseline evidence is unavailable") from None
        require(len(payload) <= 16 * 1024 * 1024, "GitHub response exceeds the evidence size limit")
        return payload.decode("utf-8") if text else json.loads(payload)

    def pages(self, path, key=None):
        for page in range(1, 101):
            separator = "&" if "?" in path else "?"
            result = self.request(f"{path}{separator}page={page}&per_page=50")
            rows = result[key] if key else result
            require(isinstance(rows, list), "Invalid GitHub list response")
            if not rows:
                return
            yield from rows
        raise BudgetError("GitHub evidence pagination limit reached")

    def status(self, commit, context):
        statuses = [row for row in self.pages(f"commits/{sha(commit)}/statuses") if row.get("context") == context]
        return max(statuses, key=lambda row: row["id"], default=None)

    def tree(self, commit):
        return sha(self.request(f"git/commits/{sha(commit)}")["tree"]["sha"])

    def run_ids(self, url, *, job_required=True):
        target = urllib.parse.urlsplit(url)
        origin = urllib.parse.urlsplit(self.server)
        require(not target.netloc or (target.scheme, target.netloc) == (origin.scheme, origin.netloc),
                "CI evidence link points outside this forge")
        prefix = origin.path.rstrip("/")
        match = re.fullmatch(re.escape(f"{prefix}/{self.repo}/actions/runs/") + r"([1-9][0-9]*)(?:/job/([1-9][0-9]*))?", target.path)
        require(match and not target.query and not target.fragment and (not job_required or match[2]),
                "CI evidence link does not identify the expected repository run/job")
        return int(match[1]), int(match[2]) if match[2] else None


def log_lines(log):
    return [re.sub(r"^\d{4}-\d\d-\d\dT\S+ ", "", line) for line in log.splitlines()]


def full_duration(log):
    timings = {}
    for line in log_lines(log):
        match = re.fullmatch(r"TIMING validate\.([a-z-]+) ([0-9]+)\.([0-9]{3})s status=([0-9]+)", line)
        if match:
            name, seconds, millis, status = match.groups()
            require(name not in timings, "Duplicate validation timing evidence")
            timings[name] = (int(seconds) * 1000 + int(millis), int(status))
    required = {name: 0 for name in (
        "shellcheck-errors", "ci-routing", "app-unittests", "app-process-fixtures",
        "page-behaviour", "wq-compile", "wq-selftest", "total",
    )}
    required["wq-invalid-profile"] = 2
    require(all(name in timings and timings[name][1] == status for name, status in required.items()),
            "Baseline unavailable: evidence is not one successful full validation run")
    elapsed = timings["total"][0]
    require(0 < elapsed <= ABSOLUTE_MS, "Full validation duration is outside the 150-second budget")
    return elapsed


def record(elapsed, tree):
    return {"v": 1, "ms": elapsed, "tree": sha(tree), "profile": PROFILE}


def cached_baseline(forge, commit):
    status = forge.status(commit, BASELINE_CONTEXT)
    if status is None:
        return None
    require(status.get("state") == "success", "Baseline unavailable: its commit status is not successful")
    value = json.loads(status["description"])
    require(value.get("v") == 1 and value.get("profile") == PROFILE
            and type(value.get("ms")) is int and 0 < value["ms"] <= ABSOLUTE_MS,
            "Baseline unavailable: invalid duration, version, or runner profile")
    require(value.get("tree") == forge.tree(commit), "Baseline unavailable: tree identity mismatch")
    forge.run_ids(status["target_url"], job_required=False)
    return value, status["target_url"]


def merged_measurement(forge, commit):
    pulls = [pull for pull in forge.pages(f"commits/{sha(commit)}/pulls")
             if pull.get("merged_at") and pull.get("merge_commit_sha") == commit
             and pull["base"]["ref"] == "main" and pull["base"]["repo"]["full_name"] == forge.repo]
    require(len(pulls) == 1, "Baseline unavailable: this is not one exact merged PR on main")
    pull = pulls[0]
    head = sha(pull["head"]["sha"])
    tree = forge.tree(head)
    merged_tree = forge.tree(commit)
    require(tree == merged_tree,
            f"Baseline unavailable: PR #{pull['number']} head tree {tree} differs from merged tree {merged_tree}")
    runs = forge.pages(f"actions/workflows/validate.yml/runs?event=pull_request&head_sha={head}", "workflow_runs")
    run = max(runs, key=lambda row: row["id"], default=None)
    require(run and run.get("head_sha") == head and run.get("event") == "pull_request"
            and run.get("path") == WORKFLOW
            and run.get("status") == "completed" and run.get("conclusion") == "success",
            "Baseline unavailable: latest validation run does not match the merged PR/head or did not succeed")
    run_id = run["id"]
    jobs = [job for job in forge.pages(f"actions/runs/{run_id}/jobs?filter=latest", "jobs")
            if job.get("name") == "validate"]
    require(len(jobs) == 1 and jobs[0].get("run_id") == run_id and jobs[0].get("head_sha") == head
            and PROFILE in jobs[0].get("labels", [])
            and jobs[0].get("status") == "completed" and jobs[0].get("conclusion") == "success",
            "Baseline unavailable: validation job is missing or unsuccessful")
    job = jobs[0]
    require(forge.run_ids(job["html_url"]) == (run_id, job["id"]), "Validation job link does not match its identity")
    log = forge.request(f"actions/jobs/{job['id']}/logs", text=True)
    lines = log_lines(log)
    identities = [line for line in lines if line.startswith("CI_RUNTIME_TREE ")]
    require(identities == [f"CI_RUNTIME_TREE {head} {tree} {PROFILE}"], "Tested tree/runner identity mismatch")
    if "PASS: documentation-only change; runtime validation is not applicable." in lines:
        parent = sha(git("rev-parse", f"{commit}^1"))
        unchanged = subprocess.run(
            [str(Path(__file__).with_name("ci-docs-only")), parent, commit],
            capture_output=True).returncode == 0
        require(unchanged, "Documentation-only evidence changes runtime files")
        value, source = baseline(forge, parent)
        print(f"Runtime unchanged by documentation merge {commit}; inheriting {parent}", flush=True)
        return record(value["ms"], tree), source
    if "PASS: WQ-only change; manager regression is not applicable." in lines:
        raise MeasurementRequired("WQ-only validation needs one full-main baseline measurement")
    return record(full_duration(log), tree), job["html_url"]


def baseline(forge, commit):
    found = cached_baseline(forge, commit)
    if found:
        return found
    print(f"Baseline status absent for {commit}; resolving only its exact merged PR evidence", flush=True)
    return merged_measurement(forge, commit)


def publish(forge, commit, value, source):
    forge.request(f"statuses/{sha(commit)}", {
        "context": BASELINE_CONTEXT, "state": "success",
        "description": json.dumps(value, separators=(",", ":")), "target_url": source,
    })
    print(f"Baseline published: {commit} {value['ms'] / 1000:.3f}s source={source}")


def allowed_ms(baseline_ms):
    return min(ABSOLUTE_MS, baseline_ms * (100 + MAX_INCREASE_PERCENT) // 100)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("check")
    check.add_argument("--base", required=True)
    check.add_argument("--log", required=True)
    promote = commands.add_parser("promote")
    promote.add_argument("--commit", required=True)
    seed = commands.add_parser("seed")
    seed.add_argument("--commit", required=True)
    seed.add_argument("--log", required=True)
    seed.add_argument("--run-url", required=True)
    args = parser.parse_args()
    try:
        forge = Forge()
        if args.command == "check":
            base = sha(args.base)
            git("merge-base", "--is-ancestor", base, "HEAD")
            current = forge.request("branches/main")["commit"]["sha"]
            require(current == base, "Main advanced; update the PR from current main and rerun validation")
            elapsed = full_duration(Path(args.log).read_text())
            if base == IMPORT_BASE:
                print(f"Initial import: {elapsed / 1000:.3f}s within 150s; no pre-import runtime baseline. "
                      "The merged import establishes the GitHub baseline.")
                return 0
            value, source = baseline(forge, base)
            limit = allowed_ms(value["ms"])
            print(f"CI runtime: {elapsed / 1000:.3f}s; main {base}: {value['ms'] / 1000:.3f}s; "
                  f"limit {limit / 1000:.3f}s (+{MAX_INCREASE_PERCENT}%, absolute 150s); source={source}")
            require(elapsed <= limit, "CI runtime increase exceeds the per-PR budget")
        elif args.command == "promote":
            commit = sha(args.commit)
            if cached_baseline(forge, commit):
                print(f"Baseline already recorded for {commit}")
            else:
                value, source = merged_measurement(forge, commit)
                publish(forge, commit, value, source)
        else:
            commit = sha(args.commit)
            require(git("rev-parse", "HEAD") == commit, "Manual baseline checkout does not match its commit")
            require(forge.request("branches/main")["commit"]["sha"] == commit,
                    "Main advanced during manual measurement; rerun it on current main")
            forge.run_ids(args.run_url, job_required=False)
            value = record(full_duration(Path(args.log).read_text()), git("rev-parse", "HEAD^{tree}"))
            publish(forge, commit, value, args.run_url)
    except MeasurementRequired as error:
        print(f"CI runtime budget: {error}", file=sys.stderr)
        return 2 if args.command == "promote" else 1
    except (BudgetError, KeyError, ValueError, TypeError, AttributeError, OSError) as error:
        # API bodies and unexpected values can contain secrets; never echo them.
        message = str(error) if isinstance(error, BudgetError) else "Invalid or unavailable CI baseline evidence/configuration"
        print(f"CI runtime budget: {message}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

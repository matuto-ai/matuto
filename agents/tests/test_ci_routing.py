"""Keep the required CI job present without skipping runtime changes."""
import http.server
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock
import urllib.error


AGENTS = Path(__file__).resolve().parents[1]
PREDICATE = AGENTS / "ci-docs-only"
WQ_PREDICATE = AGENTS / "ci-wq-only"
VALIDATOR = AGENTS.parent / "validate.sh"


class RoutingTest(unittest.TestCase):
    def test_changed_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = dict(os.environ, GIT_AUTHOR_NAME="CI fixture", GIT_AUTHOR_EMAIL="ci@example.invalid",
                       GIT_COMMITTER_NAME="CI fixture", GIT_COMMITTER_EMAIL="ci@example.invalid")

            def git(*arguments):
                return subprocess.check_output(["git", *arguments], cwd=root, env=env, stderr=subprocess.PIPE, text=True).strip()

            def write(path):
                target = root / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("fixture\n")

            def selected(predicate, base):
                return subprocess.run([str(predicate), base], cwd=root, stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE).returncode == 0

            git("init", "-q")
            for path in ("README.md", "CONTEXT.md", "LICENSE", "docs/guide.md", "agents/runtime.py", "wq.py", "wq.sh"):
                write(path)
            git("add", ".")
            git("commit", "-qm", "baseline", "--no-gpg-sign")
            base = git("rev-parse", "HEAD")
            self.assertFalse(selected(PREDICATE, base), "empty diff must run validation")
            self.assertFalse(selected(PREDICATE, ""))
            self.assertFalse(selected(PREDICATE, "0" * 40))
            self.assertFalse(selected(PREDICATE, "f" * 40))
            self.assertFalse(selected(WQ_PREDICATE, base), "empty diff must run validation")
            self.assertFalse(selected(WQ_PREDICATE, ""))
            self.assertFalse(selected(WQ_PREDICATE, "0" * 40))
            self.assertFalse(selected(WQ_PREDICATE, "f" * 40))

            for paths, expected in (
                (("README.md",), True),
                (("CONTEXT.md", "LICENSE"), True),
                (("docs/guide.md", "docs/decisions/new.md"), True),
                (("agents/runtime.py",), False),
                (("README.md", "agents/runtime.py"), False),
                (("agents/tests/new.py",), False),
                ((".github/workflows/validate.yml",), False),
                (("agents/README.md",), False),
                (("docs/script.sh",), False),
            ):
                with self.subTest(paths=paths):
                    git("reset", "--hard", base)
                    git("clean", "-fd")
                    for path in paths:
                        write(path)
                        with (root / path).open("a") as stream:
                            stream.write("change\n")
                    git("add", ".")
                    git("commit", "-qm", "change", "--no-gpg-sign")
                    self.assertEqual(selected(PREDICATE, base), expected)

            git("reset", "--hard", base)
            git("clean", "-fd")
            git("mv", "agents/runtime.py", "docs/runtime.md")
            git("commit", "-qm", "move runtime into documentation", "--no-gpg-sign")
            self.assertFalse(selected(PREDICATE, base), "runtime deletion must not be hidden by a rename")

            git("reset", "--hard", base)
            (root / "docs/guide.md").unlink()
            git("add", ".")
            git("commit", "-qm", "delete documentation", "--no-gpg-sign")
            self.assertTrue(selected(PREDICATE, base))

            for paths, expected in (
                (("wq.py",), True),
                (("wq.sh",), True),
                (("wq.py", "wq.sh"), True),
                (("README.md", "wq.py"), False),
                (("agents/runtime.py", "wq.sh"), False),
                (("agents/tests/new.py",), False),
                (("agents/tests/runtime_fixture.py",), False),
                (("agents/matuto-lab",), False),
                ((".github/workflows/validate.yml",), False),
                (("validate.sh",), False),
                (("docs/guide.md",), False),
            ):
                with self.subTest(wq_paths=paths):
                    git("reset", "--hard", base)
                    git("clean", "-fd")
                    for path in paths:
                        target = root / path
                        target.parent.mkdir(parents=True, exist_ok=True)
                        with target.open("a") as stream:
                            stream.write("change\n")
                    git("add", ".")
                    git("commit", "-qm", "WQ route change", "--no-gpg-sign")
                    self.assertEqual(selected(WQ_PREDICATE, base), expected)

            git("reset", "--hard", base)
            git("clean", "-fd")
            (root / "wq.py").unlink()
            git("add", ".")
            git("commit", "-qm", "delete WQ runtime", "--no-gpg-sign")
            self.assertFalse(selected(WQ_PREDICATE, base), "runtime deletion must run full validation")

            git("reset", "--hard", base)
            git("clean", "-fd")
            git("mv", "wq.py", "docs/wq.py")
            git("commit", "-qm", "move WQ runtime into docs", "--no-gpg-sign")
            self.assertFalse(selected(WQ_PREDICATE, base), "runtime deletion must not be hidden by a rename")

    def test_selected_validation_failure_propagates(self):
        with tempfile.TemporaryDirectory() as directory:
            fake_bin = Path(directory)
            failing_check = fake_bin / "shellcheck"
            failing_check.write_text('#!/bin/sh\necho "shellcheck stderr diagnostic" >&2\nexit 17\n')
            failing_check.chmod(0o755)
            env = dict(os.environ, PATH=f"{fake_bin}:{os.environ['PATH']}")
            result = subprocess.run([str(VALIDATOR), "--wq-only"], env=env,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.assertEqual(result.returncode, 17)
            self.assertIn(b"shellcheck stderr diagnostic", result.stderr)
            timing = {}
            for line in result.stdout.decode().splitlines():
                if not line.startswith("TIMING "):
                    continue
                _, name, elapsed, status = line.split()
                self.assertRegex(name, r"^validate\.[a-z0-9-]+$")
                self.assertRegex(elapsed, r"^\d+\.\d{3}s$")
                self.assertRegex(status, r"^status=\d+$")
                timing[name] = status
            self.assertEqual(timing.get("validate.shellcheck-errors"), "status=17",
                             "the failed command must be named with its original status")
            self.assertEqual(timing.get("validate.total"), "status=17",
                             "the inclusive total must report the failed status")

    def test_validation_total_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "bin").mkdir()
            clock = root / "clock"
            # Replace only the clock; execute the validator's real stages and EXIT trap.
            source = VALIDATOR.read_text()
            clock_function = next(line for line in source.splitlines() if line.startswith("now_ms()"))
            (root / "validate.sh").write_text(source.replace(
                clock_function, 'now_ms() { local value; read -r value < "$CLOCK_FILE"; printf "%s" "$value"; }'))
            python = root / "bin/python3"
            python.write_text('#!/bin/sh\nprintf "%s\\n" "$ELAPSED_MS" > "$CLOCK_FILE"\nexit "$CHECK_STATUS"\n')
            python.chmod(0o755)
            wq = root / "wq.sh"
            wq.write_text('#!/bin/sh\n[ "${WQ_PROFILE:-}" != invalid ] || exit 2\nexit 0\n')
            wq.chmod(0o755)
            for elapsed_ms, check_status, expected_status in (
                (149999, 0, 0), (150000, 0, 0), (150001, 0, 1), (151234, 0, 1),
                (151234, 17, 17),
            ):
                with self.subTest(elapsed_ms=elapsed_ms, check_status=check_status):
                    clock.write_text("0\n")
                    env = dict(os.environ, PATH=f"{root / 'bin'}:{os.environ['PATH']}",
                               CLOCK_FILE=str(clock), ELAPSED_MS=str(elapsed_ms), CHECK_STATUS=str(check_status))
                    result = subprocess.run(["bash", str(root / "validate.sh"), "--wq-only"], env=env,
                                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                    self.assertEqual(result.returncode, expected_status, result.stderr)
                    elapsed = f"{elapsed_ms // 1000}.{elapsed_ms % 1000:03d}"
                    self.assertIn(f"TIMING validate.total {elapsed}s status={expected_status}", result.stdout)
                    if expected_status == 1:
                        self.assertIn(f"{elapsed}s", result.stderr)
                        self.assertIn("150s", result.stderr)
                    else:
                        self.assertNotIn("budget", result.stderr)
                    if check_status:
                        self.assertIn(f"TIMING validate.shellcheck-errors {elapsed}s status=17", result.stdout)

    def test_validation_runs_process_fixtures_after_app_tests(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tests = root / "agents/tests"
            (tests / "app").mkdir(parents=True)
            (root / "bin").mkdir()
            (root / "validate.sh").write_bytes(VALIDATOR.read_bytes())
            (tests / "test_ci_routing.py").write_text('''import os
from pathlib import Path
if int(os.environ["ROUTING_STATUS"]):
    (Path(os.environ["FIXTURE_DIR"]) / "release").touch()
    raise SystemExit(int(os.environ["ROUTING_STATUS"]))
''')
            process_fixture_test = '''import os
from pathlib import Path
import time
import unittest
class ProcessFixtureTest(unittest.TestCase):
    def test_live_server_fixture_runs(self):
        root = Path(os.environ["FIXTURE_DIR"])
        (root / "fixture-ran").touch()
        marker = root / "fixture-active"
        marker.touch()
        try:
            time.sleep(.1)
        finally:
            marker.unlink(missing_ok=True)
'''
            for name in ("test_procedures.py", "test_serve.py", "test_store_launch.py"):
                (tests / "app" / name).write_text(process_fixture_test)
            (root / "wq.py").write_text("")

            def executable(path, source):
                path.write_text(source)
                path.chmod(0o755)

            executable(root / "bin/shellcheck", '#!/bin/sh\nprintf "[]\\n"\n')
            (tests / "app/test_fixture.py").write_text('''import os
from pathlib import Path
import unittest
class FixtureTest(unittest.TestCase):
    def test_result(self):
        if int(os.environ["APP_STATUS"]):
            (Path(os.environ["FIXTURE_DIR"]) / "release").touch()
            self.fail("app failure fixture")
''')
            (tests / "app/fixture.test.cjs").write_text("")
            executable(root / "bin/node", '''#!/bin/sh
if [ "$PAGE_STATUS" != 0 ]; then touch "$FIXTURE_DIR/release"; fi
exit "$PAGE_STATUS"
''')
            executable(root / "wq.sh", '''#!/bin/sh
[ "${WQ_PROFILE:-}" != invalid ] || exit 2
touch "$FIXTURE_DIR/release"
exit "$WQ_STATUS"
''')
            for routing, app, page, wq, expected in (
                (0, 0, 0, 0, 0), (37, 0, 0, 0, 37),
                (0, 1, 0, 0, 1), (0, 0, 23, 0, 23), (0, 0, 0, 29, 29),
            ):
                with self.subTest(routing=routing, app=app, page=page, wq=wq):
                    for name in ("release", "joined", "fixture-active", "fixture-ran"):
                        (root / name).unlink(missing_ok=True)
                    env = dict(os.environ, PATH=f"{root / 'bin'}:{os.environ['PATH']}",
                               FIXTURE_DIR=str(root), ROUTING_STATUS=str(routing),
                               APP_STATUS=str(app), PAGE_STATUS=str(page), WQ_STATUS=str(wq))
                    result = subprocess.run(["bash", str(root / "validate.sh")], env=env,
                                            capture_output=True, text=True, timeout=15)
                    self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
                    self.assertEqual((root / "fixture-ran").exists(), routing == 0)
                    self.assertIn("TIMING validate.total ", result.stdout)
                    self.assertIn(f"status={expected}", result.stdout.splitlines()[-1])

    def test_page_output_is_quiet_on_success_and_replayed_on_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "agents/tests/app").mkdir(parents=True)
            (root / "bin").mkdir()
            (root / "validate.sh").write_bytes(VALIDATOR.read_bytes())
            for name in ("shellcheck", "python3"):
                stub = root / "bin" / name
                stub.write_text("#!/bin/sh\nexit 0\n")
                stub.chmod(0o755)
            node = root / "bin" / "node"
            node.write_text('#!/bin/sh\ncase "$1" in\n  *01-fail*) echo "probe stdout"; echo "probe diagnostic" >&2; exit "$PAGE_STATUS" ;;\n  *) echo "second page test ran"; echo "TIMING page.fixture 0.001s status=0" ;;\nesac\n')
            node.chmod(0o755)
            wq = root / "wq.sh"
            wq.write_text('#!/bin/sh\n[ "${WQ_PROFILE:-}" != invalid ] || exit 2\nexit 0\n')
            wq.chmod(0o755)
            (root / "agents/tests/app/01-fail.test.cjs").write_text("// fails through the node stub\n")
            (root / "agents/tests/app/02-pass.test.cjs").write_text("// passes through the node stub\n")
            for status in (0, 23):
                with self.subTest(status=status):
                    env = dict(os.environ, PATH=f"{root / 'bin'}:{os.environ['PATH']}", PAGE_STATUS=str(status))
                    result = subprocess.run(["bash", str(root / "validate.sh"), "--wq-only"], env=env,
                                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                    self.assertEqual(result.returncode, status, result.stderr)
                    self.assertIn("validate.page-behaviour", result.stdout)
                    self.assertIn(f"status={status}", result.stdout)
                    if status:
                        self.assertIn("probe stdout", result.stderr)
                        self.assertIn("probe diagnostic", result.stderr)
                        self.assertNotIn("second page test ran", result.stdout + result.stderr,
                                         "a later page test must not mask an earlier failure")
                    else:
                        self.assertNotIn("probe stdout", result.stdout)
                        self.assertNotIn("second page test ran", result.stdout)
                        self.assertEqual(result.stderr, "")
                        self.assertIn("TIMING page.fixture 0.001s status=0", result.stdout)


class RuntimeBudgetTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.directory.cleanup)
        cls.root = Path(cls.directory.name)
        env = dict(os.environ, GIT_AUTHOR_NAME="CI fixture", GIT_AUTHOR_EMAIL="ci@example.invalid",
                   GIT_COMMITTER_NAME="CI fixture", GIT_COMMITTER_EMAIL="ci@example.invalid")

        def git(*args):
            return subprocess.check_output(["git", *args], cwd=cls.root, env=env, stderr=subprocess.PIPE, text=True).strip()

        git("init", "-q")
        (cls.root / "file").write_text("baseline")
        git("add", ".")
        git("commit", "-qm", "base", "--no-gpg-sign")
        cls.base = git("rev-parse", "HEAD")
        cls.tree = git("rev-parse", "HEAD^{tree}")
        (cls.root / "README.md").write_text("documentation only")
        git("add", ".")
        git("commit", "-qm", "documentation", "--no-gpg-sign")
        cls.docs = git("rev-parse", "HEAD")
        cls.docs_tree = git("rev-parse", "HEAD^{tree}")
        (cls.root / "file").write_text("candidate")
        git("commit", "-qam", "candidate", "--no-gpg-sign")
        cls.head = git("rev-parse", "HEAD")
        cls.head_tree = git("rev-parse", "HEAD^{tree}")
        cls.helper = AGENTS / "ci_runtime_budget.py"
        spec = importlib.util.spec_from_file_location("ci_runtime_budget", cls.helper)
        cls.budget = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.budget)

    def setUp(self):
        self.merged_head = "c" * 40
        self.source = "/fixture/project/actions/runs/11/job/21"
        self.calls = []
        self.writes = []
        self.statuses = {self.base: {1: []}}
        self.pull = {"number": 7, "merged_at": "2026-01-01T00:00:00Z", "merge_commit_sha": self.base,
                     "head": {"sha": self.merged_head},
                     "base": {"ref": "main", "repo": {"full_name": "fixture/project"}}}
        self.run = {"id": 11, "head_sha": self.merged_head, "event": "pull_request",
                    "path": ".github/workflows/validate.yml", "status": "completed", "conclusion": "success"}
        self.runs = {self.merged_head: {1: [self.run]}}
        self.job = {"id": 21, "name": "validate", "status": "completed", "conclusion": "success",
                    "run_id": 11, "head_sha": self.merged_head, "labels": [self.budget.PROFILE],
                    "html_url": self.source}
        self.jobs = {11: [self.job]}
        self.responses = {
            "branches/main": {"commit": {"sha": self.base}},
            f"commits/{self.base}/pulls": [self.pull],
            f"git/commits/{self.base}": {"tree": {"sha": self.tree}},
            f"git/commits/{self.merged_head}": {"tree": {"sha": self.tree}},
            f"git/commits/{self.head}": {"tree": {"sha": self.head_tree}},
            "actions/jobs/21/logs": f"CI_RUNTIME_TREE {self.merged_head} {self.tree} {self.budget.PROFILE}\n" + self.log(100000),
        }
        self.log_path = self.root / "validation.log"
        self.log_path.write_text(self.log(110000))
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                from urllib.parse import parse_qs, unquote, urlsplit
                url = urlsplit(self.path)
                path = unquote(url.path).removeprefix("/repos/fixture/project/")
                owner.calls.append(("GET", path))
                query = parse_qs(url.query)
                page = int(query.get("page", ["1"])[0])
                if "page" in query and query.get("per_page") != ["50"]:
                    return self.answer((400, {"message": "incorrect pagination"}))
                if path.startswith("commits/") and path.endswith("/statuses"):
                    value = owner.statuses.get(path.split("/")[1], {}).get(page, [])
                elif path == "actions/workflows/validate.yml/runs":
                    value = {"workflow_runs": owner.runs.get(query.get("head_sha", [""])[0], {}).get(page, [])}
                elif path.startswith("actions/runs/") and path.endswith("/jobs"):
                    if query.get("filter") != ["latest"]:
                        return self.answer((400, {"message": "latest attempt required"}))
                    value = {"jobs": owner.jobs.get(int(path.split("/")[2]), []) if page == 1 else []}
                elif path.startswith("commits/") and path.endswith("/pulls") and page > 1:
                    value = []
                else:
                    value = owner.responses.get(path, (404, {"message": "missing fixture"}))
                self.answer(value)

            def do_POST(self):
                path = self.path.removeprefix("/repos/fixture/project/")
                owner.calls.append(("POST", path))
                value = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                owner.writes.append((path, value))
                commit = path.split("/")[1]
                owner.statuses[commit] = {1: [{**value, "id": 100}]}
                self.answer(value)

            def answer(self, value):
                code = 200
                if isinstance(value, tuple):
                    code, value = value
                if self.headers.get("Authorization") != "Bearer fixture-secret":
                    code = 403
                payload = value.encode() if isinstance(value, str) else json.dumps(value).encode()
                self.send_response(code)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *_args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.env = dict(os.environ, GITHUB_API_URL=f"http://127.0.0.1:{self.server.server_port}",
                        GITHUB_SERVER_URL=f"http://127.0.0.1:{self.server.server_port}",
                        GITHUB_REPOSITORY="fixture/project", GITHUB_TOKEN="fixture-secret", PYTHONDONTWRITEBYTECODE="1")

    @staticmethod
    def log(elapsed):
        stages = ("shellcheck-errors", "ci-routing", "app-unittests", "app-process-fixtures",
                  "page-behaviour", "wq-compile", "wq-selftest")
        return "".join(f"TIMING validate.{name} 0.001s status=0\n" for name in stages) + (
            f"TIMING validate.wq-invalid-profile 0.001s status=2\n"
            f"TIMING validate.total {elapsed // 1000}.{elapsed % 1000:03d}s status=0\n")

    def invoke(self, *args):
        return subprocess.run([sys.executable, "-B", str(self.helper), *args], cwd=self.root,
                              env=self.env, capture_output=True, text=True, timeout=10)

    def cache(self, elapsed=100000):
        self.statuses[self.base] = {1: [{
            "id": 1, "context": self.budget.BASELINE_CONTEXT, "state": "success",
            "description": json.dumps(self.budget.record(elapsed, self.tree)), "target_url": self.source,
        }]}

    def test_percentage_and_absolute_boundaries_use_persisted_baseline(self):
        for baseline, elapsed, success in ((100000, 119999, True), (100000, 120000, True),
                                           (100000, 120001, False), (149000, 150000, True),
                                           (149000, 150001, False), (100001, 120002, False),
                                           (100842, 115238, True), (115110, 138132, True)):
            with self.subTest(baseline=baseline, elapsed=elapsed):
                self.cache(baseline)
                self.log_path.write_text(self.log(elapsed))
                result = self.invoke("check", "--base", self.base, "--log", str(self.log_path))
                self.assertEqual(result.returncode, 0 if success else 1, result.stdout + result.stderr)
        self.assertFalse(self.writes, "PR checks must be read-only")
        self.assertNotIn(("GET", f"commits/{self.base}/pulls"), self.calls, "cached baselines must survive expired logs")

    def test_missing_status_resolves_only_exact_merged_pr_and_run(self):
        # Pagination cannot hide a newer attempt whose result must be considered.
        self.runs[self.merged_head] = {1: [{**self.run, "id": 10, "conclusion": "failure"}], 2: [self.run]}
        result = self.invoke("check", "--base", self.base, "--log", str(self.log_path))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("resolving only its exact merged PR", result.stdout)
        self.assertIn(("GET", "actions/jobs/21/logs"), self.calls)
        self.assertFalse(self.writes)

    def test_promotion_is_commit_pinned_and_idempotent(self):
        for _ in range(2):
            result = self.invoke("promote", "--commit", self.base)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(self.writes), 1)
        path, status = self.writes[0]
        self.assertEqual(path, f"statuses/{self.base}")
        self.assertEqual(json.loads(status["description"]), self.budget.record(100000, self.tree))
        self.assertEqual(status["target_url"], self.source)

    def test_documentation_merge_inherits_verified_parent_without_running_validation(self):
        self.cache(98765)
        inherited_source = "/fixture/project/actions/runs/9/job/19"
        self.statuses[self.base][1][0]["target_url"] = inherited_source
        self.pull["merge_commit_sha"] = self.docs
        self.responses[f"commits/{self.docs}/pulls"] = [self.pull]
        self.responses[f"git/commits/{self.docs}"] = {"tree": {"sha": self.docs_tree}}
        self.responses[f"git/commits/{self.merged_head}"] = {"tree": {"sha": self.docs_tree}}
        self.responses["actions/jobs/21/logs"] = (
            f"CI_RUNTIME_TREE {self.merged_head} {self.docs_tree} ubuntu-24.04\n"
            "PASS: documentation-only change; runtime validation is not applicable.\n")
        self.responses["branches/main"] = {"commit": {"sha": self.docs}}
        result = self.invoke("check", "--base", self.docs, "--log", str(self.log_path))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("limit 118.518s", result.stdout)
        self.assertFalse(self.writes, "PR fallback must stay read-only")
        result = self.invoke("promote", "--commit", self.docs)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(self.writes), 1)
        path, status = self.writes[0]
        self.assertEqual(path, f"statuses/{self.docs}")
        self.assertEqual(json.loads(status["description"]), self.budget.record(98765, self.docs_tree))
        self.assertEqual(status["target_url"], inherited_source)

    def test_docs_route_cannot_hide_runtime_changes_or_missing_parent_evidence(self):
        self.responses["actions/jobs/21/logs"] = (
            f"CI_RUNTIME_TREE {self.merged_head} {self.head_tree} ubuntu-24.04\n"
            "PASS: documentation-only change; runtime validation is not applicable.\n")
        self.pull["merge_commit_sha"] = self.head
        self.responses[f"commits/{self.head}/pulls"] = [self.pull]
        self.responses[f"git/commits/{self.merged_head}"] = {"tree": {"sha": self.head_tree}}
        result = self.invoke("promote", "--commit", self.head)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("changes runtime files", result.stderr)
        self.assertFalse(self.writes)

        self.pull["merge_commit_sha"] = self.docs
        self.responses[f"commits/{self.docs}/pulls"] = [self.pull]
        self.responses[f"git/commits/{self.docs}"] = {"tree": {"sha": self.docs_tree}}
        self.responses[f"git/commits/{self.merged_head}"] = {"tree": {"sha": self.docs_tree}}
        self.responses["actions/jobs/21/logs"] = self.responses["actions/jobs/21/logs"].replace(
            self.head_tree, self.docs_tree)
        self.responses[f"commits/{self.base}/pulls"] = (404, {"message": "no baseline"})
        result = self.invoke("promote", "--commit", self.docs)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertFalse(self.writes)

    def test_wq_only_requests_measurement_but_failed_validation_cannot(self):
        self.responses["actions/jobs/21/logs"] = (
            f"CI_RUNTIME_TREE {self.merged_head} {self.tree} ubuntu-24.04\n"
            "PASS: WQ-only change; manager regression is not applicable.\n")
        result = self.invoke("promote", "--commit", self.base)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertFalse(self.writes)
        self.run["conclusion"] = "failure"
        result = self.invoke("promote", "--commit", self.base)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertFalse(self.writes)

    def test_out_of_order_merges_keep_independent_baselines(self):
        earlier = "d" * 40
        earlier_head = "e" * 40
        earlier_tree = "f" * 40
        self.responses[f"commits/{earlier}/pulls"] = [{
            **self.pull, "number": 8, "merge_commit_sha": earlier, "head": {"sha": earlier_head}}]
        for commit in (earlier, earlier_head):
            self.responses[f"git/commits/{commit}"] = {"tree": {"sha": earlier_tree}}
        self.runs[earlier_head] = {1: [{**self.run, "id": 12, "head_sha": earlier_head}]}
        self.jobs[12] = [{**self.job, "id": 22, "run_id": 12, "head_sha": earlier_head,
                          "html_url": "/fixture/project/actions/runs/12/job/22"}]
        self.responses["actions/jobs/22/logs"] = (
            f"CI_RUNTIME_TREE {earlier_head} {earlier_tree} ubuntu-24.04\n" + self.log(80000))
        # The current main baseline finishes before the preceding merge's publisher.
        for commit in (self.base, earlier):
            result = self.invoke("promote", "--commit", commit)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual([(path, json.loads(value["description"])["ms"]) for path, value in self.writes],
                         [(f"statuses/{self.base}", 100000), (f"statuses/{earlier}", 80000)])
        result = self.invoke("check", "--base", self.base, "--log", str(self.log_path))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("limit 120.000s", result.stdout)

    def test_failed_pending_wrong_identity_and_partial_evidence_never_promote(self):
        mutations = (
            (self.pull, "merge_commit_sha", "a" * 40), (self.pull, "merged_at", None),
            (self.run, "head_sha", "a" * 40), (self.run, "path", ".github/workflows/other.yml"),
            (self.run, "conclusion", "failure"), (self.run, "status", "in_progress"),
            (self.job, "conclusion", "cancelled"), (self.job, "labels", ["different-runner"]),
            (self.responses, "actions/jobs/21/logs", self.log(100000).replace("app-process-fixtures", "other")),
            (self.responses, "actions/jobs/21/logs", f"CI_RUNTIME_TREE {self.merged_head} {'a' * 40} ubuntu-24.04\n" + self.log(100000)),
            (self.responses, f"git/commits/{self.base}", {"tree": {"sha": "a" * 40}}),
        )
        for target, field, value in mutations:
            with self.subTest(field=field, value=value):
                old = target[field]
                target[field] = value
                try:
                    result = self.invoke("promote", "--commit", self.base)
                    self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                finally:
                    target[field] = old
        self.assertFalse(self.writes)

    def test_latest_failure_does_not_fall_back_to_older_success(self):
        self.runs[self.merged_head][1].append({**self.run, "id": 12, "conclusion": "failure"})
        result = self.invoke("promote", "--commit", self.base)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertNotIn(("GET", "actions/jobs/21/logs"), self.calls)
        self.assertFalse(self.writes)

    def test_unproven_checkout_never_promotes(self):
        self.responses["actions/jobs/21/logs"] = self.log(100000)
        result = self.invoke("promote", "--commit", self.base)
        self.assertEqual(result.returncode, 1)
        self.assertFalse(self.writes)

    def test_stale_main_and_invalid_cache_fail_without_guessing(self):
        self.cache()
        self.responses["branches/main"] = {"commit": {"sha": "a" * 40}}
        result = self.invoke("check", "--base", self.base, "--log", str(self.log_path))
        self.assertEqual(result.returncode, 1)
        self.assertIn("Main advanced", result.stderr)
        self.responses["branches/main"] = {"commit": {"sha": self.base}}
        for description in ("not json", '{"v":1,"ms":0}', json.dumps(self.budget.record(100000, "a" * 40))):
            self.statuses[self.base][1][0]["description"] = description
            result = self.invoke("check", "--base", self.base, "--log", str(self.log_path))
            self.assertEqual(result.returncode, 1)
        self.assertFalse(self.writes)

    def test_manual_recovery_requires_current_main_checkout_and_full_run(self):
        self.responses["branches/main"] = {"commit": {"sha": self.head}}
        result = self.invoke("seed", "--commit", self.head, "--log", str(self.log_path),
                             "--run-url", "/fixture/project/actions/runs/12")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.writes[0][0], f"statuses/{self.head}")
        self.assertEqual(json.loads(self.writes[0][1]["description"])["tree"], self.head_tree)
        self.responses["branches/main"] = {"commit": {"sha": self.base}}
        result = self.invoke("seed", "--commit", self.head, "--log", str(self.log_path),
                             "--run-url", "/fixture/project/actions/runs/12")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(len(self.writes), 1)

    def test_initial_import_exception_is_exact_and_still_requires_full_success(self):
        argv = [str(self.helper), "check", "--base", self.base, "--log", str(self.log_path)]
        with mock.patch.dict(os.environ, self.env), mock.patch.object(sys, "argv", argv), \
                mock.patch.object(self.budget, "git", return_value=""), \
                mock.patch.object(self.budget, "IMPORT_BASE", self.base), \
                mock.patch("sys.stdout", new_callable=io.StringIO), \
                mock.patch("sys.stderr", new_callable=io.StringIO):
            self.assertEqual(self.budget.main(), 0)
            self.log_path.write_text(self.log(150001))
            self.assertEqual(self.budget.main(), 1)
            self.log_path.write_text("TIMING validate.total 1.000s status=0\n")
            self.assertEqual(self.budget.main(), 1)
            self.log_path.write_text(self.log(100000))
            self.responses["branches/main"] = {"commit": {"sha": self.head}}
            self.assertEqual(self.budget.main(), 1)
        self.responses["branches/main"] = {"commit": {"sha": self.base}}
        self.responses[f"commits/{self.base}/pulls"] = []
        result = self.invoke("check", "--base", self.base, "--log", str(self.log_path))
        self.assertEqual(result.returncode, 1, "other missing baselines must not get the import exception")
        self.assertFalse(self.writes)

    def test_log_download_redirect_never_forwards_authorization(self):
        with mock.patch.dict(os.environ, self.env):
            forge = self.budget.Forge()
        requests = []
        target = "https://logs.example.invalid/signed-log?signature=fixture"

        def download(request, timeout):
            requests.append(request)
            if len(requests) == 1:
                raise urllib.error.HTTPError(request.full_url, 302, "Found", {"Location": target}, None)
            return io.BytesIO(b"test log")

        with mock.patch.object(forge.opener, "open", side_effect=download):
            self.assertEqual(forge.request("actions/jobs/21/logs", text=True), "test log")
        self.assertEqual(requests[0].get_header("Authorization"), "Bearer fixture-secret")
        self.assertIsNone(requests[1].get_header("Authorization"))
        self.assertEqual(requests[1].full_url, target)
        for path, location in (("branches/main", target),
                               ("actions/jobs/21/logs", "http://logs.example.invalid/log"),
                               ("actions/jobs/21/logs", "https://user:secret@logs.example.invalid/log")):
            with self.subTest(path=path, location=location):
                error = urllib.error.HTTPError("https://api.example.invalid", 302, "Found", {"Location": location}, None)
                with mock.patch.object(forge.opener, "open", side_effect=error) as opened:
                    with self.assertRaises(self.budget.BudgetError):
                        forge.request(path, text=True)
                self.assertEqual(opened.call_count, 1)
        self.assertIsNone(self.budget.NoRedirect().redirect_request(None, None, 302, "Found", {}, target))

    def test_forge_errors_and_foreign_links_do_not_expose_the_token(self):
        self.responses[f"commits/{self.base}/pulls"] = (403, {"message": "fixture-secret"})
        result = self.invoke("promote", "--commit", self.base)
        self.assertEqual(result.returncode, 1)
        self.assertIn("HTTP 403", result.stderr)
        self.assertNotIn("fixture-secret", result.stdout + result.stderr)
        self.responses[f"commits/{self.base}/pulls"] = [self.pull]
        self.job["html_url"] = "https://outside.invalid/fixture/project/actions/runs/11/job/21"
        result = self.invoke("promote", "--commit", self.base)
        self.assertEqual(result.returncode, 1)
        self.assertNotIn(("GET", "actions/jobs/21/logs"), self.calls)
        self.assertFalse(self.writes)


if __name__ == "__main__":
    unittest.main()

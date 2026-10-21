"""The Runtime › Install a version flow through the protected manager HTTP seam."""

import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

AGENTS = Path(__file__).resolve().parents[2]
if str(AGENTS) not in sys.path:
    sys.path.insert(0, str(AGENTS))

from matuto import admission, login, recipes, sessions  # noqa: E402
from matuto.web import ALLOWED_HOST, MANAGER_ORIGIN, SESSION_COOKIE, build_server  # noqa: E402


class RuntimeInstallHttpTest(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.environment = mock.patch.dict(os.environ, {
            "XDG_CONFIG_HOME": str(self.home / "config"),
            "XDG_DATA_HOME": str(self.home / "data"),
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        login.create_login("operator", "secret", "secret")
        self.token, _ = sessions.start_session("operator", "secret")
        self.server = build_server(port=0)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/api/runtime-install"
        handler = self.server.RequestHandlerClass.routes["/api/runtime-install"]
        self.route_globals = handler.__globals__
        self.choices = mock.patch.object(self.route_globals["runtime_builds"], "choices",
                                         return_value={
            "state": "completed", "cards": [{"pciAddress": "0000:03:00.0",
                "name": "Test GPU", "gfxTarget": "gfx1201", "canBuildHIP": True,
                "targetReason": None, "default": False}],
            "recipes": [{"name": name, "missingPrerequisites": []}
                        for name in recipes.RECIPES],
        })
        self.choices.start()
        self.addCleanup(self.choices.stop)
        self.prerequisites = mock.patch.object(
            self.route_globals["runtime_builds"], "prerequisites", return_value=[])
        self.prerequisites.start()
        self.addCleanup(self.prerequisites.stop)
        self.serving = mock.patch.object(
            self.route_globals["serving_state"], "read",
            return_value=SimpleNamespace(state=admission.STOPPED))
        self.serving.start()
        self.addCleanup(self.serving.stop)

    def request(self, payload=None):
        headers = {"Host": ALLOWED_HOST, "Cookie": f"{SESSION_COOKIE}={self.token}"}
        data = None
        if payload is not None:
            headers.update({"Origin": MANAGER_ORIGIN, "X-CSRF-Token": self.server.RequestHandlerClass.csrf,
                            "Content-Type": "application/json"})
            data = json.dumps(payload).encode()
        request = urllib.request.Request(self.url, data=data, headers=headers)
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read())

    def test_check_and_resolve_only_use_the_fixed_recipe_origin_and_build_the_resolved_pin(self):
        versions = self.route_globals["versions"]
        listing = SimpleNamespace(refs=(SimpleNamespace(name="refs/heads/main", commit="a" * 40),),
                                  unknown=False, reason="")
        resolution = SimpleNamespace(commit="a" * 40, unknown=False, reason="")
        with mock.patch.object(versions, "check", return_value=listing) as check, \
                mock.patch.object(versions, "resolve", return_value=resolution) as resolve, \
                mock.patch.object(self.route_globals["work_cli"], "open_runner") as open_runner, \
                mock.patch.object(self.route_globals["runtime_build"], "submit") as submit:
            open_runner.return_value = SimpleNamespace(close=lambda: None)
            submit.return_value = SimpleNamespace(
                start=SimpleNamespace(decision=admission.ADMIT, work=SimpleNamespace(work_id="build-1")),
                warning="")
            status, data = self.request({"action": "check", "recipe": recipes.LLAMA_HIP,
                                         "ref": "main", "card": "0000:03:00.0",
                                         "target": "gfx1201"})
            self.assertEqual(status, 200)
            self.assertEqual(data["refs"], [{"name": "refs/heads/main", "commit": "a" * 40}])
            check.assert_called_once_with(recipes.LLAMA_ORIGIN)
            _status, resolved = self.request({"action": "resolve", "recipe": recipes.LLAMA_HIP,
                                              "ref": "main", "card": "0000:03:00.0",
                                              "target": "gfx1201"})
            resolve.assert_called_once_with(recipes.LLAMA_ORIGIN, "main")
            status, data = self.request({"action": "build", "recipe": recipes.LLAMA_HIP,
                                         "ref": "main", "card": "0000:03:00.0",
                                         "target": "gfx1201", "pinId": resolved["pinId"]})
            self.assertEqual(status, 202)
            self.assertEqual(data["work"], "build-1")
            self.assertEqual(data["href"], "#/runtime/activity")
            pin = submit.call_args.args[1]
            self.assertEqual((pin.recipe, pin.ref, pin.commit, pin.target),
                             (recipes.LLAMA_HIP, "main", "a" * 40, "gfx1201"))

    def test_build_requires_the_current_server_side_pin(self):
        with self.assertRaises(urllib.error.HTTPError) as refused:
            self.request({"action": "build", "recipe": recipes.LLAMA_VULKAN,
                          "ref": "main"})
        self.assertEqual(refused.exception.code, 409)
        self.assertIn("Resolve this version again", refused.exception.read().decode())

    def test_missing_prerequisites_refuse_before_opening_the_shared_runner(self):
        versions = self.route_globals["versions"]
        with mock.patch.object(versions, "resolve", return_value=SimpleNamespace(
                commit="a" * 40, unknown=False, reason="")), \
                mock.patch.object(self.route_globals["runtime_builds"], "prerequisites",
                                  return_value=["glslc"]), \
                mock.patch.object(self.route_globals["work_cli"], "open_runner") as open_runner:
            _status, resolved = self.request({"action": "resolve", "recipe": recipes.LLAMA_VULKAN,
                                              "ref": "main"})
            with self.assertRaises(urllib.error.HTTPError) as refused:
                self.request({"action": "build", "recipe": recipes.LLAMA_VULKAN,
                              "ref": "main", "pinId": resolved["pinId"]})
        self.assertEqual(refused.exception.code, 409)
        self.assertIn("glslc", refused.exception.read().decode())
        open_runner.assert_not_called()

    def test_serving_state_refresh_returns_the_current_state(self):
        with mock.patch.object(self.route_globals["serving_state"], "read",
                               return_value=SimpleNamespace(state="agentic")):
            status, data = self.request({"action": "serving-state"})
        self.assertEqual(status, 200)
        self.assertEqual(data["servingState"], "agentic")

    def test_build_refuses_inputs_that_do_not_match_its_resolved_pin(self):
        versions = self.route_globals["versions"]
        with mock.patch.object(versions, "resolve", return_value=SimpleNamespace(
                commit="a" * 40, unknown=False, reason="")), \
                mock.patch.object(self.route_globals["runtime_build"], "submit") as submit:
            _status, resolved = self.request({"action": "resolve", "recipe": recipes.LLAMA_VULKAN,
                                              "ref": "main"})
            with self.assertRaises(urllib.error.HTTPError) as refused:
                self.request({"action": "build", "recipe": recipes.LLAMA_VULKAN,
                              "ref": "other", "pinId": resolved["pinId"]})
        self.assertEqual(refused.exception.code, 409)
        self.assertIn("Resolve this version again", refused.exception.read().decode())
        submit.assert_not_called()

    def test_input_change_invalidates_a_resolved_pin_on_the_server(self):
        versions = self.route_globals["versions"]
        with mock.patch.object(versions, "resolve", return_value=SimpleNamespace(
                commit="a" * 40, unknown=False, reason="")):
            self.request({"action": "resolve", "recipe": recipes.LLAMA_VULKAN, "ref": "main"})
        self.request({"action": "invalidate"})
        with self.assertRaises(urllib.error.HTTPError) as refused:
            self.request({"action": "build", "recipe": recipes.LLAMA_VULKAN, "ref": "main"})
        self.assertEqual(refused.exception.code, 409)

    def test_failed_version_check_returns_unknown_without_invented_refs(self):
        versions = self.route_globals["versions"]
        with mock.patch.object(versions, "check", return_value=SimpleNamespace(
                refs=(), unknown=True, reason="origin unavailable")):
            _status, data = self.request({"action": "check", "recipe": recipes.LLAMA_VULKAN,
                                          "ref": "main"})
        self.assertTrue(data["unknown"])
        self.assertEqual(data["refs"], [])
        self.assertEqual(data["reason"], "origin unavailable")

    def test_signed_out_requests_do_not_reach_version_discovery(self):
        request = urllib.request.Request(self.url, headers={"Host": ALLOWED_HOST})
        with self.assertRaises(urllib.error.HTTPError) as refused:
            urllib.request.urlopen(request, timeout=5)
        self.assertEqual(refused.exception.code, 401)


if __name__ == "__main__":
    unittest.main()

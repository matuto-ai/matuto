"""Check official versions, through its own Python API against a fixture origin."""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from matuto import recipes, versions


class Origin:
    """A bare origin with a historical commit, branches, a tag and a light tag."""

    def __init__(self, root):
        self.root = Path(root)
        self.path = self.root / "origin.git"
        self.work = self.root / "work"
        self.env = {**os.environ,
                    "GIT_AUTHOR_NAME": "Fixture",
                    "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
                    "GIT_COMMITTER_NAME": "Fixture",
                    "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
                    "GIT_CONFIG_NOSYSTEM": "1",
                    "HOME": str(self.root)}

    def build(self):
        """Create the fixture and return it."""
        self._git("init", "--bare", "-q", str(self.path))
        self._git("init", "-q", str(self.work))
        self._work("commit", "-q", "--allow-empty", "-m", "one")
        self.first = self._rev("HEAD")
        self._work("tag", "-a", "v1", "-m", "one")
        self.tag_object = self._rev("v1")
        self._work("tag", "light")
        self._work("branch", "side", self.first)
        self._work("branch", "dup", self.first)
        self._work("tag", "dup", self.first)
        self._work("commit", "-q", "--allow-empty", "-m", "two")
        self.middle = self._rev("HEAD")
        self._work("commit", "-q", "--allow-empty", "-m", "three")
        self.tip = self._rev("HEAD")
        self._work("branch", "-M", "main")
        self._work("remote", "add", "origin", str(self.path))
        self._work("push", "-q", "--all", "origin")
        self._work("push", "-q", "--tags", "origin")
        self._git("symbolic-ref", "HEAD", "refs/heads/main", cwd=self.path)
        self.other = self.root / "other.git"
        self._git("init", "--bare", "-q", str(self.other))
        self._work("push", "-q", str(self.other), f"{self.first}:refs/heads/main")
        self._git("symbolic-ref", "HEAD", "refs/heads/main", cwd=self.other)
        return self

    def rewritten_config(self):
        """A config that would rewrite this origin to the other repository."""
        path = self.root / "poison.gitconfig"
        path.write_text(f'[url "{self.other}"]\n\tinsteadOf = {self.path}\n')
        return path

    def expected(self):
        """Every ref the origin returns, as the check must report it."""
        return {("HEAD", self.tip),
                ("refs/heads/main", self.tip),
                ("refs/heads/side", self.first),
                ("refs/heads/dup", self.first),
                ("refs/tags/v1", self.first),
                ("refs/tags/light", self.first),
                ("refs/tags/dup", self.first)}

    def _git(self, *arguments, cwd=None):
        done = subprocess.run(["git", *arguments], cwd=cwd or self.root, env=self.env,
                              capture_output=True, text=True)
        if done.returncode != 0:
            raise AssertionError(f"git {arguments}: {done.stderr}")
        return done.stdout.strip()

    def _work(self, *arguments):
        return self._git("-C", str(self.work), *arguments)

    def _rev(self, name):
        return self._work("rev-parse", name)


class FixtureTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.origin = Origin(cls.directory.name).build()

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()


class CheckTest(FixtureTest):
    def test_the_listing_is_exactly_what_the_origin_returned(self):
        listing = versions.check(str(self.origin.path))
        self.assertFalse(listing.unknown, listing.reason)
        self.assertEqual({(ref.name, ref.commit) for ref in listing.refs},
                         self.origin.expected())
        self.assertEqual(len(listing.refs), len(self.origin.expected()))

    def test_a_tag_is_reported_with_the_commit_it_names(self):
        listing = versions.check(str(self.origin.path))
        annotated = [ref for ref in listing.refs if ref.name == "refs/tags/v1"]
        self.assertEqual([ref.commit for ref in annotated], [self.origin.first])

    def test_an_unreachable_origin_is_unknown_with_its_reason(self):
        missing = str(self.origin.root / "missing.git")
        listing = versions.check(missing)
        self.assertTrue(listing.unknown)
        self.assertTrue(listing.reason)
        self.assertEqual(listing.refs, ())

    def test_a_missing_origin_is_unknown_and_runs_no_command(self):
        with mock.patch.object(versions.subprocess, "run",
                               side_effect=AssertionError("ran a check")):
            for origin in (None, ""):
                with self.subTest(origin=origin):
                    listing = versions.check(origin)
                    self.assertTrue(listing.unknown)
                    self.assertTrue(listing.reason)

    def test_a_malformed_answer_is_unknown_and_invents_no_refs(self):
        answered = subprocess.CompletedProcess([], 0, stdout="not a ref line\n", stderr="")
        with mock.patch.object(versions.subprocess, "run", return_value=answered):
            listing = versions.check("https://example.invalid/x.git")
        self.assertTrue(listing.unknown)
        self.assertEqual(listing.refs, ())
        self.assertTrue(listing.reason)

    def test_a_failed_command_carries_its_reason(self):
        answered = subprocess.CompletedProcess([], 128, stdout="", stderr="fatal: nope")
        with mock.patch.object(versions.subprocess, "run", return_value=answered):
            listing = versions.check("https://example.invalid/x.git")
        self.assertTrue(listing.unknown)
        self.assertEqual(listing.reason, "fatal: nope")

    def test_a_configured_url_rewrite_cannot_stand_in_for_the_origin(self):
        poison = self.origin.rewritten_config()
        with mock.patch.dict(os.environ, {"HOME": str(self.origin.root),
                                          "GIT_CONFIG_GLOBAL": str(poison)}):
            listing = versions.check(str(self.origin.path))
        self.assertFalse(listing.unknown, listing.reason)
        self.assertEqual({(ref.name, ref.commit) for ref in listing.refs},
                         self.origin.expected())

    def test_an_inherited_command_line_config_cannot_rewrite_the_origin(self):
        poison = f"'url.{self.origin.other}.insteadOf={self.origin.path}'"
        with mock.patch.dict(os.environ, {"GIT_CONFIG_PARAMETERS": poison}):
            listing = versions.check(str(self.origin.path))
        self.assertFalse(listing.unknown, listing.reason)
        self.assertEqual({(ref.name, ref.commit) for ref in listing.refs},
                         self.origin.expected())

    def test_an_undecodable_answer_is_unknown_with_its_reason(self):
        ref = b"refs/heads/bad-\xff"
        try:
            added = subprocess.run([b"git", b"update-ref", ref, self.origin.first.encode()],
                                   cwd=str(self.origin.path), env=self.origin.env,
                                   capture_output=True)
            self.assertEqual(added.returncode, 0, added.stderr)
            listing = versions.check(str(self.origin.path))
        finally:
            subprocess.run([b"git", b"update-ref", b"-d", ref],
                           cwd=str(self.origin.path), env=self.origin.env,
                           capture_output=True)
        self.assertTrue(listing.unknown)
        self.assertEqual(listing.refs, ())
        self.assertTrue(listing.reason)

    def test_the_one_command_is_git_ls_remote_with_argv_against_the_origin_alone(self):
        origin = "https://example.invalid/o.git"
        answered = subprocess.CompletedProcess(
            [], 0, stdout=f"{self.origin.tip}\trefs/heads/main\n", stderr="")
        poisoned = {"GIT_DIR": "/checkout/.git",
                    "GIT_CONFIG": "/checkout/.gitconfig",
                    "GIT_CONFIG_COUNT": "1",
                    "GIT_CONFIG_KEY_0": "url.x.insteadOf",
                    "GIT_CONFIG_VALUE_0": "https://elsewhere.invalid/",
                    "GIT_CONFIG_PARAMETERS": "'url.x.insteadOf=https://elsewhere.invalid/'"}
        with mock.patch.dict(os.environ, poisoned), \
                mock.patch.object(versions.subprocess, "run", return_value=answered) as run:
            versions.check(origin)
        argv, keywords = run.call_args
        self.assertEqual(argv[0], ["git", "ls-remote", "--", origin])
        self.assertNotEqual(keywords.get("shell"), True)
        self.assertEqual(keywords["cwd"], "/")
        for name in poisoned:
            self.assertNotIn(name, keywords["env"])
        self.assertEqual(keywords["env"]["GIT_CONFIG_GLOBAL"], os.devnull)
        self.assertEqual(keywords["env"]["GIT_CONFIG_NOSYSTEM"], "1")

    def test_the_answer_never_claims_latest_or_up_to_date(self):
        for listing in (versions.check(str(self.origin.path)),
                        versions.check(str(self.origin.root / "missing.git"))):
            self.assertNotIn("latest", repr(listing).lower())
            self.assertNotIn("up to date", repr(listing).lower())


class ResolveTest(FixtureTest):
    def test_a_branch_a_tag_and_a_full_commit_resolve(self):
        origin = str(self.origin.path)
        self.assertEqual(versions.resolve(origin, "main").commit, self.origin.tip)
        self.assertEqual(versions.resolve(origin, "refs/heads/side").commit, self.origin.first)
        self.assertEqual(versions.resolve(origin, "v1").commit, self.origin.first)
        self.assertEqual(versions.resolve(origin, "light").commit, self.origin.first)
        self.assertEqual(versions.resolve(origin, "HEAD").commit, self.origin.tip)
        self.assertEqual(versions.resolve(origin, self.origin.tip).commit, self.origin.tip)
        self.assertEqual(versions.resolve(origin, self.origin.middle).commit,
                         self.origin.middle)

    def test_an_unknown_name_is_refused(self):
        result = versions.resolve(str(self.origin.path), "no-such-ref")
        self.assertIsNone(result.commit)
        self.assertFalse(result.unknown)
        self.assertTrue(result.reason)

    def test_a_name_that_is_both_a_branch_and_a_tag_is_refused(self):
        result = versions.resolve(str(self.origin.path), "dup")
        self.assertIsNone(result.commit)
        self.assertIn("branch and a tag", result.reason)

    def test_an_unreachable_origin_is_unknown_with_its_reason(self):
        result = versions.resolve(str(self.origin.root / "missing.git"), "main")
        self.assertIsNone(result.commit)
        self.assertTrue(result.unknown)
        self.assertTrue(result.reason)

    def test_a_historical_commit_is_confirmed_from_the_origin(self):
        result = versions.resolve(str(self.origin.path), self.origin.middle)
        self.assertEqual(result.commit, self.origin.middle)
        self.assertFalse(result.unknown)
        self.assertEqual(result.reason, "")

    def test_a_historical_commit_is_not_written_into_an_operator_object_store(self):
        operator = self.origin.root / "operator-objects"
        operator.mkdir()
        with mock.patch.dict(os.environ, {"GIT_OBJECT_DIRECTORY": str(operator)}):
            result = versions.resolve(str(self.origin.path), self.origin.middle)
        self.assertEqual(result.commit, self.origin.middle)
        self.assertEqual(list(operator.iterdir()), [])

    def test_a_fetch_timeout_is_unknown(self):
        real_run = subprocess.run

        def fake_run(argv, **keywords):
            if "fetch" in argv:
                raise subprocess.TimeoutExpired(argv, versions.TIMEOUT_SECONDS)
            return real_run(argv, **keywords)

        with mock.patch.object(versions.subprocess, "run", side_effect=fake_run):
            result = versions.resolve(str(self.origin.path), self.origin.middle)
        self.assertIsNone(result.commit)
        self.assertTrue(result.unknown)
        self.assertTrue(result.reason)

    def test_a_failed_fetch_is_unknown_and_keeps_its_cause(self):
        real_run = subprocess.run

        def fake_run(argv, **keywords):
            if "fetch" in argv:
                return subprocess.CompletedProcess(argv, 128, stdout="",
                                                   stderr="fatal: boom")
            return real_run(argv, **keywords)

        with mock.patch.object(versions.subprocess, "run", side_effect=fake_run):
            result = versions.resolve(str(self.origin.path), self.origin.middle)
        self.assertIsNone(result.commit)
        self.assertTrue(result.unknown)
        self.assertIn("boom", result.reason)

    def test_an_origin_lost_during_the_fetch_is_unknown(self):
        real_run = subprocess.run
        moved = self.origin.root / "temporarily-unreachable.git"

        def interrupted(argv, **keywords):
            if "fetch" not in argv:
                return real_run(argv, **keywords)
            self.origin.path.rename(moved)
            try:
                return real_run(argv, **keywords)
            finally:
                moved.rename(self.origin.path)

        with mock.patch.object(versions.subprocess, "run", side_effect=interrupted):
            result = versions.resolve(str(self.origin.path), self.origin.middle)
        self.assertIsNone(result.commit)
        self.assertTrue(result.unknown)
        self.assertTrue(result.reason)

    def test_a_commit_the_origin_does_not_have_is_refused(self):
        result = versions.resolve(str(self.origin.path), "0" * 40)
        self.assertIsNone(result.commit)
        self.assertFalse(result.unknown)
        self.assertTrue(result.reason)

    def test_a_non_commit_object_is_refused(self):
        result = versions.resolve(str(self.origin.path), self.origin.tag_object)
        self.assertIsNone(result.commit)
        self.assertFalse(result.unknown)
        self.assertTrue(result.reason)

    def test_an_unreachable_origin_is_unknown_for_an_explicit_commit(self):
        result = versions.resolve(str(self.origin.root / "missing.git"),
                                  self.origin.tip)
        self.assertIsNone(result.commit)
        self.assertTrue(result.unknown)
        self.assertTrue(result.reason)


class PinTest(unittest.TestCase):
    def test_changing_the_recipe_the_ref_or_the_target_clears_the_pin(self):
        pin = versions.pin(recipes.LLAMA_HIP, "main", "a" * 40, "gfx1201")
        self.assertIs(versions.retained(pin, recipes.LLAMA_HIP, "main", "gfx1201"), pin)
        self.assertIsNone(versions.retained(pin, recipes.LLAMA_VULKAN, "main", None))
        self.assertIsNone(versions.retained(pin, recipes.LLAMA_HIP, "side", "gfx1201"))
        self.assertIsNone(versions.retained(pin, recipes.LLAMA_HIP, "main", "gfx1036"))
        self.assertIsNone(versions.retained(None, recipes.LLAMA_HIP, "main", "gfx1201"))

    def test_a_pin_refuses_an_unknown_recipe_or_an_unresolved_commit(self):
        with self.assertRaises(ValueError):
            versions.pin("not a recipe", "main", "a" * 40)
        with self.assertRaises(ValueError):
            versions.pin(recipes.LLAMA_HIP, "main", "abc")


if __name__ == "__main__":
    unittest.main()

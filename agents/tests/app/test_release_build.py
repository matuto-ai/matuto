"""The release-file builder, run against a fixture git repository."""

import hashlib
import json
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from matuto import release_build, releases


class FixtureRepository:
    """A throwaway git repository with one commit, built and read through git."""

    def __init__(self, directory):
        self.path = Path(directory)
        self._git("init", "-q")

    def _git(self, *arguments):
        return subprocess.run(
            ["git", "-C", str(self.path), "-c", "user.name=Release Test",
             "-c", "user.email=release@test.invalid", *arguments],
            check=True, capture_output=True)

    def commit_file(self, name, content, mode=None):
        target = self.path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        if mode is not None:
            target.chmod(mode)
        return target

    def commit(self, message="release fixture"):
        self._git("add", "-A")
        self._git("commit", "-q", "-m", message)
        return self._git("rev-parse", "HEAD").stdout.decode().strip()

    def attributes(self, text):
        """Local git attributes, which `git archive` would apply and the builder must not."""
        info = self.path / ".git" / "info"
        info.mkdir(parents=True, exist_ok=True)
        (info / "attributes").write_text(text)

    def tracked(self, *paths):
        out = self._git("ls-tree", "-r", "--name-only", "-z", "HEAD", "--", *paths).stdout
        return sorted(name for name in out.decode().split("\0") if name)

    def read(self, name):
        return self._git("show", f"HEAD:{name}").stdout


def tracked_files(repository, *paths):
    return repository.tracked(*paths)


def read_release(path):
    """The archive's payload bytes and its embedded manifest."""
    with tarfile.open(path, "r:gz") as archive:
        members = {member.name: (member.mode, archive.extractfile(member).read())
                   for member in archive if member.isfile()}
    manifest = json.loads(members.pop(release_build.MANIFEST)[1].decode("utf-8"))
    return members, manifest


class ReleaseBuildTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="matuto-release-build-")
        self.repository = FixtureRepository(self.temporary.name)
        self.repository.commit_file("agents/matuto/thing.py", b"print('thing')\n")
        self.repository.commit_file("agents/matuto-lab", b"#!/bin/bash\n", mode=0o755)
        self.repository.commit_file("agents/tests/app/test_thing.py", b"# test\n")
        self.repository.commit_file("wq.py", b"# wq\n")
        self.repository.commit_file("wq.sh", b"#!/bin/bash\n", mode=0o755)
        self.repository.commit_file("LICENSE", b"license\n")
        self.repository.commit_file("docs/MATUTO_LAB.md", b"lab\n")
        self.repository.commit_file("docs/MATUTO_MANAGER.md", b"manager\n")
        self.repository.commit_file("docs/RELEASING.md", b"release guide\n")
        # Files the release must never hold, whichever way the list is written.
        self.repository.commit_file("docs/OTHER.md", b"other\n")
        self.repository.commit_file("docs/evidence/2026-01-01-note.md", b"secret\n")
        self.repository.commit_file("README.md", b"readme\n")
        self.commit = self.repository.commit()
        self.output = Path(self.temporary.name) / "matuto-2026.10.0.release"

    def tearDown(self):
        self.temporary.cleanup()

    def build(self, **overrides):
        commit = overrides.pop("commit", self.commit)
        arguments = {"repository": self.repository.path, "notes": "The first release.",
                     "prerequisites": ["openssh-client"]}
        arguments.update(overrides)
        return release_build.build_release(commit, "2026.10.0", self.output, **arguments)

    def test_a_commit_builds_exactly_the_listed_files_with_matching_checksums(self):
        entry = self.build()
        members, manifest = read_release(self.output)
        expected = tracked_files(self.repository, *release_build.BUILD_PATHS)
        self.assertEqual(sorted(members), expected)
        self.assertEqual(sorted(manifest["files"]), expected)
        self.assertIn("docs/RELEASING.md", members)
        for name in expected:
            self.assertEqual(manifest["files"][name],
                             hashlib.sha256(members[name][1]).hexdigest(), name)
            self.assertEqual(members[name][1], self.repository.read(name), name)
        self.assertNotIn("README.md", members)
        self.assertNotIn("docs/OTHER.md", members)
        self.assertNotIn("docs/evidence/2026-01-01-note.md", members)

    def test_the_printed_size_and_digest_are_the_file_s_own(self):
        entry = self.build()
        data = self.output.read_bytes()
        self.assertEqual(entry["size"], len(data))
        self.assertEqual(entry["size"], self.output.stat().st_size)
        self.assertEqual(entry["sha256"], hashlib.sha256(data).hexdigest())
        self.assertEqual(entry["commit"], self.commit)
        self.assertEqual(entry["version"], "2026.10.0")
        self.assertEqual(entry["notes"], "The first release.")
        self.assertEqual(entry["prerequisites"], ["openssh-client"])
        self.assertFalse(entry["changes_database"])
        self.assertFalse(entry["withdrawn"])

    def test_the_build_uses_the_named_commit_not_uncommitted_changes(self):
        committed = self.repository.read("wq.py")
        (self.repository.path / "wq.py").write_bytes(b"# uncommitted\n")
        self.build()
        members, _ = read_release(self.output)
        self.assertEqual(members["wq.py"][1], committed)

    def test_a_local_export_attribute_never_omits_or_rewrites_a_file(self):
        # `git archive` applies these; the builder reads the commit's tree itself.
        (self.repository.path / "agents/matuto/id.py").write_bytes(b"$Format:%H$\n")
        commit = self.repository.commit("a format placeholder")
        self.repository.attributes(
            "agents/matuto/thing.py export-ignore\nagents/matuto/id.py export-subst\n")
        self.build(commit=commit)
        members, manifest = read_release(self.output)
        self.assertIn("agents/matuto/thing.py", members)
        self.assertEqual(members["agents/matuto/id.py"][1], b"$Format:%H$\n")
        self.assertEqual(manifest["files"]["agents/matuto/thing.py"],
                         hashlib.sha256(members["agents/matuto/thing.py"][1]).hexdigest())

    def test_the_executable_bit_survives(self):
        self.build()
        members, _ = read_release(self.output)
        self.assertTrue(members["wq.sh"][0] & 0o111)
        self.assertFalse(members["wq.py"][0] & 0o111)

    def test_the_same_commit_builds_the_same_bytes(self):
        self.build()
        first = self.output.read_bytes()
        self.build()
        self.assertEqual(self.output.read_bytes(), first)

    def test_an_evidence_path_in_the_list_is_refused(self):
        for path in ("docs/evidence", "docs/evidence/2026-01-01-note.md"):
            with self.subTest(path=path), self.assertRaises(release_build.BuildError) as caught:
                self.build(paths=release_build.BUILD_PATHS + (path,))
            self.assertIn("evidence", str(caught.exception))
            self.assertFalse(self.output.exists())

    def test_an_evidence_path_reached_through_a_listed_directory_is_refused(self):
        with self.assertRaises(release_build.BuildError) as caught:
            self.build(paths=("docs",))
        self.assertIn("docs/evidence", str(caught.exception))
        self.assertFalse(self.output.exists())

    def test_a_missing_listed_path_fails_and_leaves_no_file(self):
        with self.assertRaises(release_build.BuildError) as caught:
            self.build(paths=release_build.BUILD_PATHS + ("docs/NOT_TRACKED.md",))
        self.assertIn("docs/NOT_TRACKED.md", str(caught.exception))
        self.assertFalse(self.output.exists())

    def test_a_listed_path_the_commit_does_not_hold_fails(self):
        with self.assertRaises(release_build.BuildError) as caught:
            self.build(paths=("agents", "docs/empty"))
        self.assertIn("docs/empty", str(caught.exception))

    def test_a_symlink_under_the_list_is_refused(self):
        (self.repository.path / "agents/matuto/thing-link").symlink_to("thing.py")
        commit = self.repository.commit("symlink")
        with self.assertRaises(release_build.BuildError) as caught:
            self.build(commit=commit)
        self.assertIn("ordinary file", str(caught.exception))
        self.assertFalse(self.output.exists())

    def test_an_unwritable_output_location_fails_cleanly(self):
        output = Path(self.temporary.name) / "missing-directory" / "release"
        with self.assertRaises(release_build.BuildError) as caught:
            release_build.build_release(self.commit, "2026.10.0", output,
                                        repository=self.repository.path)
        self.assertIn("cannot be written", str(caught.exception))

    def test_an_unknown_commit_fails_and_leaves_no_file(self):
        with self.assertRaises(release_build.BuildError) as caught:
            self.build(commit="0" * 40)
        self.assertIn("unknown", str(caught.exception))
        self.assertFalse(self.output.exists())

    def test_a_malformed_version_fails_and_leaves_no_file(self):
        for version in ("../1.0.0", "1.0", "1.0.0-rc1"):
            with self.subTest(version=version), self.assertRaises(release_build.BuildError):
                release_build.build_release(self.commit, version, self.output,
                                            repository=self.repository.path)
            self.assertFalse(self.output.exists())

    def test_a_failed_build_leaves_an_existing_output_alone(self):
        self.output.write_bytes(b"an earlier release")
        with self.assertRaises(release_build.BuildError):
            self.build(commit="0" * 40)
        self.assertEqual(self.output.read_bytes(), b"an earlier release")

    def test_the_command_prints_the_signed_list_entry(self):
        completed = subprocess.run(
            [sys.executable, str(Path(release_build.__file__)), "--version", "2026.10.1",
             "--commit", self.commit, "--output", str(self.output),
             "--repository", str(self.repository.path), "--notes", "Fix the thing.",
             "--prerequisite", "openssh-client", "--prerequisite", "curl",
             "--changes-database"],
            capture_output=True)
        self.assertEqual(completed.returncode, 0, completed.stderr.decode())
        entry = json.loads(completed.stdout.decode())
        data = self.output.read_bytes()
        self.assertEqual(entry["size"], len(data))
        self.assertEqual(entry["sha256"], hashlib.sha256(data).hexdigest())
        self.assertEqual(entry["commit"], self.commit)
        self.assertEqual(entry["notes"], "Fix the thing.")
        self.assertEqual(entry["prerequisites"], ["openssh-client", "curl"])
        self.assertTrue(entry["changes_database"])
        self.assertNotIn("key", entry)


class ListExtensionTest(unittest.TestCase):
    """The unsigned list the release workflow signs, against throwaway keys."""

    @classmethod
    def setUpClass(cls):
        cls.keys = tempfile.TemporaryDirectory(prefix="matuto-release-list-keys-")
        cls.trusted = Path(cls.keys.name) / "trusted"
        cls.other = Path(cls.keys.name) / "other"
        for key in (cls.trusted, cls.other):
            subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)],
                           check=True, capture_output=True)
        cls.public_key = Path(f"{cls.trusted}.pub").read_bytes()

    @classmethod
    def tearDownClass(cls):
        cls.keys.cleanup()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="matuto-release-list-")
        self.directory = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def entry(self, version, **overrides):
        record = {"version": version, "commit": "a" * 40, "size": 7, "sha256": "b" * 64,
                  "notes": f"Release {version}.", "prerequisites": ["openssh-client"],
                  "changes_database": False, "withdrawn": False}
        record.update(overrides)
        return record

    def signed(self, document, key=None):
        path = self.directory / "release-list.json"
        path.write_bytes(document)
        Path(f"{path}.sig").unlink(missing_ok=True)
        subprocess.run(["ssh-keygen", "-Y", "sign", "-f", str(key or self.trusted),
                        "-n", releases.SIGNATURE_NAMESPACE, str(path)],
                       check=True, capture_output=True, stdin=subprocess.DEVNULL)
        return document, Path(f"{path}.sig").read_bytes()

    def published(self, number=100, key=None):
        return self.signed(json.dumps({"number": number, "releases": [
            self.entry("1.0.0"), self.entry("1.1.0", withdrawn=True)]}).encode(), key)

    def test_a_new_release_extends_the_verified_list_with_a_higher_number(self):
        previous = self.published()
        document = release_build.extend_list(self.entry("1.1.1"), previous, self.public_key,
                                             now=200)
        reading = releases.read_list(*self.signed(document), self.public_key,
                                     accepted_number=100)
        self.assertFalse(reading.stale)
        self.assertEqual(reading.release_list.number, 200)
        self.assertEqual(json.loads(document)["releases"],
                         json.loads(previous[0])["releases"] + [self.entry("1.1.1")])

    def test_the_first_release_starts_a_list_numbered_from_the_clock(self):
        before = int(time.time())
        document = release_build.extend_list(self.entry("1.0.0"))
        parsed = json.loads(document)
        self.assertEqual(parsed["releases"], [self.entry("1.0.0")])
        self.assertGreaterEqual(parsed["number"], before)

    def test_a_version_not_above_every_listed_one_is_refused(self):
        # 1.1.0 is withdrawn, and still used up.
        for version in ("1.0.0", "0.9.9", "1.1.0", "1.0.5"):
            with self.subTest(version=version), self.assertRaises(release_build.BuildError) as caught:
                release_build.extend_list(self.entry(version), self.published(), self.public_key,
                                          now=200)
            self.assertIn("1.1.0", str(caught.exception))

    def test_a_malformed_version_is_refused(self):
        with self.assertRaises(release_build.BuildError):
            release_build.extend_list(self.entry("1.02.0"), self.published(), self.public_key,
                                      now=200)

    def test_a_previous_list_not_signed_by_the_trusted_key_is_refused(self):
        with self.assertRaises(release_build.BuildError) as caught:
            release_build.extend_list(self.entry("1.2.0"), self.published(key=self.other),
                                      self.public_key, now=200)
        self.assertIn("does not verify", str(caught.exception))

    def test_a_previous_number_not_below_the_clock_is_refused(self):
        for number in (200, 201):
            with self.subTest(number=number), self.assertRaises(release_build.BuildError):
                release_build.extend_list(self.entry("1.2.0"), self.published(number),
                                          self.public_key, now=200)

    def test_the_command_writes_the_list_and_refuses_a_missing_signature(self):
        (self.directory / "repository").mkdir()
        repository = FixtureRepository(self.directory / "repository")
        for name in release_build.BUILD_PATHS:
            repository.commit_file(f"{name}/file" if name == "agents" else name, b"x\n")
        commit = repository.commit()
        previous = self.directory / "previous.json"
        previous.write_bytes(self.published()[0])
        key = self.directory / "release-key.pub"
        key.write_bytes(self.public_key)
        command = [sys.executable, str(Path(release_build.__file__)), "--version", "1.2.0",
                   "--commit", commit, "--output", str(self.directory / "matuto-1.2.0.release"),
                   "--repository", str(repository.path), "--previous-list", str(previous),
                   "--trusted-key", str(key), "--list-output", str(self.directory / "new.json")]
        refused = subprocess.run(command, capture_output=True)
        self.assertEqual(refused.returncode, 1)
        self.assertFalse((self.directory / "new.json").exists())
        Path(f"{previous}.sig").write_bytes((self.directory / "release-list.json.sig").read_bytes())
        completed = subprocess.run(command, capture_output=True)
        self.assertEqual(completed.returncode, 0, completed.stderr.decode())
        listed = json.loads((self.directory / "new.json").read_bytes())["releases"]
        self.assertEqual(listed[-1], json.loads(completed.stdout))


if __name__ == "__main__":
    unittest.main()

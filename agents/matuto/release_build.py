"""Build one Matuto release file from one named source commit.

The explicit list is `BUILD_PATHS`: `agents/`, `wq.py`, `wq.sh`, `LICENSE` and
the three operator guides. Only files tracked in the named commit are read, so an
uncommitted change never enters a release, and any path under `docs/evidence/`
is refused before anything is written, so private evidence can never be
published.

The release file is a gzipped POSIX tar. It holds those files at their
repository paths plus a `RELEASE.json` manifest that lists every held payload
file with its SHA-256; the manifest itself is covered by the file's signed
SHA-256, which is what it cannot list itself. Because the manifest is inside
the file, that signed digest covers it, and the archive metadata is normalized
so the same commit always builds the same bytes. The signing key is not this
tool's business: the build prints the version, source commit, size and SHA-256
plus the notes, prerequisites and database-change flag for the signed list, and
never reads, asks for or uses a signing key. Given the newest published signed
list and the release public key, it also writes the unsigned list that adds
this release, for the release workflow to sign.

Run it from a checkout:

    python3 agents/matuto/release_build.py --version 1.0.0 \\
        --commit <full source SHA> --output matuto-1.0.0.release
"""

import argparse
import gzip
import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from matuto import releases

# The explicit list a release holds; nothing outside it enters the file.
BUILD_PATHS = (
    "agents",
    "wq.py",
    "wq.sh",
    "LICENSE",
    "docs/MATUTO_LAB.md",
    "docs/MATUTO_MANAGER.md",
    "docs/RELEASING.md",
)
# The one private tree a release never holds.
EVIDENCE = ("docs", "evidence")
# The per-file checksum list, inside the file and covered by its signed SHA-256.
MANIFEST = "RELEASE.json"
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


class BuildError(Exception):
    """A refused build, with a reason to show."""


def build_release(commit, version, output, *, repository=None, paths=BUILD_PATHS,
                  notes="", prerequisites=(), changes_database=False):
    """Build one release file from `commit` and return its signed-list entry.

    `commit` is the source commit to read; a malformed version, a missing
    commit, a listed path the commit does not hold and an evidence path are
    refused with `BuildError` before the output exists. The output is written
    in one step, so a failed build leaves no partial file. The returned mapping
    carries the fields the signed release list names for one release.
    """
    _version_key(version)
    repository = Path(repository) if repository is not None else REPOSITORY_ROOT
    for path in paths:
        if _is_evidence(path):
            raise BuildError(
                f"the release may not include {path}: docs/evidence/ is never published")
    full = _resolve_commit(repository, commit)
    files = _committed_files(repository, full, paths)
    data = _archive(files)
    _write_atomically(Path(output), data)
    return {
        "version": version,
        "commit": full,
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "notes": notes,
        "prerequisites": list(prerequisites),
        "changes_database": changes_database,
        "withdrawn": False,
    }


def extend_list(entry, previous=None, trusted_key=None, *, now=None):
    """The unsigned release list that adds `entry` to the newest published one.

    `previous` is that list's bytes and detached signature, or None for the
    first release; it must verify against `trusted_key` before anything in it
    is used. The entry's version must be above every listed version, withdrawn
    ones included. The list number is the current Unix time, so a later list
    numbers above any signed before, a discarded draft's included, with no
    stored state; a previous number not below it is refused.
    """
    version = _version_key(entry["version"])
    number = int(time.time() if now is None else now)
    listed = []
    if previous is not None:
        try:
            known = releases.read_list(*previous, trusted_key).release_list
        except releases.ReleaseError as error:
            raise BuildError(f"the previous release list is refused: {error}") from None
        highest = max((release.version for release in known.releases), key=_version_key)
        if version <= _version_key(highest):
            raise BuildError(
                f"version {entry['version']} is not above {highest}, the highest already listed")
        if number <= known.number:
            raise BuildError(
                f"the previous list number {known.number} is not below the current time {number}")
        listed = json.loads(previous[0])["releases"]
    document = json.dumps({"number": number, "releases": listed + [entry]}, indent=2).encode()
    try:
        releases._parse_list(document)
    except releases.ReleaseError as error:
        raise BuildError(str(error)) from None
    return document + b"\n"


def _version_key(version):
    if not isinstance(version, str) or not releases.VERSION.fullmatch(version):
        raise BuildError(f"the release version {version!r} is not MAJOR.MINOR.PATCH")
    return tuple(int(part) for part in version.split("."))


def _resolve_commit(repository, commit):
    done = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "--verify", "--quiet",
         f"{commit}^{{commit}}"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if done.returncode != 0 or not done.stdout.strip():
        raise BuildError(f"the source commit {commit!r} is unknown")
    return done.stdout.decode("ascii").strip()


def _committed_files(repository, commit, paths):
    """The tracked blobs of `commit` under `paths`, read from the tree itself.

    `git ls-tree` and `git cat-file` read the commit's own tree and blobs, so a
    local `export-ignore` or `export-subst` attribute never omits or rewrites a
    file the way `git archive` would.
    """
    listing = subprocess.run(
        ["git", "-C", str(repository), "ls-tree", "-r", "-z", commit, "--", *paths],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if listing.returncode != 0:
        raise BuildError(
            f"the listed paths cannot be read from {commit[:12]}: {_reason(listing.stderr)}")
    files = {}
    covered = {path.rstrip("/"): False for path in paths}
    for entry in listing.stdout.split(b"\0"):
        if not entry:
            continue
        metadata, name = entry.split(b"\t", 1)
        mode, kind, object_id = metadata.split(b" ")
        name = name.decode("utf-8", "surrogateescape")
        if kind != b"blob" or mode not in (b"100644", b"100755"):
            raise BuildError(f"the release cannot hold {name}: it is not an ordinary file")
        if _is_evidence(name):
            raise BuildError(
                f"the release may not include {name}: docs/evidence/ is never published")
        files[name] = (int(mode, 8), object_id)
        for path, found in covered.items():
            if not found and (name == path or name.startswith(f"{path}/")):
                covered[path] = True
    for path, found in covered.items():
        if not found:
            raise BuildError(f"the listed path {path} is not tracked in {commit[:12]}")
    if not files:
        raise BuildError("the listed paths name no tracked files")
    return _blobs(repository, files)


def _blobs(repository, files):
    """Every tracked blob's committed bytes, by object ID, from one git process."""
    request = b"".join(object_id + b"\n" for _, object_id in files.values())
    done = subprocess.run(
        ["git", "-C", str(repository), "cat-file", "--batch"],
        input=request, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if done.returncode != 0:
        raise BuildError(f"the tracked files cannot be read: {_reason(done.stderr)}")
    output, offset, read = done.stdout, 0, {}
    for name, (mode, object_id) in files.items():
        end = output.index(b"\n", offset)
        header = output[offset:end].split(b" ")
        if header[1:2] != [b"blob"]:
            raise BuildError(
                f"the tracked file {name} cannot be read from {object_id.decode()}: "
                f"{output[offset:end].decode(errors='replace')}")
        size = int(header[2])
        read[name] = (mode, output[end + 1:end + 1 + size])
        offset = end + 1 + size + 1
    return read


def _archive(files):
    """One gzipped tar: the files plus a manifest of their SHA-256, normalized."""
    manifest = json.dumps(
        {"files": {name: hashlib.sha256(content).hexdigest()
                   for name, (_, content) in sorted(files.items())}},
        sort_keys=True, separators=(",", ":")).encode("utf-8")
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.GNU_FORMAT) as archive:
        for name in sorted(files):
            mode, content = files[name]
            _add(archive, name, mode, content)
        _add(archive, MANIFEST, 0o644, manifest)
    return gzip.compress(buffer.getvalue(), mtime=0)


def _add(archive, name, mode, content):
    """Add one regular file with fixed ownership and time, so bytes never vary."""
    info = tarfile.TarInfo(name)
    info.size = len(content)
    info.mode = mode
    info.mtime = 0
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    archive.addfile(info, io.BytesIO(content))


def _write_atomically(output, data):
    """Write the whole file, or nothing: a failure never leaves a partial file."""
    directory = output.parent if str(output.parent) else Path(".")
    try:
        descriptor, temporary = tempfile.mkstemp(prefix=f"{output.name}.", dir=directory)
    except OSError as error:
        raise BuildError(f"the release file cannot be written: {error}") from error
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
        os.replace(temporary, output)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _is_evidence(path):
    parts = Path(path).parts
    return len(parts) >= 2 and parts[0] == EVIDENCE[0] and parts[1] == EVIDENCE[1]


def _reason(stderr):
    lines = [line.strip() for line in stderr.decode("utf-8", "replace").splitlines()
             if line.strip()]
    return lines[-1] if lines else "git archive failed"


def main(argv):
    parser = argparse.ArgumentParser(
        prog="release_build.py",
        description="Build one Matuto release file from one source commit.")
    parser.add_argument("--version", required=True, help="the release version, MAJOR.MINOR.PATCH")
    parser.add_argument("--commit", required=True, help="the full source commit")
    parser.add_argument("--output", required=True, help="the release file to write")
    parser.add_argument("--repository", help="the checkout to read (default: this one)")
    parser.add_argument("--notes", default="", help="the release notes")
    parser.add_argument("--prerequisite", action="append", default=[], dest="prerequisites",
                        help="a prerequisite name; repeat for more than one")
    parser.add_argument("--changes-database", action="store_true",
                        help="mark the release as changing the database")
    parser.add_argument("--list-output",
                        help="also write the unsigned release list that adds this release")
    parser.add_argument("--previous-list",
                        help="the newest published signed list, its .sig beside it; "
                             "omit only for the first release")
    parser.add_argument("--trusted-key", help="the release public-key file")
    args = parser.parse_args(argv)
    if args.previous_list and not (args.list_output and args.trusted_key):
        parser.error("--previous-list needs --list-output and --trusted-key")
    try:
        previous = key = None
        if args.previous_list:
            previous = (Path(args.previous_list).read_bytes(),
                        Path(f"{args.previous_list}.sig").read_bytes())
            key = Path(args.trusted_key).read_bytes()
        entry = build_release(
            args.commit, args.version, args.output, repository=args.repository,
            notes=args.notes, prerequisites=args.prerequisites,
            changes_database=args.changes_database)
        if args.list_output:
            _write_atomically(Path(args.list_output), extend_list(entry, previous, key))
    except (OSError, BuildError) as error:
        print(f"release_build: {error}", file=sys.stderr)
        return 1
    print(json.dumps(entry, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

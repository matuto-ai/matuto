"""Resolve allowed public Hugging Face repositories to immutable file sets."""

import http.client
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from matuto import store

API_BASE = "https://huggingface.co"
TIMEOUT_SECONDS = 15
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
_COMMIT = re.compile(r"^[0-9a-fA-F]{40}$")
_SHA256 = re.compile(r"^(?:sha256:)?([0-9a-fA-F]{64})$")
_SHARD = re.compile(r"^(?P<stem>.+)-(?P<index>[0-9]+)-of-(?P<count>[0-9]+)\.gguf$")


class ResolutionError(Exception):
    """One refusal or unavailable source, with its cause for the operator."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Keep metadata requests on the fixed Hugging Face address."""

    def redirect_request(self, request, response, code, message, headers, new_url):
        return None


class _SocketDeadline:
    """Abort the request socket when its absolute deadline expires."""

    def __init__(self):
        self._lock = threading.Lock()
        self._socket = None
        self._expired = False

    def register(self, connection):
        with self._lock:
            self._socket = connection
            expired = self._expired
        if expired:
            self.abort()

    def abort(self):
        with self._lock:
            self._expired = True
            connection = self._socket
        if connection is not None:
            try:
                connection.shutdown(2)
            except OSError:
                pass
            try:
                connection.close()
            except OSError:
                pass


class _TrackedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, *args, tracker, **kwargs):
        self._tracker = tracker
        super().__init__(*args, **kwargs)

    def _open_socket(self):
        getattr(super(), "connect")()
        self._tracker.register(self.sock)

    connect = _open_socket


class _TrackedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, *args, tracker, **kwargs):
        self._tracker = tracker
        super().__init__(*args, **kwargs)

    def _open_socket(self):
        getattr(super(http.client.HTTPSConnection, self), "connect")()
        self._tracker.register(self.sock)
        server_hostname = self._tunnel_host or self.host
        self.sock = self._context.wrap_socket(
            self.sock, server_hostname=server_hostname, do_handshake_on_connect=False)
        self._tracker.register(self.sock)
        self.sock.do_handshake()

    connect = _open_socket


class _TrackedHTTPHandler(urllib.request.HTTPHandler):
    def __init__(self, tracker):
        super().__init__()
        self._tracker = tracker

    def http_open(self, request):
        return self.do_open(_TrackedHTTPConnection, request, tracker=self._tracker)


class _TrackedHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, tracker):
        super().__init__()
        self._tracker = tracker

    def https_open(self, request):
        return self.do_open(_TrackedHTTPSConnection, request, tracker=self._tracker,
                            context=self._context)


@dataclass(frozen=True)
class File:
    name: str
    size: int
    sha256: str | None
    checksum_status: str
    url: str


@dataclass(frozen=True)
class Resolution:
    repository: str
    revision: str
    files: tuple
    total_size: int


def list_files(connection, repository, revision, *, api_base=API_BASE):
    """List every source file before selection, in the repository's own order."""
    canonical, commit, siblings = _source(connection, repository, revision, api_base)
    return _result(canonical, commit, siblings,
                   {entry["rfilename"] for entry in siblings})


def resolve(connection, repository, revision, selected_files, *, api_base=API_BASE):
    """Resolve selected files after checking origin permission and public metadata.

    `api_base` exists for fixture-server tests only; production callers use the
    fixed Hugging Face origin and there is no setting or environment override.
    """
    if not isinstance(selected_files, (list, tuple)) or not selected_files:
        raise ResolutionError("choose at least one source file")
    requested = list(selected_files)
    if any(not isinstance(name, str) or not _valid_filename(name) for name in requested):
        raise ResolutionError("a selected file name is malformed")
    if len(set(requested)) != len(requested):
        raise ResolutionError("a selected file was named more than once")
    canonical, commit, siblings = _source(connection, repository, revision, api_base)
    by_name = {entry["rfilename"]: entry for entry in siblings}
    missing = [name for name in requested if name not in by_name]
    if missing:
        raise ResolutionError(f"Hugging Face revision {commit} has no selected file {missing[0]!r}")

    required = set(requested)
    for name in requested:
        match = _SHARD.fullmatch(name)
        if match is None:
            continue
        count = int(match.group("count"))
        index = int(match.group("index"))
        if not count or not 1 <= index <= count:
            raise ResolutionError(f"sharded file {name!r} has an invalid shard number")
        siblings_in_set = {}
        for entry in siblings:
            sibling = _SHARD.fullmatch(entry["rfilename"])
            if sibling and (sibling.group("stem"), sibling.group("count")) == (
                    match.group("stem"), match.group("count")):
                shard_index = int(sibling.group("index"))
                if shard_index in siblings_in_set:
                    raise ResolutionError(f"Hugging Face lists shard {shard_index} more than once")
                siblings_in_set[shard_index] = entry["rfilename"]
        if (len(siblings_in_set) != count or min(siblings_in_set, default=0) != 1
                or max(siblings_in_set, default=0) != count):
            raise ResolutionError(f"Hugging Face's shard set for {name!r} is incomplete")
        required.update(siblings_in_set.values())
    return _result(canonical, commit, siblings, required)


def _source(connection, repository, revision, api_base):
    """Return the admitted repository's confirmed immutable revision and files."""
    if (not isinstance(repository, str) or not repository
            or not isinstance(revision, str) or not revision):
        raise ResolutionError("choose a Hugging Face repository and revision")
    if not _valid_repository(repository):
        raise ResolutionError(f"{repository!r} is not a Hugging Face repository name")
    allowed = [dict(row) for row in connection.execute(
        "SELECT kind, name FROM download_origins WHERE enabled = 1")]
    if not any(_admits(origin, repository) for origin in allowed):
        raise ResolutionError(f"Hugging Face repository {repository!r} is not admitted by a download origin")
    _validate_api_base(api_base)

    metadata = _metadata(api_base, repository, revision)
    _public_metadata(metadata)
    canonical = _repository_id(metadata)
    if canonical.casefold() != repository.casefold():
        raise ResolutionError(
            f"Hugging Face resolved {repository!r} to a different repository {canonical!r}")
    if not any(_admits(origin, canonical) for origin in allowed):
        raise ResolutionError(f"Hugging Face repository {canonical!r} is not admitted by a download origin")
    commit = metadata.get("sha")
    if not isinstance(commit, str) or not _COMMIT.fullmatch(commit):
        raise ResolutionError("Hugging Face returned no full immutable commit")
    commit = commit.lower()
    if _COMMIT.fullmatch(revision) and revision.lower() != commit:
        raise ResolutionError(f"Hugging Face did not resolve the requested commit {revision.lower()}")

    pinned = _metadata(api_base, repository, commit)
    _public_metadata(pinned)
    if _repository_id(pinned).casefold() != canonical.casefold():
        raise ResolutionError("Hugging Face returned metadata for a different repository")
    confirmed = pinned.get("sha")
    if not isinstance(confirmed, str) or confirmed.lower() != commit:
        raise ResolutionError(f"Hugging Face did not confirm commit {commit}")
    return canonical, commit, _siblings(pinned)


def _result(repository, commit, siblings, required):
    files = []
    encoded_repo = "/".join(urllib.parse.quote(part, safe="") for part in repository.split("/"))
    for entry in siblings:
        name = entry["rfilename"]
        if name not in required:
            continue
        size = entry.get("size")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise ResolutionError(f"Hugging Face did not give a valid size for {name!r}")
        digest = _published_sha256(entry, name)
        encoded_name = "/".join(urllib.parse.quote(part, safe="") for part in name.split("/"))
        url = f"{API_BASE}/{encoded_repo}/resolve/{commit}/{encoded_name}"
        files.append(File(name, size, digest,
                          "published SHA-256" if digest else "no published SHA-256", url))
    return Resolution(repository, commit, tuple(files), sum(item.size for item in files))


def _admits(origin, repository):
    if origin["kind"] == "repository":
        return origin["name"].casefold() == repository.casefold()
    return (origin["kind"] == "organisation"
            and repository.partition("/")[0].casefold() == origin["name"].casefold())


def _valid_repository(repository):
    parts = repository.split("/")
    return len(parts) == 2 and all(
        part and part not in (".", "..") and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}", part)
        for part in parts)


def _valid_filename(name):
    return (name and not name.startswith("/") and "\\" not in name
            and all(part not in ("", ".", "..") for part in name.split("/")))


def _validate_api_base(api_base):
    if not isinstance(api_base, str):
        raise ResolutionError("the Hugging Face API address is invalid")
    parsed = urllib.parse.urlsplit(api_base)
    try:
        port = parsed.port
    except ValueError:
        raise ResolutionError("the Hugging Face API address is invalid") from None
    if (parsed.scheme not in ("https", "http") or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment):
        raise ResolutionError("the Hugging Face API address is invalid")
    local = parsed.hostname in ("127.0.0.1", "::1", "localhost")
    if local:
        return
    if parsed.scheme != "https" or parsed.hostname != "huggingface.co" or port is not None:
        raise ResolutionError("Hugging Face requests must use the fixed HTTPS host")


def _metadata(api_base, repository, revision):
    path = "/api/models/" + "/".join(
        urllib.parse.quote(part, safe="") for part in repository.split("/"))
    path += "/revision/" + urllib.parse.quote(revision, safe="") + "?blobs=true"
    url = api_base.rstrip("/") + path
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    deadline = time.monotonic() + TIMEOUT_SECONDS
    tracker = _SocketDeadline()
    timer = threading.Timer(TIMEOUT_SECONDS, tracker.abort)
    timer.daemon = True
    timer.start()
    try:
        return _metadata_response(request, deadline, tracker)
    finally:
        timer.cancel()


def _metadata_response(request, deadline, tracker):
    opener = urllib.request.build_opener(
        _NoRedirect(), _TrackedHTTPHandler(tracker), _TrackedHTTPSHandler(tracker))
    try:
        with opener.open(request, timeout=TIMEOUT_SECONDS) as response:
            length = response.headers.get("Content-Length")
            if length is not None:
                try:
                    announced_size = int(length)
                except ValueError:
                    raise ResolutionError("Hugging Face returned an invalid content length") from None
                if announced_size < 0:
                    raise ResolutionError("Hugging Face returned an invalid content length")
                if announced_size > MAX_RESPONSE_BYTES:
                    raise ResolutionError("Hugging Face's metadata answer is too large")
            raw = _read_limited(response, deadline)
    except urllib.error.HTTPError as error:
        if 300 <= error.code < 400:
            raise ResolutionError(
                f"Hugging Face redirected the metadata request (HTTP {error.code}); "
                "redirects are not followed") from None
        try:
            body = _read_limited(error, deadline)
            if time.monotonic() >= deadline:
                raise TimeoutError("the error detail exceeded its deadline")
            reason = _source_reason(body, error.code)
        except TimeoutError:
            reason = f"HTTP {error.code}; timed out reading the source's error detail"
        except ResolutionError as failure:
            reason = f"HTTP {error.code}; {failure}"
        except OSError as failure:
            reason = f"HTTP {error.code}; could not read the source's error detail: {failure}"
        raise ResolutionError(f"Hugging Face refused the repository or revision: {reason}") from None
    except urllib.error.URLError as error:
        if time.monotonic() >= deadline:
            raise ResolutionError(f"Hugging Face did not answer within {TIMEOUT_SECONDS} seconds") from None
        raise ResolutionError(f"Hugging Face could not be reached: {error.reason}") from None
    except http.client.HTTPException as error:
        if time.monotonic() >= deadline:
            raise ResolutionError(f"Hugging Face did not answer within {TIMEOUT_SECONDS} seconds") from None
        raise ResolutionError(f"Hugging Face returned an invalid HTTP response: {error}") from None
    except TimeoutError:
        raise ResolutionError(f"Hugging Face did not answer within {TIMEOUT_SECONDS} seconds") from None
    except OSError as error:
        if time.monotonic() >= deadline:
            raise ResolutionError(f"Hugging Face did not answer within {TIMEOUT_SECONDS} seconds") from None
        raise ResolutionError(f"Hugging Face could not be read: {error}") from None
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        raise ResolutionError("Hugging Face returned malformed JSON metadata") from None
    if not isinstance(document, dict):
        raise ResolutionError("Hugging Face returned metadata that is not an object")
    return document


def _read_limited(response, deadline):
    """Read at most the metadata limit before an absolute elapsed-time deadline."""
    # The request's watchdog bounds blocking reads, including headers and chunk framing.
    body = bytearray()
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("the metadata response exceeded its deadline")
        chunk = response.read1(min(65536, MAX_RESPONSE_BYTES + 1 - len(body)))
        if time.monotonic() >= deadline:
            raise TimeoutError("the metadata response exceeded its deadline")
        if not chunk:
            return bytes(body)
        body.extend(chunk)
        if len(body) > MAX_RESPONSE_BYTES:
            raise ResolutionError("Hugging Face's metadata answer is too large")


def _source_reason(raw, status):
    try:
        body = json.loads(raw.decode("utf-8"))
        detail = body.get("error") or body.get("detail") if isinstance(body, dict) else None
        if isinstance(detail, str) and detail.strip():
            return detail.strip()[:500]
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        pass
    return f"HTTP {status}"


def _public_metadata(document):
    if document.get("private") is True:
        raise ResolutionError("Hugging Face reports that this repository is private")
    if "private" not in document or "gated" not in document:
        raise ResolutionError("Hugging Face did not confirm that this repository is public")
    if document["gated"] not in (False, None):
        raise ResolutionError("Hugging Face reports that this repository is gated")
    if document["private"] is not False:
        raise ResolutionError("Hugging Face did not confirm that this repository is public")


def _repository_id(document):
    identity = document.get("id") or document.get("modelId")
    if not isinstance(identity, str) or not _valid_repository(identity):
        raise ResolutionError("Hugging Face returned no valid repository identity")
    return identity


def _siblings(document):
    entries = document.get("siblings")
    if not isinstance(entries, list):
        raise ResolutionError("Hugging Face returned no valid file list")
    names = set()
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("rfilename"), str):
            raise ResolutionError("Hugging Face returned a malformed file entry")
        name = entry["rfilename"]
        if not _valid_filename(name) or name in names:
            raise ResolutionError(f"Hugging Face returned a malformed or duplicate file name {name!r}")
        names.add(name)
    return entries


def _published_sha256(entry, name):
    lfs = entry.get("lfs")
    if lfs is None:
        return None
    if not isinstance(lfs, dict):
        raise ResolutionError(f"Hugging Face returned a malformed checksum for {name!r}")
    value = lfs.get("sha256") or lfs.get("oid")
    if value is None:
        return None
    if not isinstance(value, str) or not (match := _SHA256.fullmatch(value)):
        raise ResolutionError(f"Hugging Face returned a non-SHA-256 checksum for {name!r}")
    return match.group(1).lower()

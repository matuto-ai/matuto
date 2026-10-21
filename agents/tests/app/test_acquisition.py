"""Hugging Face resolution and origin permissions through their Python APIs."""

import http.client
import json
import shutil
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit
from urllib.request import Request

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from matuto import acquisition, origins, store  # noqa: E402


COMMIT = "0123456789abcdef0123456789abcdef01234567"
ORG = "unsloth"
REPOSITORY = "unsloth/model"


def sibling(name, size=10, sha="a" * 64):
    entry = {"rfilename": name, "size": size}
    if sha is not None:
        entry["lfs"] = {"sha256": sha}
    return entry


class FixtureHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "fixture"

    def do_GET(self):
        self.server.requests.append((self.path, dict(self.headers)))
        if self.server.slow_headers:
            raw = json.dumps(self.server.branch_document).encode()
            response = (b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                        + f"Content-Length: {len(raw)}\r\nConnection: close\r\n\r\n".encode())
            try:
                for byte in response:
                    self.wfile.write(bytes((byte,)))
                    self.wfile.flush()
                    time.sleep(0.01)
                self.wfile.write(raw)
                self.wfile.flush()
            except OSError:
                pass
            return
        if self.server.slow_chunk_line:
            try:
                self.wfile.write(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n"
                                 b"Connection: close\r\n\r\n")
                self.wfile.flush()
                for byte in b"1;" + b"x" * 80 + b"\r\n":
                    self.wfile.write(bytes((byte,)))
                    self.wfile.flush()
                    time.sleep(0.01)
            except OSError:
                pass
            return
        if self.server.redirect:
            self.send_response(302)
            self.send_header("Location", self.server.redirect)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if self.server.delay:
            time.sleep(self.server.delay)
        if self.server.response is None:
            path = unquote(urlsplit(self.path).path)
            if path.endswith("/revision/main") or path.endswith("/revision/v1"):
                status, document = 200, self.server.branch_document
            elif path.endswith("/revision/" + COMMIT):
                status, document = 200, self.server.commit_document
            else:
                status, document = 404, {"error": "Repository or revision not found"}
        else:
            status, document = self.server.response
        raw = document if isinstance(document, bytes) else json.dumps(document).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        try:
            chunk_size = self.server.stream_chunk_size or len(raw) or 1
            for offset in range(0, len(raw), chunk_size):
                self.wfile.write(raw[offset:offset + chunk_size])
                self.wfile.flush()
                if self.server.stream_delay:
                    time.sleep(self.server.stream_delay)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, _format, *_args):
        pass


class AcquisitionTest(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        self.saved = store.open_store(self.home / "config")
        self.connection = self.saved.connection()
        self.org_origin = origins.add(self.connection, "organisation", ORG)
        files = [sibling("model.gguf", 10), sibling("tokenizer.json", 2, None)]
        self.document = {"id": REPOSITORY, "private": False, "gated": False,
                         "sha": COMMIT, "siblings": files}
        self.http = ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
        self.http.daemon_threads = True
        self.http.requests = []
        self.http.delay = 0
        self.http.slow_headers = False
        self.http.slow_chunk_line = False
        self.http.redirect = None
        self.http.stream_chunk_size = 0
        self.http.stream_delay = 0
        self.http.response = None
        self.http.branch_document = dict(self.document, sha=COMMIT)
        self.http.commit_document = self.document
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)
        self.base = f"http://127.0.0.1:{self.http.server_port}"

    def close_server(self):
        self.http.shutdown()
        self.http.server_close()
        self.thread.join(timeout=2)
        self.saved.close()

    def resolve(self, files=("model.gguf", "tokenizer.json"), revision="main"):
        return acquisition.resolve(self.connection, REPOSITORY, revision, list(files),
                                   api_base=self.base)

    def test_origins_add_list_and_remove_and_malformed_origin_refuses(self):
        repo = origins.add(self.connection, "repository", "someone/model", "notes")
        self.assertEqual(repo["name"], "someone/model")
        with self.assertRaisesRegex(store.StoreError, "already configured"):
            origins.add(self.connection, "repository", "someone/model")
        with self.assertRaisesRegex(store.StoreError, "already configured"):
            origins.add(self.connection, "repository", "Someone/Model")
        self.assertEqual([item["name"] for item in origins.list_all(self.connection)],
                         ["unsloth", "someone/model"])
        for kind, name in (("organisation", "owner/repository"),
                           ("repository", "owner"), ("organisation", "../bad"),
                           ("repository", "owner/../bad"), ("repository", "owner/name?x")):
            with self.subTest(kind=kind, name=name), self.assertRaises(store.StoreError):
                origins.add(self.connection, kind, name)
        origins.remove(self.connection, repo["origin_id"])
        self.assertEqual([item["name"] for item in origins.list_all(self.connection)], [ORG])
        with self.assertRaises(store.StoreError):
            origins.remove(self.connection, repo["origin_id"])
        restored = origins.add(self.connection, "repository", "someone/model")
        self.assertEqual(restored["origin_id"], repo["origin_id"])
        self.assertEqual({item["name"] for item in origins.list_all(self.connection)},
                         {ORG, "someone/model"})

    def test_inventory_lists_every_file_in_source_order_before_selection(self):
        result = acquisition.list_files(self.connection, REPOSITORY, "main", api_base=self.base)
        self.assertEqual(result.revision, COMMIT)
        self.assertEqual([file.name for file in result.files], [
            entry["rfilename"] for entry in self.document["siblings"]])
        self.assertEqual(result.total_size, 12)
        self.assertTrue(all("/resolve/" + COMMIT + "/" in file.url for file in result.files))

    def test_organisation_and_repository_origins_admit_only_exact_owners(self):
        self.assertEqual(self.resolve().repository, REPOSITORY)
        self.assertEqual(acquisition.resolve(
            self.connection, "UNSLOTH/model", "main", ["model.gguf"],
            api_base=self.base).repository, REPOSITORY)
        for repository in ("unsloth-tools/model", "unrelated/model"):
            with self.subTest(repository=repository), self.assertRaises(acquisition.ResolutionError):
                acquisition.resolve(self.connection, repository, "main", ["model.gguf"],
                                    api_base=self.base)
        origins.add(self.connection, "repository", "someone/model")
        self.http.branch_document = dict(self.document, id="someone/model")
        self.http.commit_document = dict(self.document, id="someone/model")
        result = acquisition.resolve(self.connection, "someone/model", "main", ["model.gguf"],
                                     api_base=self.base)
        self.assertEqual(result.repository, "someone/model")
        with self.assertRaises(acquisition.ResolutionError):
            acquisition.resolve(self.connection, "someone-else/model", "main", ["model.gguf"],
                                api_base=self.base)
        origins.add(self.connection, "organisation", "someone")
        self.http.branch_document = dict(self.document, id="someone/another-model")
        self.http.commit_document = dict(self.document, id="someone/another-model")
        result = acquisition.resolve(self.connection, "someone/another-model", "main", ["model.gguf"],
                                     api_base=self.base)
        self.assertEqual(result.repository, "someone/another-model")

    def test_branch_and_tag_pin_files_and_addresses_to_confirmed_full_commit(self):
        for choice in ("main", "v1", COMMIT):
            with self.subTest(choice=choice):
                result = self.resolve(revision=choice)
                self.assertEqual(result.revision, COMMIT)
                self.assertEqual(result.total_size, 12)
                self.assertEqual([f.name for f in result.files], ["model.gguf", "tokenizer.json"])
                self.assertEqual(result.files[0].sha256, "a" * 64)
                self.assertEqual(result.files[1].sha256, None)
                self.assertEqual(result.files[1].checksum_status, "no published SHA-256")
                self.assertIn("/resolve/" + COMMIT + "/", result.files[0].url)
        self.http.response = (200, self.document)
        with self.assertRaisesRegex(acquisition.ResolutionError, "requested commit"):
            self.resolve(revision="f" * 40)
        self.http.response = None
        reversed_files = list(reversed(self.document["siblings"]))
        self.http.branch_document = dict(self.document, siblings=reversed_files)
        self.http.commit_document = dict(self.document, siblings=reversed_files)
        ordered = self.resolve()
        self.assertEqual([file.name for file in ordered.files],
                         [entry["rfilename"] for entry in reversed_files])
        metadata_paths = [path for path, _headers in self.http.requests]
        self.assertTrue(any(path.endswith("/revision/main?blobs=true") for path in metadata_paths))
        self.assertTrue(any(path.endswith("/revision/v1?blobs=true") for path in metadata_paths))
        self.assertTrue(all("authorization" not in {k.lower() for k in headers}
                            for _path, headers in self.http.requests))

    def test_selecting_one_shard_requires_the_complete_shard_set(self):
        shards = [sibling(f"model-{i}-of-3.gguf", i) for i in range(1, 4)]
        self.http.commit_document = dict(self.document, siblings=shards)
        self.http.branch_document = self.http.commit_document
        result = self.resolve(("model-2-of-3.gguf",))
        self.assertEqual([file.name for file in result.files],
                         [f"model-{i}-of-3.gguf" for i in range(1, 4)])
        self.assertEqual(result.total_size, 6)
        self.http.commit_document = dict(self.document, siblings=shards[:2])
        self.http.branch_document = self.http.commit_document
        with self.assertRaisesRegex(acquisition.ResolutionError, "incomplete"):
            self.resolve(("model-2-of-3.gguf",))

    def test_private_gated_and_missing_repositories_are_refused_with_cause(self):
        cases = ((401, {"error": "private repository"}, "private repository"),
                 (403, {"error": "gated repository"}, "gated repository"),
                 (404, {"error": "repository missing"}, "repository missing"))
        for status, body, reason in cases:
            with self.subTest(status=status):
                self.http.response = (status, body)
                with self.assertRaisesRegex(acquisition.ResolutionError, reason):
                    self.resolve()
        self.http.response = None
        self.http.branch_document = dict(self.document, private=True)
        with self.assertRaisesRegex(acquisition.ResolutionError, "private"):
            self.resolve()
        self.http.branch_document = dict(self.document, gated="auto")
        with self.assertRaisesRegex(acquisition.ResolutionError, "gated"):
            self.resolve()

    def test_failure_malformed_oversized_and_timeout_answers_refuse_without_partial_result(self):
        self.http.response = (500, {"error": "upstream unavailable"})
        with self.assertRaisesRegex(acquisition.ResolutionError, "upstream unavailable"):
            self.resolve()
        self.http.response = (200, {"private": False, "gated": False, "sha": "bad"})
        with self.assertRaises(acquisition.ResolutionError):
            self.resolve()
        self.http.response = (200, b"[" * 4000 + b"]" * 4000)
        # Refuse arrays whether parsing reaches the type check or its depth limit.
        with self.assertRaises(acquisition.ResolutionError):
            self.resolve()
        self.http.response = (200, b"{not-json")
        with self.assertRaisesRegex(acquisition.ResolutionError, "malformed JSON"):
            self.resolve()
        self.http.response = (200, b"{" + b" " * acquisition.MAX_RESPONSE_BYTES)
        with self.assertRaisesRegex(acquisition.ResolutionError, "too large"):
            self.resolve()
        self.http.response = None
        self.http.delay = 0.15
        with mock.patch.object(acquisition, "TIMEOUT_SECONDS", 0.02):
            with self.assertRaisesRegex(acquisition.ResolutionError, "did not answer"):
                self.resolve()

    def test_malformed_file_metadata_and_bad_checksums_refuse(self):
        for entries in ([{"rfilename": "../escape", "size": 1}],
                        [sibling("model.gguf", "10")],
                        [sibling("model.gguf", 10, "not-sha256")]):
            with self.subTest(entries=entries):
                self.http.commit_document = dict(self.document, siblings=entries)
                self.http.branch_document = self.http.commit_document
                with self.assertRaises(acquisition.ResolutionError):
                    self.resolve()

    def test_response_can_close_when_its_content_length_is_consumed(self):
        read1 = http.client.HTTPResponse.read1

        def close_at_end(response, amount=None):
            data = read1(response, amount)
            if response.length == 0:
                response.close()
            return data

        with mock.patch.object(http.client.HTTPResponse, "read1", close_at_end):
            self.assertEqual(self.resolve().revision, COMMIT)
            self.http.response = (500, {"error": "upstream unavailable"})
            with self.assertRaisesRegex(acquisition.ResolutionError, "upstream unavailable"):
                self.resolve()

    def test_elapsed_time_is_bounded_even_when_the_server_keeps_sending(self):
        body = json.dumps(self.document).encode()
        self.http.response = (200, body)
        self.http.stream_chunk_size = 20
        self.http.stream_delay = 0.03
        started = time.monotonic()
        with mock.patch.object(acquisition, "TIMEOUT_SECONDS", 0.06):
            with self.assertRaisesRegex(acquisition.ResolutionError, "did not answer"):
                self.resolve()
        self.assertLess(time.monotonic() - started, 0.3)

    def test_a_slow_http_error_body_still_becomes_a_resolution_refusal(self):
        self.http.response = (500, {"error": "upstream unavailable"})
        self.http.stream_chunk_size = 1
        self.http.stream_delay = 0.15
        started = time.monotonic()
        with mock.patch.object(acquisition, "TIMEOUT_SECONDS", 0.06):
            with self.assertRaises(acquisition.ResolutionError):
                self.resolve()
        self.assertLess(time.monotonic() - started, 0.3)

    def test_absolute_deadline_covers_headers_and_chunk_extensions(self):
        for mode in ("slow_headers", "slow_chunk_line"):
            with self.subTest(mode=mode):
                setattr(self.http, mode, True)
                started = time.monotonic()
                with mock.patch.object(acquisition, "TIMEOUT_SECONDS", 0.06):
                    with self.assertRaisesRegex(acquisition.ResolutionError, "did not answer"):
                        self.resolve()
                self.assertLess(time.monotonic() - started, 0.3)
                setattr(self.http, mode, False)

    def test_metadata_redirect_is_refused_without_following_the_other_host(self):
        self.http.redirect = "https://example.com/elsewhere"
        before = len(self.http.requests)
        with self.assertRaisesRegex(acquisition.ResolutionError, "redirected"):
            self.resolve()
        self.assertEqual(len(self.http.requests) - before, 1)

    def test_https_dispatch_uses_a_modern_connection_signature(self):
        handler = acquisition._TrackedHTTPSHandler(acquisition._SocketDeadline())
        if hasattr(handler, "_check_hostname"):
            del handler._check_hostname

        def compatible_init(connection, host, timeout=None, source_address=None,
                            context=None, blocksize=8192):
            connection.host = host
            connection.timeout = timeout
            connection.source_address = source_address
            connection._context = context
            connection._tunnel_host = None

        request = Request("https://huggingface.co/api/models/unsloth/model")
        with mock.patch.object(http.client.HTTPSConnection, "__init__", compatible_init), \
                mock.patch.object(handler, "do_open", side_effect=lambda cls, req, **kwargs:
                                  cls(req.host, timeout=1, **kwargs)) as do_open:
            handler.https_open(request)
        self.assertNotIn("check_hostname", do_open.call_args.kwargs)
        self.assertIn("context", do_open.call_args.kwargs)

    def test_unknown_selected_file_and_bad_fixed_host_override_are_refused(self):
        with self.assertRaisesRegex(acquisition.ResolutionError, "no selected file"):
            self.resolve(("missing.gguf",))
        for base in ("http://example.com", "https://example.com",
                     "https://huggingface.co:444", "https://user:pass@huggingface.co"):
            with self.subTest(base=base), self.assertRaises(acquisition.ResolutionError):
                acquisition.resolve(self.connection, REPOSITORY, "main", ["model.gguf"],
                                    api_base=base)


if __name__ == "__main__":
    unittest.main()

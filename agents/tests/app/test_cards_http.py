"""Settings › Cards over the protected manager HTTP interface."""

import json
import os
import sys
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

AGENTS = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(AGENTS))
sys.path.insert(0, str(Path(__file__).parent))

from matuto import admission, live_records, login, sessions, serving_state, store  # noqa: E402
from matuto.web import ALLOWED_HOST, MANAGER_ORIGIN, SESSION_COOKIE, build_server  # noqa: E402
from test_record_validation import GPU, RecordTestCase  # noqa: E402

OTHER_GPU = "0000:04:00.0"
ATTRS = {"vendor": "0x1002", "device": "0x747e", "class": "0x030000",
         "subsystem_vendor": "0x1002", "subsystem_device": "0x0e1b"}


class CardsHttpTest(RecordTestCase):
    def setUp(self):
        super().setUp()
        self.inventory = {"state": "completed", "devices": [
            {"pciAddress": GPU, **ATTRS, "displayName": "Radeon R9700",
             "vramBytes": 34359738368, "offered": False},
            {"pciAddress": OTHER_GPU, **ATTRS, "device": "0x747f",
             "displayName": "Radeon Other", "vramBytes": 16000000000, "offered": True}],
            "reason": None, "warning": None}
        self.detect_patch = mock.patch("matuto.cards.detect", side_effect=self.detect)
        self.detect_patch.start()
        self.addCleanup(self.detect_patch.stop)
        environment = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.config)})
        environment.start(); self.addCleanup(environment.stop)
        login.create_login("operator", "secret", "secret")
        self.token, _expiry = sessions.start_session("operator", "secret", config_root=self.config)
        self.server = build_server(port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.thread.join, 5)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.address = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.csrf = self.server.RequestHandlerClass.csrf
        self.opener = urllib.request.build_opener()

    def detect(self, opened, **_kwargs):
        devices = [dict(item) for item in self.inventory["devices"]]
        used = {row[0] for row in opened.connection().execute("SELECT gpu_identity FROM cards")}
        for device in devices:
            device["offered"] = device["pciAddress"] not in used
        cards = []
        for row in opened.connection().execute("SELECT * FROM cards ORDER BY card_id"):
            card = dict(row, detected=row["gpu_identity"] in {d["pciAddress"] for d in devices})
            cards.append(card)
        return {**self.inventory, "devices": devices, "cards": cards}

    def request(self, body=None):
        headers = {"Host": ALLOWED_HOST, "Cookie": f"{SESSION_COOKIE}={self.token}"}
        data = None
        method = "GET"
        if body is not None:
            method = "POST"
            headers.update({"Origin": MANAGER_ORIGIN, "X-CSRF-Token": self.csrf,
                            "Content-Type": "application/json"})
            data = json.dumps(body).encode()
        request = urllib.request.Request(self.address + "/api/cards", data=data,
                                         method=method, headers=headers)
        try:
            response = self.opener.open(request, timeout=5)
            return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    def card_payload(self, **changes):
        return {**store.read_card(self.store, "card-r9"), **changes}

    def test_cards_stylesheet_is_served_as_css(self):
        request = urllib.request.Request(self.address + "/cards.css", headers={
            "Host": ALLOWED_HOST, "Cookie": f"{SESSION_COOKIE}={self.token}"})
        with self.opener.open(request, timeout=5) as response:
            self.assertEqual(response.headers.get_content_type(), "text/css")
            self.assertTrue(response.read())

    def test_list_reports_card_detection_and_add_uses_only_a_detected_gpu(self):
        status, listing = self.request()
        self.assertEqual(status, 200)
        card = listing["cards"][0]
        self.assertEqual((card["gpu_name"], card["gpu_identity"], card["detected"]),
                         ("Radeon R9700", GPU, True))
        selected = next(device for device in listing["devices"] if device["pciAddress"] == OTHER_GPU)
        status, result = self.request({"action": "add", "card_id": "card-other",
            "name": "Other card", "gpu_identity": OTHER_GPU, "gpu": selected,
            "vram_ceiling_bytes": 16000000000, "default_gpu_layers": "all",
            "smoke_output_tokens": 16, "bench_prompt_tokens": 4096,
            "bench_generation_tokens": 512, "bench_repetitions": 5, "notes": ""})
        self.assertEqual(status, 201, result)
        self.assertEqual(result["card"]["gpu_identity"], OTHER_GPU)
        self.assertEqual(self.request({"action": "add", "card_id": "typed",
            "name": "Typed", "gpu_identity": "0000:05:00.0", "gpu": "0000:05:00.0",
            "vram_ceiling_bytes": 1000, "default_gpu_layers": "all",
            "smoke_output_tokens": 1, "bench_prompt_tokens": 1,
            "bench_generation_tokens": 1, "bench_repetitions": 1, "notes": ""})[0], 409)

    def test_edit_review_lists_only_served_change_and_affected_profiles_and_bundles(self):
        self.insert_language_profile()
        bundle = self.bundle()
        store.save_record(self.store, bundle)
        edited = self.card_payload(default_gpu_layers=128)
        status, response = self.request({"action": "preview", **edited})
        self.assertEqual(status, 200, response)
        preview = response["preview"]
        self.assertEqual(preview["kind"], "served-change")
        self.assertEqual([(row["field"], row["old"], row["new"])
                          for row in preview["changed_values"]],
                         [("default_gpu_layers", 99, 128)])
        self.assertEqual({(item["kind"], item["id"], item["name"])
                          for item in preview["affected"]}, {
            ("language", "prof-coder", "Qwen3.6 Coder 192K"),
            ("bundle", "bundle-voice", "Voice 192K")})
        status, refusal = self.request({"action": "save", **edited})
        self.assertEqual(status, 409)
        self.assertIn("Review the changed card values", refusal["error"])
        self.assertEqual(store.read_card(self.store, "card-r9")["default_gpu_layers"], 99)
        status, result = self.request({"action": "save", **edited, "confirm": True,
                                       "review_token": response["review_token"]})
        self.assertEqual(status, 200, result)
        self.assertEqual(result["card"]["default_gpu_layers"], 128)

        moved = self.card_payload(gpu_identity=OTHER_GPU, vram_ceiling_bytes=16000000000)
        status, moved_preview = self.request({"action": "preview", **moved})
        self.assertEqual(status, 200, moved_preview)
        self.assertEqual([(row["field"], row["old"], row["new"])
                          for row in moved_preview["preview"]["changed_values"]],
                         [("gpu_identity", GPU, OTHER_GPU)])
        self.assertEqual({item["id"] for item in moved_preview["preview"]["affected"]},
                         {"prof-coder", "prof-breeze", "bundle-voice"})
        status, result = self.request({"action": "save", **moved, "confirm": True,
                                       "review_token": moved_preview["review_token"]})
        self.assertEqual(status, 200, result)
        self.assertEqual(result["card"]["gpu_identity"], OTHER_GPU)

    def test_measurement_edit_saves_directly_but_unchanged_form_cannot_save(self):
        edited = self.card_payload(bench_generation_tokens=1024)
        status, response = self.request({"action": "preview", **edited})
        self.assertEqual((status, response["preview"]["kind"], response["preview"]["affected"]),
                         (200, "cosmetic-only", []))
        status, saved = self.request({"action": "save", **edited})
        self.assertEqual(status, 200, saved)
        self.assertEqual(saved["card"]["bench_generation_tokens"], 1024)
        status, refusal = self.request({"action": "save", **self.card_payload()})
        self.assertEqual(status, 409)
        self.assertIn("no changes", refusal["error"])

    def test_undetected_card_allows_safe_edits_but_not_gpu_reassignment_or_ceiling_growth(self):
        self.inventory["devices"] = []
        edited = self.card_payload(name="Radeon offline", bench_repetitions=6)
        status, saved = self.request({"action": "save", **edited})
        self.assertEqual(status, 200, saved)
        self.assertEqual((saved["card"]["name"], saved["card"]["bench_repetitions"]),
                         ("Radeon offline", 6))

        moved = {**saved["card"], "gpu_identity": OTHER_GPU}
        status, refusal = self.request({"action": "save", **moved})
        self.assertEqual(status, 409)
        self.assertIn("GPU detection does not report", refusal["error"])
        oversized = {**saved["card"], "vram_ceiling_bytes": 34359738369}
        status, refusal = self.request({"action": "save", **oversized})
        self.assertEqual(status, 409)
        self.assertIn("capacity is unavailable", refusal["error"])
        unchanged = store.read_card(self.store, "card-r9")
        self.assertEqual((unchanged["name"], unchanged["gpu_identity"],
                          unchanged["vram_ceiling_bytes"]),
                         ("Radeon offline", GPU, 34359738368))

    def test_live_card_edit_is_refused_at_confirmation_and_preserves_store(self):
        self.insert_language_profile()
        self.store.connection().execute(
            "UPDATE model_profiles SET published = 1 WHERE profile_id = 'prof-coder'")
        edited = self.card_payload(default_gpu_layers=128)
        status, preview = self.request({"action": "preview", **edited})
        self.assertEqual(status, 200)
        live = mock.patch.object(live_records, "read_state",
                                 return_value=serving_state.State(admission.AGENTIC, healthy=True))
        with live:
            status, refusal = self.request({"action": "save", **edited, "confirm": True,
                                             "review_token": preview["review_token"]})
        self.assertEqual(status, 409)
        self.assertIn("Stop serving", refusal["error"])
        self.assertEqual(store.read_card(self.store, "card-r9")["default_gpu_layers"], 99)

    def test_remove_refuses_a_profile_reference_without_removing_card(self):
        self.insert_language_profile()
        status, refusal = self.request({"action": "remove", "card_id": "card-r9"})
        self.assertEqual(status, 409)
        self.assertIn("Qwen3.6 Coder 192K", refusal["error"])
        self.assertEqual(store.read_card(self.store, "card-r9")["gpu_identity"], GPU)


if __name__ == "__main__":
    unittest.main()

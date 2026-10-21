"""The admission, lease and serving rules, through their own Python API."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from matuto import admission


FREE = admission.Card(admission.CARD_FREE)
FOREIGN = admission.Card(admission.CARD_FOREIGN, "llama-server pid 4242")

# The kinds RUN-1 and card admission require, written out here rather than read from the
# implementation, so dropping one from admission.py fails these tests.
REQUIRED_RUN_KINDS = ("verify", "smoke", "bench", "serve", "bundle", "wq", "context_search")
REQUIRED_SLOT_KINDS = REQUIRED_RUN_KINDS + ("build", "download")
NO_CARD_RUN_KINDS = ("verify",)
CARD_RUN_KINDS = [kind for kind in REQUIRED_RUN_KINDS if kind not in NO_CARD_RUN_KINDS]


def run_holds_card(purpose="smoke for llama-3.1-8b"):
    return admission.Card(admission.CARD_RUN, purpose)


def decide(kind, purpose="a request", current_work=None, serving=admission.STOPPED, card=FREE):
    return admission.decide(kind, purpose, current_work, serving, card)


class AdmissionTest(unittest.TestCase):
    def test_every_required_run_kind_holds_the_slot(self):
        self.assertEqual(sorted(admission.RUN_KINDS), sorted(REQUIRED_RUN_KINDS))
        self.assertEqual(sorted(admission.SLOT_KINDS), sorted(REQUIRED_SLOT_KINDS))
        for kind in REQUIRED_RUN_KINDS:
            with self.subTest(kind=kind):
                self.assertEqual(decide(kind).outcome, admission.ADMIT)

    def test_free_slot_admits_every_kind(self):
        for kind in REQUIRED_SLOT_KINDS:
            with self.subTest(kind=kind):
                self.assertEqual(decide(kind).outcome, admission.ADMIT)

    def test_unknown_kind_is_refused(self):
        self.assertEqual(decide("acceptance").outcome, admission.REFUSE)

    def test_second_request_is_refused_naming_the_current_work(self):
        holders = [admission.Work("build", "llama.cpp HIP 9f1c2d4"),
                   admission.Work("download", "llama-3.1-8b from meta-llama"),
                   admission.Work("bench", "whisper base for breeze")]
        for kind in REQUIRED_SLOT_KINDS:
            for holder in holders:
                with self.subTest(kind=kind, holder=holder.kind):
                    decision = decide(kind, current_work=holder)
                    self.assertEqual(decision.outcome, admission.REFUSE)
                    self.assertIn(holder.kind, decision.reason)
                    self.assertIn(holder.purpose, decision.reason)
                    self.assertIn("nothing is queued", decision.reason)

    def test_verify_and_download_run_beside_serving_and_voice(self):
        for kind in ("verify", admission.DOWNLOAD):
            for serving in (admission.AGENTIC, admission.VOICE):
                with self.subTest(kind=kind, serving=serving):
                    self.assertEqual(decide(kind, serving=serving).outcome, admission.ADMIT)

    def test_build_may_run_beside_agentic_serving(self):
        self.assertEqual(decide(admission.BUILD, serving=admission.AGENTIC).outcome, admission.ADMIT)

    def test_builds_and_voice_serving_exclude_each_other(self):
        refused = decide(admission.BUILD, purpose="llama.cpp HIP 9f1c2d4", serving=admission.VOICE)
        self.assertEqual(refused.outcome, admission.REFUSE)
        self.assertIn("Stop serving", refused.reason)

        refused = decide(admission.SERVE_VOICE,
                         current_work=admission.Work(admission.BUILD, "llama.cpp HIP 9f1c2d4"))
        self.assertEqual(refused.outcome, admission.REFUSE)
        self.assertIn("llama.cpp HIP 9f1c2d4", refused.reason)

    def test_card_run_while_serving_is_offered_only_as_stop_serving_and_run(self):
        for kind in CARD_RUN_KINDS:
            for serving in (admission.AGENTIC, admission.VOICE):
                with self.subTest(kind=kind, serving=serving):
                    decision = decide(kind, serving=serving)
                    self.assertEqual(decision.outcome, admission.STOP_SERVING_AND_RUN)
                    self.assertIn("Consumers lose the model", decision.reason)
                    self.assertIn("stays stopped", decision.reason)

    def test_serving_starts_are_refused_while_a_run_holds_the_card(self):
        card = run_holds_card("smoke for llama-3.1-8b")
        for kind in (admission.START_AGENTIC, admission.SERVE_VOICE):
            with self.subTest(kind=kind):
                decision = decide(kind, card=card)
                self.assertEqual(decision.outcome, admission.REFUSE)
                self.assertIn("smoke for llama-3.1-8b", decision.reason)

    def test_every_card_run_is_refused_for_a_foreign_llama_server(self):
        for kind in CARD_RUN_KINDS:
            for serving in (admission.STOPPED, admission.AGENTIC, admission.VOICE):
                with self.subTest(kind=kind, serving=serving):
                    decision = decide(kind, serving=serving, card=FOREIGN)
                    self.assertEqual(decision.outcome, admission.REFUSE)
                    self.assertIn("llama-server Matuto does not own", decision.reason)
                    self.assertIn("llama-server pid 4242", decision.reason)
        for kind in (admission.START_AGENTIC, admission.SERVE_VOICE):
            with self.subTest(kind=kind):
                self.assertEqual(decide(kind, card=FOREIGN).outcome, admission.REFUSE)

    def test_unread_serving_state_refuses_card_runs_and_serving_starts(self):
        unread = [admission.SERVING_UNKNOWN, None, "", "up", 1]
        for kind in ("smoke", admission.BUILD, admission.START_AGENTIC, admission.SERVE_VOICE):
            for serving in unread:
                with self.subTest(kind=kind, serving=serving):
                    decision = decide(kind, serving=serving)
                    self.assertEqual(decision.outcome, admission.REFUSE)
                    self.assertIn("serving state is unknown", decision.reason)

    def test_unread_card_holder_refuses_card_runs_and_serving_starts(self):
        unread = [admission.Card(admission.CARD_UNKNOWN), admission.Card(None), admission.Card(""),
                  admission.Card("busy"), None]
        for kind in ("smoke", admission.START_AGENTIC, admission.SERVE_VOICE):
            for card in unread:
                for serving in (admission.STOPPED, admission.AGENTIC, admission.VOICE):
                    with self.subTest(kind=kind, holder=repr(card), serving=serving):
                        decision = decide(kind, serving=serving, card=card)
                        self.assertEqual(decision.outcome, admission.REFUSE)
                        self.assertIn("holder is unknown", decision.reason)

    def test_browser_and_cli_requests_are_answered_alike(self):
        # The HTTP route builds its request from a JSON body; the CLI builds the
        # same values from argv. Neither may change the answer.
        body = {"kind": admission.SERVE_VOICE, "purpose": "breeze for atlas"}
        argv = ["serve_voice", "--bundle", "breeze for atlas"]
        holder = admission.Work(admission.BUILD, "llama.cpp HIP 9f1c2d4")
        from_browser = admission.decide(body["kind"], body["purpose"], holder,
                                         admission.AGENTIC, FREE)
        from_cli = admission.decide(argv[0], argv[-1], holder, admission.AGENTIC, FREE)
        self.assertEqual(from_browser, from_cli)
        self.assertEqual(from_cli.outcome, admission.REFUSE)
        self.assertIn("llama.cpp HIP 9f1c2d4", from_cli.reason)

    def test_the_rules_change_nothing(self):
        holder = admission.Work("run", "wq for llama-3.1-8b")
        card = run_holds_card()
        first = admission.decide("bench", "for llama-3.1-8b", holder, admission.AGENTIC, card)
        second = admission.decide("bench", "for llama-3.1-8b", holder, admission.AGENTIC, card)
        self.assertEqual(first, second)
        self.assertEqual(holder, admission.Work("run", "wq for llama-3.1-8b"))
        self.assertEqual(card, run_holds_card())


if __name__ == "__main__":
    unittest.main()
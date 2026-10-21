"""The serving controls and their consequences, through their own Python API."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from matuto import admission, serving  # noqa: E402
from matuto.serving import Bundle, Runtime, Serving, Storage  # noqa: E402

# SERVE-2's five controls and SERVE-3's four confirmations, written out here
# rather than read from the implementation, so dropping one fails these tests.
REQUIRED_CONTROLS = ("start_agentic", "stop_serving", "restart", "serve_voice", "switch_to_agentic")
REQUIRED_CONFIRMATIONS = ("stop_serving", "restart", "serve_voice", "switch_to_agentic")
CARD_HOLDING_RUNS = ("smoke", "bench", "serve", "bundle", "wq", "context_search")

# SERVE-2's four states, plus the partly stopped voice SERVE-3 names.
STOPPED = Serving(admission.STOPPED)
AGENTIC_HEALTHY = Serving(admission.AGENTIC, healthy=True)
AGENTIC_UNHEALTHY = Serving(admission.AGENTIC, healthy=False)
VOICE = Serving(admission.VOICE, bundle="Studio voice")
VOICE_PARTIAL = Serving(admission.VOICE, bundle="Studio voice", parts=("synthesis",))

READY_RUNTIME = Runtime(usable=True)
REACHABLE_STORAGE = Storage(reachable=True)
QUALIFIED_BUNDLE = Bundle("Studio voice", serving.QUALIFIED)

# The offered set per state. VOICE_PARTIAL keeps Switch in place but refused.
EXPECTED_OFFERED = {
    STOPPED: ["serve_voice", "start_agentic"],
    AGENTIC_HEALTHY: ["restart", "serve_voice", "stop_serving"],
    AGENTIC_UNHEALTHY: ["restart", "serve_voice", "stop_serving"],
    VOICE: ["stop_serving", "switch_to_agentic"],
    VOICE_PARTIAL: ["stop_serving"],
}
# Every state SERVE-2 offers Serve voice in.
SERVE_VOICE_STATES = (STOPPED, AGENTIC_HEALTHY, AGENTIC_UNHEALTHY)


def controls(state, work=None, runtime=READY_RUNTIME, storage=REACHABLE_STORAGE,
             bundles=(QUALIFIED_BUNDLE,)):
    return serving.controls(state, work, runtime, storage, bundles)


def offered(state, **keywords):
    return sorted(control.name for control in controls(state, **keywords) if control.offered)


def control_named(state, name, **keywords):
    return next(control for control in controls(state, **keywords) if control.name == name)


class ControlsTest(unittest.TestCase):
    def test_the_module_exposes_exactly_the_named_controls_and_confirmations(self):
        self.assertEqual(sorted(serving.CONTROLS), sorted(REQUIRED_CONTROLS))
        self.assertEqual(sorted(serving.CONFIRMED), sorted(REQUIRED_CONFIRMATIONS))
        self.assertEqual(set(serving.LABELS), set(REQUIRED_CONTROLS))

    def test_every_state_offers_exactly_its_controls(self):
        for state, expected in EXPECTED_OFFERED.items():
            with self.subTest(state=state.state, healthy=state.healthy, parts=state.parts):
                found = controls(state)
                self.assertEqual(sorted(control.name for control in found if control.offered),
                                 expected)
                self.assertTrue(all(control.name in REQUIRED_CONTROLS for control in found))

    def test_healthy_and_unhealthy_agentic_offer_the_same_controls(self):
        self.assertEqual(offered(AGENTIC_HEALTHY), offered(AGENTIC_UNHEALTHY))

    def test_only_start_agentic_serving_acts_without_confirmation(self):
        for state in (STOPPED, AGENTIC_HEALTHY, AGENTIC_UNHEALTHY, VOICE, VOICE_PARTIAL):
            for control in controls(state):
                with self.subTest(state=state.state, control=control.name):
                    self.assertEqual(control.confirmation, control.name != serving.START_AGENTIC)

    def test_every_refused_control_carries_a_reason(self):
        refused = [control for state in (STOPPED, AGENTIC_HEALTHY, VOICE, VOICE_PARTIAL)
                   for control in controls(state, work=admission.Work(admission.BUILD, "llama.cpp"),
                                           runtime=Runtime(False), storage=Storage(False),
                                           bundles=(Bundle("Candidate voice",
                                                           serving.CANDIDATE),))
                   if not control.offered]
        self.assertTrue(refused)
        for control in refused:
            with self.subTest(control=control.name):
                self.assertTrue(control.reasons)
                self.assertTrue(all(reason.strip() for reason in control.reasons))

    def test_a_remaining_speech_part_keeps_stop_retryable_and_switch_refused(self):
        self.assertEqual(offered(VOICE_PARTIAL), ["stop_serving"])
        refused = control_named(VOICE_PARTIAL, serving.SWITCH_TO_AGENTIC)
        self.assertFalse(refused.offered)
        self.assertTrue(any("synthesis" in reason for reason in refused.reasons))

    def test_an_unread_state_offers_no_control(self):
        for state in (Serving("unknown"), Serving(""), Serving(None)):
            with self.subTest(state=repr(state.state)):
                self.assertEqual(controls(state), ())


class ServeVoiceGateTest(unittest.TestCase):
    def reasons(self, state=STOPPED, **keywords):
        return control_named(state, serving.SERVE_VOICE, **keywords).reasons

    def test_serve_voice_is_offered_when_its_gate_is_met(self):
        self.assertEqual(offered(STOPPED), ["serve_voice", "start_agentic"])
        self.assertEqual(offered(AGENTIC_HEALTHY), ["restart", "serve_voice", "stop_serving"])

    def test_serve_voice_needs_a_usable_runtime(self):
        for state in SERVE_VOICE_STATES:
            with self.subTest(state=state.state, healthy=state.healthy):
                reasons = self.reasons(state, runtime=Runtime(False, "the selected build is missing"))
                self.assertTrue(any("runtime" in reason and "missing" in reason
                                    for reason in reasons))

    def test_serve_voice_needs_reachable_storage(self):
        for state in SERVE_VOICE_STATES:
            with self.subTest(state=state.state, healthy=state.healthy):
                reasons = self.reasons(state,
                                       storage=Storage(False, "the storage location is unavailable"))
                self.assertTrue(any("storage" in reason.lower() and "unavailable" in reason
                                    for reason in reasons))

    def test_a_build_refuses_serve_voice_and_names_it(self):
        for state in SERVE_VOICE_STATES:
            with self.subTest(state=state.state, healthy=state.healthy):
                reasons = self.reasons(state, work=admission.Work(admission.BUILD,
                                                                  "llama.cpp HIP 9f1c2d4"))
                self.assertTrue(any("llama.cpp HIP 9f1c2d4" in reason for reason in reasons))
                self.assertTrue(any("build" in reason.lower() for reason in reasons))

    def test_a_card_holding_run_refuses_serve_voice_and_names_it(self):
        for state in SERVE_VOICE_STATES:
            for kind in CARD_HOLDING_RUNS:
                with self.subTest(state=state.state, healthy=state.healthy, kind=kind):
                    reasons = self.reasons(state, work=admission.Work(kind, "llama-3.1-8b"))
                    self.assertTrue(any("llama-3.1-8b" in reason for reason in reasons))

    def test_a_download_does_not_refuse_serve_voice(self):
        for state in SERVE_VOICE_STATES:
            with self.subTest(state=state.state, healthy=state.healthy):
                self.assertEqual(self.reasons(state,
                                              work=admission.Work(admission.DOWNLOAD,
                                                                  "llama-3.1-8b")), ())

    def test_a_verify_run_does_not_refuse_serve_voice(self):
        for state in SERVE_VOICE_STATES:
            with self.subTest(state=state.state, healthy=state.healthy):
                self.assertEqual(self.reasons(state,
                                              work=admission.Work("verify", "llama-3.1-8b")), ())

    def test_serve_voice_is_refused_without_a_qualified_available_bundle(self):
        for state in SERVE_VOICE_STATES:
            with self.subTest(state=state.state, healthy=state.healthy):
                self.assertTrue(self.reasons(state, bundles=()))
                self.assertTrue(self.reasons(state,
                                             bundles=(Bundle("Candidate voice",
                                                             serving.CANDIDATE),)))

    def test_the_picker_offers_only_a_qualified_available_bundle(self):
        choices = serving.bundle_choices([
            Bundle("Studio voice", serving.QUALIFIED),
            Bundle("Candidate voice", serving.CANDIDATE),
            Bundle("Changed voice", serving.CHANGED),
            Bundle("Gone voice", serving.QUALIFIED, available=False,
                   detail="its language member's file is missing"),
            Bundle("Odd voice", "something else"),
        ])
        self.assertEqual([choice.name for choice in choices],
                         ["Studio voice", "Candidate voice", "Changed voice", "Gone voice",
                          "Odd voice"])
        self.assertEqual([choice.offered for choice in choices],
                         [True, False, False, False, False])
        self.assertTrue(any("candidate" in reason.lower() for reason in choices[1].reasons))
        self.assertTrue(any("changed since qualification" in reason for reason in choices[2].reasons))
        self.assertTrue(any("missing" in reason for reason in choices[3].reasons))
        self.assertTrue(any("unknown" in reason.lower() for reason in choices[4].reasons))


class StartAgenticGateTest(unittest.TestCase):
    def test_a_card_holding_run_refuses_start_agentic_serving_and_names_it(self):
        for kind in CARD_HOLDING_RUNS:
            with self.subTest(kind=kind):
                refused = control_named(STOPPED, serving.START_AGENTIC,
                                        work=admission.Work(kind, "llama-3.1-8b"))
                self.assertFalse(refused.offered)
                self.assertTrue(any(kind in reason and "llama-3.1-8b" in reason
                                    for reason in refused.reasons))

    def test_a_build_download_or_verify_does_not_refuse_start_agentic_serving(self):
        works = (admission.Work(admission.BUILD, "llama.cpp HIP 9f1c2d4"),
                 admission.Work(admission.DOWNLOAD, "llama-3.1-8b"),
                 admission.Work("verify", "llama-3.1-8b"))
        for work in works:
            with self.subTest(kind=work.kind):
                self.assertTrue(control_named(STOPPED, serving.START_AGENTIC, work=work).offered)


class ConsequenceTest(unittest.TestCase):
    def test_stop_serving_stops_agentic_serving_and_starts_nothing(self):
        consequence = serving.consequence(serving.STOP_SERVING, AGENTIC_UNHEALTHY)
        self.assertTrue(any("agentic serving" in stop.lower() for stop in consequence.stops))
        self.assertEqual(consequence.starts, ("Nothing",))

    def test_stop_serving_from_voice_stops_every_voice_part_and_starts_nothing(self):
        for state in (VOICE, VOICE_PARTIAL):
            with self.subTest(state=state.state):
                consequence = serving.consequence(serving.STOP_SERVING, state)
                self.assertEqual(consequence.stops, ("Every voice part of Studio voice",))
                self.assertEqual(consequence.starts, ("Nothing",))

    def test_restart_stops_and_starts_agentic_serving_and_unloads_the_model(self):
        consequence = serving.consequence(serving.RESTART, AGENTIC_UNHEALTHY)
        self.assertTrue(any("unloads" in stop for stop in consequence.stops))
        self.assertIn("Agentic serving with the published profiles", consequence.starts)

    def test_serve_voice_stops_agentic_serving_and_starts_the_bundle(self):
        consequence = serving.consequence(serving.SERVE_VOICE, AGENTIC_HEALTHY,
                                          bundle="Studio voice")
        self.assertTrue(any("agentic serving" in stop.lower() for stop in consequence.stops))
        self.assertEqual(consequence.starts,
                         ("Studio voice: language, synthesis and recognition",))

    def test_serve_voice_from_stopped_stops_nothing(self):
        consequence = serving.consequence(serving.SERVE_VOICE, STOPPED, bundle="Studio voice")
        self.assertEqual(consequence.stops, ("Nothing",))

    def test_switch_stops_voice_and_starts_agentic_with_the_published_profiles(self):
        consequence = serving.consequence(serving.SWITCH_TO_AGENTIC, VOICE)
        self.assertEqual(consequence.stops, ("Every voice part",))
        self.assertEqual(consequence.starts, ("Agentic serving with the published profiles",))

    def test_switch_is_not_described_while_a_speech_part_remains(self):
        with self.assertRaises(ValueError):
            serving.consequence(serving.SWITCH_TO_AGENTIC, VOICE_PARTIAL)

    def test_every_confirmation_names_the_safe_choice(self):
        activations = [(serving.STOP_SERVING, AGENTIC_HEALTHY, None),
                       (serving.STOP_SERVING, VOICE, None),
                       (serving.RESTART, AGENTIC_HEALTHY, None),
                       (serving.SERVE_VOICE, STOPPED, "Studio voice"),
                       (serving.SERVE_VOICE, AGENTIC_HEALTHY, "Studio voice"),
                       (serving.SWITCH_TO_AGENTIC, VOICE, None)]
        for control, state, bundle in activations:
            with self.subTest(control=control, state=state.state):
                consequence = serving.consequence(control, state, bundle=bundle)
                self.assertTrue(consequence.safe_choice)
                self.assertEqual(consequence.safe_choice, serving.SAFE_CHOICE)

    def test_the_running_work_is_kept_while_it_continues(self):
        work = admission.Work(admission.DOWNLOAD, "llama-3.1-8b")
        consequence = serving.consequence(serving.STOP_SERVING, AGENTIC_HEALTHY, work)
        self.assertTrue(any("download" in stay and "llama-3.1-8b" in stay
                            for stay in consequence.stays))

    def test_start_agentic_serving_confirms_nothing(self):
        with self.assertRaises(ValueError):
            serving.consequence(serving.START_AGENTIC, STOPPED)

    def test_a_transition_the_state_does_not_offer_is_refused(self):
        attempts = [(serving.RESTART, STOPPED, None),
                    (serving.SWITCH_TO_AGENTIC, AGENTIC_HEALTHY, None),
                    (serving.SERVE_VOICE, VOICE, "Studio voice"),
                    (serving.STOP_SERVING, STOPPED, None),
                    (serving.START_AGENTIC, VOICE, None)]
        for control, state, bundle in attempts:
            with self.subTest(control=control, state=state.state):
                with self.assertRaises(ValueError):
                    serving.consequence(control, state, bundle=bundle)


class RestoreAfterFailedVoiceStartTest(unittest.TestCase):
    def test_agentic_serving_is_restored_when_it_was_serving(self):
        for state in (AGENTIC_HEALTHY, AGENTIC_UNHEALTHY):
            with self.subTest(healthy=state.healthy):
                self.assertEqual(serving.workload_to_restore(state), serving.AGENTIC_SERVING)

    def test_nothing_is_restored_when_serving_was_stopped(self):
        self.assertEqual(serving.workload_to_restore(STOPPED), serving.NOTHING)

    def test_a_state_that_never_starts_voice_restores_nothing_it_started(self):
        for state in (VOICE, VOICE_PARTIAL, Serving("unknown")):
            with self.subTest(state=state.state):
                with self.assertRaises(ValueError):
                    serving.workload_to_restore(state)


class PurityTest(unittest.TestCase):
    def test_the_rules_start_stop_and_record_nothing(self):
        state = Serving(admission.AGENTIC, healthy=False, parts=("synthesis",),
                        bundle="Studio voice")
        work = admission.Work(admission.BUILD, "llama.cpp HIP 9f1c2d4")
        runtime = Runtime(False, "the selected build is missing")
        storage = Storage(False, "the location is unavailable")
        bundles = [Bundle("Studio voice", serving.CANDIDATE)]
        first = serving.controls(state, work, runtime, storage, bundles)
        second = serving.controls(state, work, runtime, storage, bundles)
        self.assertEqual(first, second)
        self.assertEqual(state, Serving(admission.AGENTIC, healthy=False, parts=("synthesis",),
                                        bundle="Studio voice"))
        self.assertEqual(work, admission.Work(admission.BUILD, "llama.cpp HIP 9f1c2d4"))
        self.assertEqual(bundles, [Bundle("Studio voice", serving.CANDIDATE)])
        self.assertEqual(serving.consequence(serving.STOP_SERVING, state, work),
                         serving.consequence(serving.STOP_SERVING, state, work))


if __name__ == "__main__":
    unittest.main()

"""The context-search rules, through their own Python API."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from matuto import context_search


K = 1024
# RUN-9 and the operator ruling of 2026-10-09, written out here rather than read
# from the implementation, so dropping a rung from context_search.py fails.
REQUIRED_LADDER = (512 * K, 384 * K, 256 * K, 192 * K, 128 * K, 96 * K)
FLOOR = 96 * K

PASS = context_search.PASS


def language_result(**overrides):
    facts = {"checks": (("WQ-06", PASS), ("WQ-10", PASS)),
             "server_loaded": True,
             "peak_vram": 28_000_000_000,
             "vram_ceiling": 32_000_000_000}
    facts.update(overrides)
    return context_search.RungResult(context_search.LANGUAGE, **facts)


def bundle_result(**overrides):
    facts = {"checks": (("fit", PASS), ("real-time", PASS), ("recognition", PASS))}
    facts.update(overrides)
    return context_search.RungResult(context_search.BUNDLE, **facts)


def failing(kind=context_search.LANGUAGE):
    return (language_result(server_loaded=False) if kind == context_search.LANGUAGE
            else bundle_result(checks=(("fit", context_search.FAIL), ("real-time", PASS),
                                       ("recognition", PASS))))


def judge(trained_context, start_rung, results):
    return context_search.judge(trained_context, start_rung, results)


class LadderTest(unittest.TestCase):
    def test_the_ladder_is_the_operator_ruled_one_in_tokens(self):
        self.assertEqual(K, 1024)
        self.assertEqual(context_search.LADDER, REQUIRED_LADDER)
        self.assertEqual(context_search.LOWEST_RUNG, FLOOR)

    def test_offered_rungs_are_at_or_below_the_trained_context(self):
        cases = {512 * K: REQUIRED_LADDER,
                 196_608: (196_608, 131_072, 98_304),
                 131_072: (131_072, 98_304),
                 FLOOR: (FLOOR,)}
        for trained_context, expected in cases.items():
            with self.subTest(trained_context=trained_context):
                self.assertEqual(context_search.offered_rungs(trained_context), expected)

    def test_a_bundle_takes_its_language_member_context(self):
        # The bundle's trained context is its language member's, so the ladder
        # stops at 192K and a bundle rung decides the search.
        results = {196_608: failing(context_search.BUNDLE),
                   131_072: bundle_result(),
                   98_304: bundle_result()}
        self.assertEqual(context_search.offered_rungs(196_608), (196_608, 131_072, 98_304))
        verdict = judge(196_608, 196_608, results)
        self.assertEqual(verdict.outcome, context_search.WINNER)
        self.assertEqual(verdict.rung, 131_072)
        self.assertTrue(verdict.checked)

    def test_a_trained_context_below_96k_offers_no_rung(self):
        for trained_context in (FLOOR - 1, 65_536, 32_768, 0):
            with self.subTest(trained_context=trained_context):
                self.assertEqual(context_search.offered_rungs(trained_context), ())
                verdict = judge(trained_context, FLOOR, {})
                self.assertEqual(verdict.outcome, context_search.REFUSED)
                self.assertIn("below the 96K floor", verdict.reason)
                self.assertIn(str(trained_context), verdict.reason)

    def test_an_unread_trained_context_offers_no_rung(self):
        for trained_context in (None, "196608", 196_608.0, True):
            with self.subTest(trained_context=repr(trained_context)):
                self.assertEqual(context_search.offered_rungs(trained_context), ())
                verdict = judge(trained_context, FLOOR, {})
                self.assertEqual(verdict.outcome, context_search.REFUSED)
                self.assertIn("unread", verdict.reason)


class OrderTest(unittest.TestCase):
    def test_a_start_rung_outside_the_offered_set_is_refused(self):
        refused = judge(131_072, 512 * K, {})
        self.assertEqual(refused.outcome, context_search.REFUSED)
        self.assertIn("512K", refused.reason)
        self.assertIn("not offered", refused.reason)
        for start_rung in (196_608, 65_536, 100_000, None):
            with self.subTest(start_rung=repr(start_rung)):
                self.assertEqual(judge(131_072, start_rung, {}).outcome, context_search.REFUSED)
        self.assertEqual(judge(131_072, 131_072, {}).outcome, context_search.RUNNING)

    def test_the_search_runs_down_one_rung_at_a_time_from_the_start(self):
        results = {}
        expected = [512 * K, 384 * K, 256 * K, 196_608, 131_072, FLOOR]
        for rung in expected:
            with self.subTest(rung=rung):
                verdict = judge(512 * K, 512 * K, results)
                self.assertEqual(verdict.outcome, context_search.RUNNING)
                self.assertEqual(verdict.next_rung, rung)
                results[rung] = failing()
        self.assertEqual(judge(512 * K, 512 * K, results).outcome, context_search.NO_WINNER)

    def test_the_search_starts_at_the_operator_chosen_rung(self):
        verdict = judge(196_608, 131_072, {})
        self.assertEqual(verdict.next_rung, 131_072)

    def test_the_search_stops_after_the_consistency_check(self):
        results = {512 * K: failing(), 384 * K: failing(), 256 * K: failing(),
                   196_608: language_result()}
        # The rung below the first working rung runs as its check...
        check = judge(512 * K, 512 * K, results)
        self.assertEqual(check.outcome, context_search.RUNNING)
        self.assertEqual(check.next_rung, 131_072)
        # ...and the search stops there: nothing below the check is ever asked for.
        results[131_072] = language_result()
        verdict = judge(512 * K, 512 * K, results)
        self.assertEqual(verdict.outcome, context_search.WINNER)
        self.assertEqual(verdict.rung, 196_608)
        self.assertTrue(verdict.checked)
        self.assertIn("consistency check at 128K", verdict.reason)


class VerdictTest(unittest.TestCase):
    def test_the_first_working_rung_wins_when_its_check_works(self):
        results = {512 * K: language_result(), 384 * K: language_result()}
        verdict = judge(512 * K, 512 * K, results)
        self.assertEqual(verdict.outcome, context_search.WINNER)
        self.assertEqual(verdict.rung, 512 * K)
        self.assertTrue(verdict.checked)

    def test_a_failed_consistency_check_names_no_winner(self):
        results = {512 * K: failing(), 384 * K: language_result(), 256 * K: failing()}
        verdict = judge(512 * K, 512 * K, results)
        self.assertEqual(verdict.outcome, context_search.NO_WINNER)
        self.assertIsNone(verdict.rung)
        self.assertIn("384K worked", verdict.reason)
        self.assertIn("consistency check at 256K failed", verdict.reason)

    def test_no_working_rung_names_no_winner(self):
        results = {rung: failing() for rung in REQUIRED_LADDER}
        verdict = judge(512 * K, 512 * K, results)
        self.assertEqual(verdict.outcome, context_search.NO_WINNER)
        self.assertIsNone(verdict.rung)
        self.assertIn("No rung from 512K to 96K worked", verdict.reason)

    def test_the_lowest_rung_wins_without_a_check(self):
        results = {256 * K: failing(), 196_608: failing(), 131_072: failing(),
                   FLOOR: language_result()}
        verdict = judge(256 * K, 256 * K, results)
        self.assertEqual(verdict.outcome, context_search.WINNER)
        self.assertEqual(verdict.rung, FLOOR)
        self.assertFalse(verdict.checked)
        self.assertIn("96K is the lowest rung", verdict.reason)
        self.assertIn("no consistency check", verdict.reason)

    def test_below_the_floor_a_start_at_the_floor_is_still_refused(self):
        verdict = judge(65_536, FLOOR, {})
        self.assertEqual(verdict.outcome, context_search.REFUSED)
        self.assertIn("no rung is offered", verdict.reason)


class PredicateTest(unittest.TestCase):
    def test_a_language_rung_works_on_the_server_the_ceiling_and_wq_06_10(self):
        self.assertTrue(context_search.rung_works(language_result()))

        fails = {"server not loaded": language_result(server_loaded=False),
                 "peak at the ceiling": language_result(vram_ceiling=28_000_000_000),
                 "peak over the ceiling": language_result(peak_vram=33_000_000_000),
                 "ceiling unread": language_result(vram_ceiling=None),
                 "peak unread": language_result(peak_vram=None),
                 "WQ-06 FAIL": language_result(checks=(("WQ-06", context_search.FAIL),
                                                        ("WQ-10", PASS))),
                 "WQ-06 ERROR": language_result(checks=(("WQ-06", context_search.ERROR),
                                                        ("WQ-10", PASS))),
                 "WQ-10 SKIP": language_result(checks=(("WQ-06", PASS),
                                                       ("WQ-10", context_search.SKIP))),
                 "deciding row missing": language_result(checks=(("WQ-06", PASS),))}
        for why, result in fails.items():
            with self.subTest(why=why):
                self.assertFalse(context_search.rung_works(result))

    def test_other_wq_rows_are_recorded_but_decide_nothing(self):
        result = language_result(checks=(("WQ-01", context_search.FAIL),
                                         ("WQ-06", PASS),
                                         ("WQ-10", PASS)))
        self.assertTrue(context_search.rung_works(result))

    def test_a_bundle_rung_works_on_fit_real_time_and_recognition(self):
        self.assertTrue(context_search.rung_works(bundle_result()))
        for name in ("fit", "real-time", "recognition"):
            failing_rows = tuple((row, context_search.FAIL if row == name else PASS)
                                 for row in ("fit", "real-time", "recognition"))
            with self.subTest(name=name):
                self.assertFalse(context_search.rung_works(bundle_result(checks=failing_rows)))
            missing_rows = tuple((row, PASS) for row in ("fit", "real-time", "recognition")
                                 if row != name)
            with self.subTest(missing=name):
                self.assertFalse(context_search.rung_works(bundle_result(checks=missing_rows)))

    def test_an_unknown_kind_or_no_result_never_works(self):
        self.assertFalse(context_search.rung_works(None))
        self.assertFalse(context_search.rung_works(
            context_search.RungResult("bundle-ish", (("fit", PASS),))))

    def test_the_recorded_256k_fails_wq_06_and_192k_wins(self):
        # The operator ruled this by hand against the records: 256K's WQ-06
        # reached its deadline with the queue not idle, while 192K passed.
        results = {256 * K: language_result(peak_vram=31_800_000_000,
                                            checks=(("WQ-06", context_search.FAIL),
                                                    ("WQ-10", PASS))),
                   196_608: language_result(),
                   131_072: language_result()}
        verdict = judge(256 * K, 256 * K, results)
        self.assertEqual(verdict.outcome, context_search.WINNER)
        self.assertEqual(verdict.rung, 196_608)
        self.assertTrue(verdict.checked)


class PurityTest(unittest.TestCase):
    def test_the_rules_change_nothing(self):
        results = {256 * K: failing(), 196_608: language_result()}
        copied = dict(results)
        first = judge(256 * K, 256 * K, results)
        second = judge(256 * K, 256 * K, results)
        self.assertEqual(first, second)
        self.assertEqual(results, copied)
        self.assertEqual(context_search.LADDER, REQUIRED_LADDER)
        self.assertEqual(context_search.offered_rungs(196_608), (196_608, 131_072, 98_304))


if __name__ == "__main__":
    unittest.main()

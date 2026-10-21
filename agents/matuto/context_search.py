"""Pure rules for one context search: the ladder, the order and the verdict.

The rules take the record's trained context, the operator's start rung and the
result each rung's run recorded, and answer which rung the search runs next or
what it found. They run, record and change nothing; the runner that starts the
rungs decides from these answers. See README.md for the package conventions.
"""

from dataclasses import dataclass

K = 1024
# RUN-9: the ladder, largest first. The operator ruling of 2026-10-09 ends it at
# 96K, so 96K has no rung below and a search never goes lower.
LADDER = (512 * K, 384 * K, 256 * K, 192 * K, 128 * K, 96 * K)
LOWEST_RUNG = LADDER[-1]

LANGUAGE = "language"
BUNDLE = "bundle"

PASS = "PASS"
FAIL = "FAIL"
ERROR = "ERROR"
SKIP = "SKIP"

RUNNING = "running"
WINNER = "winner"
NO_WINNER = "no winner"
REFUSED = "refused"

# A language rung decides on the server, the card's whole-card peak and these WQ
# rows; every other row is recorded for the acceptance and decides nothing. A
# bundle rung decides on its fit, real-time and recognition rows.
LANGUAGE_DECIDING_CHECKS = ("WQ-06", "WQ-10")
BUNDLE_DECIDING_CHECKS = ("fit", "real-time", "recognition")


@dataclass(frozen=True)
class RungResult:
    """One rung's run, as the checks it recorded left it.

    A language rung carries the server, the whole-card peak and the card's
    ceiling; a bundle rung carries only its checks. A fact the run could not
    supply is absent, and an absent fact never counts as a pass.
    """

    kind: str
    checks: tuple = ()
    server_loaded: bool = False
    peak_vram: int | None = None
    vram_ceiling: int | None = None


@dataclass(frozen=True)
class Verdict:
    """The rung to run next (`next_rung`), or the search's outcome and reason."""

    outcome: str
    next_rung: int | None = None
    rung: int | None = None
    checked: bool = False
    reason: str = ""


def label(rung):
    """A rung's ladder name, e.g. 196608 -> '192K'."""
    return f"{rung // K}K"


def offered_rungs(trained_context):
    """The rungs at or below the record's trained context, largest first.

    A bundle takes its language member's trained context. An unread context, and
    one below the 96K floor, offer no rung at all, so no search can start.
    """
    if isinstance(trained_context, bool) or not isinstance(trained_context, int):
        return ()
    if trained_context < LOWEST_RUNG:
        return ()
    return tuple(rung for rung in LADDER if rung <= trained_context)


def rung_works(result):
    """Whether one rung's recorded result satisfies RUN-9's predicate.

    A missing or unread fact is never a pass, so a partial result refuses the
    rung rather than qualifying it.
    """
    if result is None:
        return False
    statuses = dict(result.checks)
    if result.kind == LANGUAGE:
        return (result.server_loaded is True
                and _under_ceiling(result.peak_vram, result.vram_ceiling)
                and all(statuses.get(name) == PASS for name in LANGUAGE_DECIDING_CHECKS))
    if result.kind == BUNDLE:
        return all(statuses.get(name) == PASS for name in BUNDLE_DECIDING_CHECKS)
    return False


def judge(trained_context, start_rung, results):
    """The next rung to run, or the search's verdict, from the results so far.

    Pure: it reads the trained context, the start rung and `results` and changes
    none of them. The caller runs `next_rung`, records that rung's result under
    its size in `results`, and calls again while the outcome is RUNNING. A start
    rung outside the offered set, and a record below the floor, are refused
    before any rung runs.
    """
    offered = offered_rungs(trained_context)
    if not offered:
        return Verdict(REFUSED, reason=_no_rungs_reason(trained_context))
    if start_rung not in offered:
        return Verdict(REFUSED, reason=(
            f"The start rung {_shown(start_rung)} is not offered at a trained context of "
            f"{_shown(trained_context)}; it offers {', '.join(label(rung) for rung in offered)}."))
    plan = tuple(rung for rung in offered if rung <= start_rung)
    for rung in plan:
        result = results.get(rung)
        if result is None:
            return Verdict(RUNNING, next_rung=rung)
        if rung_works(result):
            return _after_first_working(rung, results)
    return Verdict(NO_WINNER, reason=(
        f"No rung from {label(plan[0])} to {label(plan[-1])} worked, so the search names no winner."))


def _after_first_working(rung, results):
    """The first working rung wins unless its one consistency check fails."""
    below = _rung_below(rung)
    if below is None:
        return Verdict(WINNER, rung=rung, checked=False, reason=(
            f"{label(rung)} is the lowest rung, so it is the winner with no consistency check."))
    check = results.get(below)
    if check is None:
        return Verdict(RUNNING, next_rung=below)
    if rung_works(check):
        return Verdict(WINNER, rung=rung, checked=True, reason=(
            f"{label(rung)} worked and its consistency check at {label(below)} worked."))
    return Verdict(NO_WINNER, reason=(
        f"{label(rung)} worked but its consistency check at {label(below)} failed, so the search "
        f"names no winner."))


def _rung_below(rung):
    """The ladder rung under this one, or None when it is the lowest."""
    index = LADDER.index(rung)
    return LADDER[index + 1] if index + 1 < len(LADDER) else None


def _under_ceiling(peak, ceiling):
    """A whole-card peak strictly under the card's ceiling; an unread value fails."""
    if isinstance(peak, bool) or isinstance(ceiling, bool):
        return False
    if not isinstance(peak, int) or not isinstance(ceiling, int):
        return False
    return peak < ceiling


def _no_rungs_reason(trained_context):
    if isinstance(trained_context, bool) or not isinstance(trained_context, int):
        return (f"The record's trained context is unread ({trained_context!r}), so no rung is "
                f"offered and the search is refused.")
    return (f"The record's trained context of {trained_context} tokens is below the 96K floor of "
            f"{LOWEST_RUNG}, so no rung is offered and the search is refused.")


def _shown(value):
    """A rung or context named by its ladder name and its token count when it has one."""
    if isinstance(value, int) and not isinstance(value, bool) and value in LADDER:
        return f"{label(value)} ({value})"
    return f"{value}"

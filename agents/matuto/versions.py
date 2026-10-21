"""Check one official origin's versions for a fixed recipe. See README.md.

Check versions asks the given official origin which refs it returns, lets the
operator choose one of them or name an official branch or commit, resolves that
choice to a full commit, and holds the recipe, the chosen ref, the full commit
and the physical target together in the pin Build is shown. A check that cannot
reach the origin, or cannot read its answer, is unknown with its reason: no ref
is invented, and no result is ever called "latest" or "up to date". Nothing runs
on a schedule; the commands run only when `check` or `resolve` is called.

The commands are `git ls-remote` and, to establish an explicit commit that is
not one of the returned refs, a `git fetch --depth=1` of that commit into a
scratch repository. Both are argv form, run from a directory that is not a
checkout, against the given origin URL alone, with the caller's inherited git
configuration dropped, so no configured remote, URL rewrite or operator
checkout can stand in for the origin.
"""

import os
import subprocess
import tempfile
from dataclasses import dataclass

from matuto.recipes import COMMIT, RECIPES

# A stalled transport must not hold the page open forever; past this the check
# is unknown rather than pending.
TIMEOUT_SECONDS = 60


@dataclass(frozen=True)
class Ref:
    """One ref the origin returned and the full commit it names."""

    name: str
    commit: str


@dataclass(frozen=True)
class Listing:
    """One check's answer: the origin's refs, or unknown with its reason."""

    refs: tuple = ()
    unknown: bool = False
    reason: str = ""


@dataclass(frozen=True)
class Resolution:
    """One choice resolved to a full commit, or refused/unknown with its reason."""

    commit: str | None = None
    unknown: bool = False
    reason: str = ""


@dataclass(frozen=True)
class Pin:
    """The request a Build may start: recipe, chosen ref, full commit, target."""

    recipe: str
    ref: str
    commit: str
    target: object = None


def check(origin):
    """The refs `origin` returns, or unknown with the reason it could not.

    The refs are exactly what the origin answered, in its order, each with the
    full commit it names; a tag's peeled commit is used. A malformed answer is
    unknown, never a ref list with invented entries.
    """
    done, failure = _ls_remote(origin)
    if failure is not None:
        return Listing(unknown=True, reason=failure)
    refs = _refs(done.stdout)
    if refs is None:
        return Listing(unknown=True,
                       reason=f"{origin} answered with something other than refs")
    return Listing(refs=refs)


def resolve(origin, choice):
    """Resolve one chosen ref or explicit commit from `origin`.

    The choice is established against the refs the check returned. A full
    40-character commit that is not a returned ref's commit is fetched from the
    origin, so a historical commit resolves and a commit or object the origin
    says it does not have is refused. An operational failure while fetching is
    unknown with its reason, because it does not prove absence. Any other choice
    is matched by its full ref name, or its unambiguous branch or tag short
    name. A name the origin does not have is refused, and an unreachable origin
    is unknown with its reason.
    """
    if not isinstance(choice, str) or not choice:
        return Resolution(reason="no ref or commit was chosen")
    listing = check(origin)
    if listing.unknown:
        return Resolution(unknown=True, reason=listing.reason)
    if COMMIT.fullmatch(choice):
        if any(ref.commit == choice for ref in listing.refs):
            return Resolution(commit=choice)
        confirmed, unknown, reason = _confirm(origin, choice)
        return Resolution(commit=choice if confirmed else None, unknown=unknown,
                          reason=reason)
    match, reason = _match(listing.refs, choice)
    return Resolution(commit=match, reason=reason or "")


def pin(recipe, ref, commit, target=None):
    """Hold one resolved request together: recipe, ref, full commit and target.

    The recipe must be one of the four fixed recipes and the commit full, so a
    pin always names a request the build job can take.
    """
    if recipe not in RECIPES:
        raise ValueError(f"{recipe!r} is not one of the four fixed recipes")
    if not isinstance(commit, str) or not COMMIT.fullmatch(commit):
        raise ValueError(f"{commit!r} is not a full 40-character commit")
    return Pin(recipe, ref, commit, target)


def retained(pin_value, recipe, ref, target=None):
    """The pin while every input still matches, or None once any of them changed.

    Build takes only what this returns, so a pin made for another recipe, ref or
    physical target can never be built.
    """
    if pin_value is None:
        return None
    if (pin_value.recipe, pin_value.ref, pin_value.target) == (recipe, ref, target):
        return pin_value
    return None


def _ls_remote(origin):
    """Run git ls-remote; return (completed, None) or (None, reason)."""
    if not isinstance(origin, str) or not origin:
        return None, "no official origin was given"
    argv = ["git", "ls-remote", "--", origin]
    try:
        done = subprocess.run(argv, cwd="/", env=_environment(),
                              capture_output=True, text=True, timeout=TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        return None, f"git ls-remote did not answer within {TIMEOUT_SECONDS} seconds"
    except UnicodeDecodeError as error:
        return None, f"git ls-remote answered with bytes this check cannot read: {error}"
    except OSError as error:
        return None, f"git ls-remote could not run: {error}"
    if done.returncode != 0:
        return None, (done.stderr.strip() or f"git ls-remote exited {done.returncode}")
    return done, None


def _environment():
    """The caller's environment with every git variable removed.

    Inherited git variables can point at an operator checkout (GIT_DIR,
    GIT_OBJECT_DIRECTORY, ...) or rewrite the given origin URL
    (GIT_CONFIG_PARAMETERS, `url.<base>.insteadOf`). None of them is needed to
    query the origin, so they are all dropped; the two that disable the
    caller's configuration are set explicitly.
    """
    environment = {name: value for name, value in os.environ.items()
                   if not name.startswith("GIT_")}
    environment["GIT_CONFIG_GLOBAL"] = os.devnull
    environment["GIT_CONFIG_NOSYSTEM"] = "1"
    return environment


def _confirm(origin, commit):
    """Fetch exactly this commit from `origin`; (confirmed, unknown, reason)."""
    try:
        with tempfile.TemporaryDirectory(prefix="matuto-versions-") as scratch:
            started = subprocess.run(["git", "init", "--bare", "-q", scratch],
                                     cwd="/", env=_environment(),
                                     capture_output=True, text=True, timeout=TIMEOUT_SECONDS)
            if started.returncode != 0:
                return False, True, (started.stderr.strip()
                                     or "could not open a scratch repository")
            done = subprocess.run(["git", "-C", scratch, "fetch", "--quiet", "--depth=1",
                                   "--no-tags", "--", origin, commit],
                                  cwd="/", env=_environment(),
                                  capture_output=True, text=True, timeout=TIMEOUT_SECONDS)
            if done.returncode != 0:
                message = done.stderr.strip() or f"git fetch exited {done.returncode}"
                # Only the origin saying the object is not there is a refusal; a
                # lost transport, permission or disk failure is unknown.
                absent = ("not our ref" in message or "couldn't find remote ref" in message)
                return False, not absent, message
            kind = subprocess.run(["git", "-C", scratch, "cat-file", "-t", commit],
                                  cwd="/", env=_environment(),
                                  capture_output=True, text=True, timeout=TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        return False, True, f"git fetch did not answer within {TIMEOUT_SECONDS} seconds"
    except (OSError, UnicodeDecodeError) as error:
        return False, True, f"git fetch could not run: {error}"
    if kind.returncode != 0:
        return False, True, (kind.stderr.strip() or f"could not inspect {commit}")
    if kind.stdout.strip() != "commit":
        return False, False, f"the official origin did not return {commit} as a commit"
    return True, False, ""


def _refs(text):
    """The returned refs, or None when a line is not a `<commit>\\t<ref>` pair."""
    entries = []
    peeled = {}
    for line in text.splitlines():
        parts = line.split("\t")
        if len(parts) != 2 or not COMMIT.fullmatch(parts[0]):
            return None
        commit, name = parts
        if name.endswith("^{}"):
            peeled[name[:-3]] = commit
        else:
            entries.append((name, commit))
    return tuple(Ref(name, peeled.get(name, commit)) for name, commit in entries)


def _match(refs, name):
    """(commit, "") for one unambiguous ref name, else (None, reason)."""
    for ref in refs:
        if ref.name == name:
            return ref.commit, ""
    matches = [ref.commit for ref in refs
               if ref.name in (f"refs/heads/{name}", f"refs/tags/{name}")]
    if len(matches) == 1:
        return matches[0], ""
    if len(matches) > 1:
        return None, f"{name} names both a branch and a tag; choose one by its full ref"
    return None, f"the official origin returned no {name}"

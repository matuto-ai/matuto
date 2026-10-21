"""The Install a version API: discover inputs, resolve an official pin, submit it."""

import contextlib
import json
import secrets

from matuto import admission, recipes, runtime_build, runtime_builds, serving_state, store, versions
from matuto import work_cli
from matuto.web import RefusedError

PATH = "/api/runtime-install"
_pins = {}


def _body(request):
    try:
        value = json.loads(request.body or b"{}")
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise RefusedError("Install a version needs a valid JSON request.") from None
    if not isinstance(value, dict):
        raise RefusedError("Install a version needs a JSON object.")
    return value


def _recipe(value):
    if value not in recipes.RECIPES:
        raise RefusedError("Choose one of the four fixed runtime recipes.")
    return recipes.RECIPES[value]


def handle(request):
    if request.method not in ("GET", "HEAD", "POST"):
        raise RefusedError("Install a version accepts GET and POST only.")
    try:
        with contextlib.closing(store.open_store()) as opened:
            if request.method in ("GET", "HEAD"):
                choices = runtime_builds.choices(opened)
                missing = {item["name"]: item["missingPrerequisites"]
                           for item in choices["recipes"]}
                return 200, {"recipes": [
                    {"name": name, "takesGfxTarget": recipe.takes_gfx,
                     "missingPrerequisites": missing[name]}
                    for name, recipe in recipes.RECIPES.items()],
                    "cards": choices["cards"], "state": choices["state"],
                    "reason": choices.get("reason"),
                    "servingState": serving_state.read().state, "csrf": request.csrf}
            body = _body(request)
            action = body.get("action")
            if action == "invalidate":
                _pins.pop(request.csrf, None)
                return 200, {"invalidated": True}
            if action == "serving-state":
                return 200, {"servingState": serving_state.read().state}
            recipe_name = body.get("recipe")
            recipe = _recipe(recipe_name)
            ref = body.get("ref", "")
            target = body.get("target") or None
            if not isinstance(ref, str):
                raise RefusedError("Choose an official branch, tag or full commit.")
            if recipe.takes_gfx:
                if action in ("resolve", "build"):
                    choices = runtime_builds.choices(opened)
                    card = next((item for item in choices["cards"]
                                 if item["pciAddress"] == body.get("card")), None)
                    if not card or not card["canBuildHIP"] or card["gfxTarget"] != target:
                        _pins.pop(request.csrf, None)
                        raise RefusedError(card["targetReason"] if card else
                                           "Choose a currently detected physical card and HIP target.")
            elif target is not None or body.get("card"):
                raise RefusedError(f"The {recipe_name} recipe has no physical target.")

            origin = recipe.origin
            if action == "check":
                _pins.pop(request.csrf, None)
                listing = versions.check(origin)
                return 200, {"refs": [ref.__dict__ for ref in listing.refs],
                             "unknown": listing.unknown, "reason": listing.reason}
            if action == "resolve":
                _pins.pop(request.csrf, None)
                result = versions.resolve(origin, ref)
                if result.commit is None:
                    return 200, {"unknown": result.unknown, "reason": result.reason}
                pin = versions.pin(recipe_name, ref, result.commit, target)
                pin_id = secrets.token_urlsafe(24)
                _pins[request.csrf] = (pin, body.get("card"), pin_id)
                return 200, {"commit": pin.commit, "recipe": pin.recipe,
                             "ref": pin.ref, "target": pin.target, "pinId": pin_id}
            if action == "build":
                missing = runtime_builds.prerequisites(recipe_name)
                if missing:
                    _pins.pop(request.csrf, None)
                    raise RefusedError(f"Missing prerequisites: {', '.join(missing)}.")
                saved = _pins.get(request.csrf)
                if (not saved or saved[1] != body.get("card") or saved[2] != body.get("pinId")
                        or (saved[0].recipe, saved[0].ref, saved[0].target)
                        != (recipe_name, ref, target)):
                    _pins.pop(request.csrf, None)
                    raise RefusedError("Resolve this version again before Build.")
                pin = saved[0]
                running = work_cli.open_runner()
                if running is None:
                    raise RefusedError("The shared work runner is unavailable; Build was not started.")
                try:
                    result = runtime_build.submit(running, pin, recipe=recipe_name,
                                                  ref=ref, target=target)
                    if result.start.decision != admission.ADMIT:
                        raise RefusedError(result.start.reason)
                    _pins.pop(request.csrf, None)
                    return 202, {"work": result.start.work.work_id,
                                 "warning": result.warning,
                                 "href": "#/runtime/activity"}
                finally:
                    running.close()
            raise RefusedError("Choose Check versions, Resolve version or Build.")
    except (OSError, store.StoreError, ValueError) as error:
        raise RefusedError(str(error)) from None

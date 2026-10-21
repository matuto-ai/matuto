"""Read Overview's serving panel and current work without changing either."""

import json
import logging

from matuto import admission, runner, serving, serving_state, store, voice
from matuto.status import current_work as current_work_status
from matuto.web import RefusedError

PATH = "/api/overview/serving"
CONTROL_ROUTES = {
    serving.START_AGENTIC: "/api/serving/start",
    serving.STOP_SERVING: "/api/serving/stop",
    serving.RESTART: "/api/serving/restart",
    serving.SERVE_VOICE: "/api/serving/voice",
    serving.SWITCH_TO_AGENTIC: "/api/serving/voice/switch",
}


def handle(request):
    if request.method not in ("GET", "HEAD"):
        raise RefusedError("Overview serving data is read-only.")
    opened = store.read_store()
    if opened is None:
        return 503, {"error": "Overview serving data is unavailable."}
    try:
        connection = opened.connection()
        state = serving_state.read(store_connection=connection)
        work = runner._current_work(connection)
        current = current_work_status.report(connection)
        if current["label"] != "Nothing running" and work is not None:
            current["label"] = _work_label(current["label"], work)
        profile_data = _published_profiles(opened)
        bundle_options = voice.bundle_options()
        runtime, storage = voice._runtime(None), voice._storage(None)
        selection = connection.execute(
            "SELECT s.build_id, b.label, b.backend, s.card_id, c.name AS card"
            " FROM runtime_selection AS s LEFT JOIN builds AS b ON b.build_id=s.build_id"
            " LEFT JOIN cards AS c ON c.card_id=s.card_id WHERE s.selection_id=1").fetchone()
        remaining_parts = (() if state.state == admission.VOICE and state.healthy
                           else tuple(part.profile for part in state.parts))
        controls = serving.controls(
            serving.Serving(state.state, state.healthy, remaining_parts, state.bundle),
            work, runtime, storage,
            [serving.Bundle(item["name"], item["qualification"], item["available"],
                            "; ".join(item["reasons"])) for item in bundle_options])
        offered = {item.name: item for item in controls}
        routes = dict(CONTROL_ROUTES)
        if state.state == admission.VOICE:
            routes[serving.STOP_SERVING] = "/api/serving/voice/stop"
        entries = []
        names = (serving.CONTROLS if state.state not in
                 (admission.STOPPED, admission.AGENTIC, admission.VOICE) else
                 tuple(item.name for item in controls))
        for name in names:
            control = offered.get(name)
            reasons = control.reasons if control else (state.reason or "Serving state is unknown; nothing was queued.",)
            if name == serving.START_AGENTIC:
                reasons = list(reasons)
                if not runtime.usable:
                    reasons.append(runtime.detail or "No usable runtime is selected.")
                if not storage.reachable:
                    reasons.append(storage.detail or "The model storage is unreachable.")
                if not profile_data:
                    reasons.append("Nothing is published.")
                reasons = tuple(reasons)
            entries.append({"name": name, "label": serving.LABELS[name],
                            "confirmation": name in serving.CONFIRMED,
                            "href": routes[name], "reason": " ".join(reasons)})
        bundles = connection.execute("SELECT bundle_id, name FROM bundles").fetchall()
        bundle_names = {row["bundle_id"]: row["name"] for row in bundles}
        addresses = []
        units = dict(getattr(state, "units", ()))
        if state.address and any(units.get(unit) == "active" for unit in
                                 (serving_state.ROUTER_UNIT, serving_state.VOICE_ROUTER_UNIT)):
            addresses.append({"label": "Serving", "address": state.address})
        addresses.extend({"label": f"{part.role or 'Voice'} — {part.profile}",
                          "address": part.address} for part in state.parts
                         if part.address and part.state == "active")
        parts = [{"profile": part.profile, "role": part.role, "state": part.state,
                  "address": part.address, "stray": part.stray} for part in state.parts]
        return 200, {
            "state": state.state, "healthy": state.healthy, "reason": state.reason,
            "headline": _headline(state), "model": state.model,
            "bundle": bundle_names.get(state.bundle, state.bundle),
            "runtime": ({"build": selection["label"] or selection["build_id"],
                         "backend": selection["backend"],
                         "card": selection["card"] or selection["card_id"]} if selection else None),
            "addresses": addresses, "parts": parts, "profiles": profile_data,
            "publishLink": not bool(profile_data),
            "bundles": bundle_options, "controls": entries,
            "currentWork": current if current["label"] != "Nothing running" else None,
        }
    except Exception:
        logging.exception("Failed to read Overview serving data")
        return 503, {"error": "Overview serving data is unavailable."}
    finally:
        opened.close()


def _work_label(label, work):
    if work.kind == admission.BUILD:
        return f"Building runtime: {work.purpose}"
    if work.kind == admission.DOWNLOAD:
        try:
            request = json.loads(work.purpose)
            return f"Downloading {request['repository']}@{request['revision']}"
        except (KeyError, TypeError, ValueError):
            return label
    return label


def _published_profiles(opened):
    from matuto.routes.language_profiles import _list

    return [profile for profile in _list(opened)["profiles"] if profile["published"]]


def _headline(state):
    if state.state == admission.STOPPED:
        return "Ready. Not serving."
    if state.state == admission.AGENTIC:
        return "Serving your published models." if state.healthy else "Agentic serving is unhealthy."
    if state.state == admission.VOICE:
        return "Voice is on." if state.healthy else "Voice serving is unhealthy."
    return "Serving state is unknown." if not state.reason else f"Serving state is unknown: {state.reason}"

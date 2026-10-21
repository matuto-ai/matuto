"""Check the trusted signed release list without downloading an update."""

import json
import sqlite3
import time

from matuto import admission, releases, runner, serving_state, store, updater


def _state(config_root=None):
    raw = store.read_update_state(store.store_path(config_root))
    if not raw:
        return None, {}
    try:
        details = json.loads(raw["phase"]) if raw["phase"] else {}
    except (TypeError, ValueError):
        details = {}
    return raw, details if isinstance(details, dict) else {}


def display(config_root=None):
    """Return installed version and the last saved check without contacting a feed."""
    _raw, details = _state(config_root)
    installed = _installed_version()
    state = details.get("last_update_check_state", "status")
    if not details.get("release_address"):
        state = "no-feed"
    offer = details.get("update_offer")
    if (state in ("failed", "stale", "no-feed") or not installed
            or not isinstance(offer, dict) or not isinstance(offer.get("version"), str)
            or not releases.VERSION.fullmatch(offer["version"])
            or tuple(map(int, offer["version"].split("."))) <= tuple(map(int, installed.split(".")))):
        offer = None
    withdrawn = details.get("withdrawn_offer")
    if isinstance(withdrawn, dict):
        withdrawn = {**withdrawn, "installed": withdrawn.get("version") == installed}
    stage = details.get("stage", "")
    installation = None
    if stage in updater.UPDATE_STAGES:
        installation = {"version": details.get("version", installed), "stage": "interrupted",
                        "phase": stage, "reason": details.get("reason", "")}
    elif stage == "maintenance":
        installation = {"version": details.get("version", installed), "stage": stage,
                        "reason": details.get("reason", "")}
    elif details.get("app_install_pending"):
        pending = details["app_install_pending"]
        if not isinstance(pending, dict):
            pending = {}
        if (stage == "failed" and not pending.get("relaunching")):
            installation = {"version": pending.get("version", installed), "stage": "failed",
                            "reason": details.get("reason", "the updater failed before activation")}
        elif (stage == "complete" and pending.get("version") == details.get("version") == installed
              and pending.get("list_number") == details.get("list_number")):
            installation = {"version": installed, "stage": "complete", "reason": ""}
        else:
            active = updater.app_install_unit_status(pending) == "active"
            installation = {"version": pending.get("version", installed),
                            "stage": "starting" if active else "interrupted",
                            "phase": "handoff",
                            "reason": "" if active else "Run Repair Matuto to continue."}
    elif details.get("install_result"):
        installation = details["install_result"]
    return {
        "installedVersion": installed,
        "lastChecked": details.get("last_update_check"),
        "listNumber": details.get("list_number"),
        "offer": offer,
        "withdrawn": withdrawn,
        "installation": installation,
        "state": state,
        "reason": details.get("last_update_check_error", ""),
        "csrf": None,
    }


def check(config_root=None, *, persist=True):
    """Verify a bounded signed list, optionally without changing the saved check state."""
    raw, details = _state(config_root)
    installed = _installed_version()
    address = details.get("release_address")
    key = details.get("trusted_key")
    if not address:
        return {"installedVersion": installed, "lastChecked": details.get("last_update_check"),
                "listNumber": details.get("list_number"), "offer": None, "withdrawn": None,
                "state": "no-feed", "reason": "There are no public releases yet."}
    if not isinstance(address, str) or not isinstance(key, str) or not key.strip():
        reason = "The trusted release address or key is unavailable."
        _remember_attempt(raw, config_root, "failed", reason, persist=persist)
        return {"installedVersion": installed, "lastChecked": details.get("last_update_check"),
                "listNumber": details.get("list_number"), "offer": None,
                "withdrawn": details.get("withdrawn_offer"), "state": "failed", "reason": reason}
    if not isinstance(installed, str) or not releases.VERSION.fullmatch(installed):
        reason = "The installed Matuto release version is unavailable."
        _remember_attempt(raw, config_root, "failed", reason, persist=persist)
        return {"installedVersion": installed, "lastChecked": details.get("last_update_check"),
                "listNumber": details.get("list_number"), "offer": None,
                "withdrawn": details.get("withdrawn_offer"), "state": "failed", "reason": reason}
    try:
        accepted = details.get("list_number")
        try:
            document = updater._http_bytes(address.rstrip("/") + "/release-list.json",
                                           updater.MAX_LIST_BYTES)
        except updater.UpdateError as error:
            if "HTTP 404" not in str(error):
                raise
            reason = "There are no public releases yet."
            _remember_attempt(raw, config_root, "no-feed", reason, persist=persist)
            return {"installedVersion": installed, "lastChecked": details.get("last_update_check"),
                    "listNumber": details.get("list_number"), "offer": None,
                    "withdrawn": details.get("withdrawn_offer"), "state": "no-feed",
                    "reason": reason}
        signature = updater._http_bytes(address.rstrip("/") + "/release-list.json.sig",
                                        updater.MAX_LIST_BYTES)
        reading = releases.read_list(document, signature, key.encode("utf-8"),
                                     accepted_number=accepted)
        if reading.stale:
            raise updater.UpdateError("the signed release list is stale; it brings no news")
        candidates = reading.release_list.releases
        installed_tuple = tuple(int(part) for part in installed.split(".")) if installed else None
        prior_offer = details.get("update_offer")
        prior_withdrawn = details.get("withdrawn_offer")
        prior_versions = {record.get("version") for record in (prior_offer, prior_withdrawn)
                          if isinstance(record, dict)}
        withdrawn = next((item for item in candidates if item.version in prior_versions
                          and item.withdrawn), None)
        installed_release = next((item for item in candidates if item.version == installed
                                  and item.withdrawn), None)
        withdrawn = installed_release or withdrawn
        newer = [item for item in candidates if not item.withdrawn
                 and (installed_tuple is None or tuple(int(part) for part in item.version.split("."))
                      > installed_tuple)]
        release = max(newer, key=lambda item: tuple(int(part) for part in item.version.split(".")),
                      default=None)
        offer = _facts(release) if release else None
        if withdrawn:
            withdrawn_facts = _facts(withdrawn)
            withdrawn_facts["installed"] = withdrawn.version == installed
        else:
            withdrawn_facts = None
        checked = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        if persist:
            saved = store.record_update_check(
                store.store_path(config_root),
                {"last_update_check": checked, "last_update_check_state": "success",
                 "last_update_check_error": "", "update_offer": offer,
                 "withdrawn_offer": withdrawn_facts},
                list_number=reading.release_list.number)
            if not saved:
                latest = display(config_root)
                return {"installedVersion": latest["installedVersion"],
                        "lastChecked": latest["lastChecked"], "listNumber": latest["listNumber"],
                        "offer": None, "withdrawn": latest["withdrawn"], "state": "stale",
                        "reason": "The signed release list is stale; it brings no news."}
        return {"installedVersion": installed, "lastChecked": checked,
                "listNumber": reading.release_list.number, "offer": offer,
                "withdrawn": withdrawn_facts,
                "state": "offer" if offer else "withdrawn" if withdrawn else "none",
                "reason": ""}
    except (OSError, releases.ReleaseError, updater.UpdateError, store.StoreError,
            TypeError, ValueError) as error:
        reason = str(error)
        state = "stale" if "stale" in reason.lower() else "failed"
        _remember_attempt(raw, config_root, state, reason, persist=persist)
        return {"installedVersion": installed, "lastChecked": details.get("last_update_check"),
                "listNumber": details.get("list_number"), "offer": None,
                "withdrawn": details.get("withdrawn_offer"), "state": state, "reason": reason}


def install_status(config_root=None, *, opened_store=None, offer=None, list_number=None,
                   settle_work=True):
    """Current install offer and every serving, work, or host blocker."""
    result = display(config_root)
    if offer is not None:
        result["offer"] = offer
    if list_number is not None:
        result["listNumber"] = list_number
    blockers = []
    owned_store = opened_store is None
    try:
        opened = opened_store or store.open_store(config_root)
        state = serving_state.read(config_root=config_root,
                                   store_connection=opened.connection())
        active_units = [unit for unit, status in state.units
                        if status in ("active", "activating", "reloading", "deactivating")]
        if state.state == admission.SERVING_UNKNOWN:
            blockers.append({"text": f"Serving state is unknown: {state.reason}",
                             "href": "#/overview/serving", "label": "Check serving on Overview"})
        if state.state != admission.STOPPED:
            if active_units:
                blockers.extend({"text": f"Serving is active ({unit})",
                                 "command": f"systemctl --user stop {unit}",
                                 "href": "#/overview/serving", "label": "Stop serving on Overview"}
                                for unit in active_units)
            elif state.state != admission.SERVING_UNKNOWN:
                blockers.append({"text": f"{state.state.capitalize()} serving is active",
                                 "href": "#/overview/serving", "label": "Stop serving on Overview"})
        reader = runner.Runner(config_root, opened_store=opened)
        try:
            observed = reader.works() if settle_work else reader.live_works()
            work = [item for item in observed if not item.outcome]
        finally:
            reader.close()
        for item in work:
            action = "Cancel download" if item.kind == admission.DOWNLOAD else "Stop"
            label = f"Runs & jobs — {action}"
            blockers.append({"text": f"{item.kind.capitalize()}: {item.purpose} ({action})",
                             "command": f"systemctl --user stop {item.unit}",
                             "href": f"#/runs?work={item.work_id}", "label": label})
    except (OSError, sqlite3.Error, store.StoreError):
        blockers.append({"text": "Serving or current work is unknown",
                         "href": "#/runs", "label": "Check Runs & jobs"})
    finally:
        if owned_store and "opened" in locals():
            opened.close()
    offer = result.get("offer")
    missing = updater.missing_prerequisites(offer.get("prerequisites", [])) if offer else []
    blockers.extend({"text": f"Missing OS prerequisite: {name}",
                     "href": "#/settings/maintenance", "label": "Settings › Maintenance"}
                    for name in missing)
    if result.get("installation") and result["installation"].get("stage") not in ("complete", "failed"):
        installation = result["installation"]
        blockers.append({"text": f"Matuto update to {installation['version']} is {installation['stage']}",
                         "href": "#/settings/maintenance", "label": "Settings › Maintenance"})
    pending = _state(config_root)[1].get("app_install_pending")
    if (isinstance(pending, dict) and result.get("installation", {}).get("stage") == "failed"
            and updater.app_install_unit_status(pending) == "active"):
        blockers.append({"text": f"Matuto update to {pending.get('version', 'unknown')} is still running",
                         "href": "#/settings/maintenance", "label": "Settings › Maintenance"})
    result["blockers"] = blockers
    return result


def preview(version, list_number=None, config_root=None):
    result = install_status(config_root)
    offer = result.get("offer")
    if not offer or offer.get("version") != version or result.get("listNumber") != list_number:
        raise updater.UpdateError("The update offer changed. Review the current offer before installing.")
    if result["blockers"]:
        raise updater.UpdateError("Install is blocked: " + "; ".join(item["text"] for item in result["blockers"]))
    return result


def install(version, list_number, config_root=None, *, open_after=False, persist_check=True):
    """Revalidate the reviewed signed offer and host state before detaching updater."""
    checked = check(config_root, persist=persist_check)
    if checked.get("listNumber") != list_number or not checked.get("offer") \
            or checked["offer"].get("version") != version:
        raise updater.UpdateError("The update offer changed. Review the current offer before installing.")
    opened = store.open_store(config_root, exclusive=True)
    try:
        result = install_status(config_root, opened_store=opened, offer=checked["offer"],
                                list_number=list_number, settle_work=persist_check)
        if result["blockers"]:
            raise updater.UpdateError("Install is blocked: " + "; ".join(
                item["text"] for item in result["blockers"]))
        offer = result.get("offer")
        unit_name = updater.app_install_unit_name()
        if (result.get("listNumber") != list_number or not offer
                or offer.get("version") != version
                or not store.begin_app_install(
                    store.store_path(config_root), version, list_number, unit_name,
                    verified_offer=checked["offer"] if not persist_check else None)):
            raise updater.UpdateError("The update offer changed. Review the current offer before installing.")
    finally:
        opened.close()
    try:
        updater.start_app_install(version, list_number, unit_name, open_after=open_after)
    except updater.UpdaterLaunchUncertain:
        raise
    except (OSError, updater.UpdateError) as error:
        store.write_update_state(
            store.store_path(config_root), result["installedVersion"], "complete",
            details={"app_install_pending": None, "app_install_result_unit": unit_name,
                     "install_result": {"version": version, "stage": "failed",
                                        "reason": str(error)[:500]}})
        raise updater.UpdateError(f"the independent updater could not start: {error}") from None
    return {"started": True, "version": version}


def _remember_attempt(raw, config_root, state, reason, *, persist=True):
    if raw and persist:
        store.record_update_check(
            store.store_path(config_root),
            {"last_update_check_state": state, "last_update_check_error": reason})


def _installed_version():
    try:
        return updater.installed_version()
    except updater.UpdateError:
        return None


def _facts(release):
    return {"version": release.version, "notes": release.notes, "size": release.size,
            "prerequisites": list(release.prerequisites),
            "changesDatabase": release.changes_database, "withdrawn": release.withdrawn}

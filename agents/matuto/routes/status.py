"""Collect the app's discovered status providers without changing their records."""

from matuto import discovery, serving_state, store

PATH = "/api/status"
ORDER = {"serving": 0, "current-work": 1, "repair": 2}


def handle(request):
    if request.method not in ("GET", "HEAD"):
        from matuto.web import RefusedError
        raise RefusedError("status is read-only")
    opened = store.read_store()
    if opened is None:
        return 503, {"error": "status is unavailable"}
    try:
        state = serving_state.read(store_connection=opened.connection())
        console = {"usable": state.state == "agentic" and state.healthy}
        items = []
        for name in discovery.item_names("status"):
            provider = discovery.load_item("status", name)
            if name == "console":
                console = provider.item(state)
                continue
            if name == "serving":
                item = {**provider.item(state), "type": "serving",
                        "usable": state.state == "agentic" and state.healthy}
            else:
                item = provider.report(opened.connection())
                if item and name == "storage":
                    item = {"name": name, "type": "repair", "label": item["title"],
                            "detail": f"{item['path']}: {item['reason']}", "href": item["link"]}
            if item:
                items.append(item)
        repairs = sorted((item for item in items if item["type"] == "repair"),
                         key=lambda item: item["name"])
        items = [item for item in items if item["type"] != "repair"]
        if repairs:
            items.append(repairs[0])
        items.sort(key=lambda item: (ORDER[item["type"]], item["name"]))
        serving = next(item for item in items if item["type"] == "serving")
        current_work = next(item for item in items if item["type"] == "current-work")
        if current_work.get("card_holding"):
            serving["label"] = "Card held by a run"
        result = {"items": items, "console": console}
        return 200, result
    except Exception:
        return 503, {"error": "status is unavailable"}
    finally:
        opened.close()

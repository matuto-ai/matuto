"""Read physical PCI GPUs for card records; never probe a GPU runtime."""

import os
from pathlib import Path
import re
import stat

from matuto import store as store_module

PCI_IDS = (Path("/usr/share/hwdata/pci.ids"), Path("/usr/share/misc/pci.ids"),
           Path("/usr/share/pci.ids"))
PCI_ADDRESS = re.compile(r"[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]")


def _pci_inventory(root):
    devices = []
    incomplete = False
    try:
        entries = sorted(root.iterdir())
    except OSError:
        return {"state": "unavailable", "devices": [],
                "reason": "Linux PCI identity attributes are unavailable."}
    for entry in entries:
        if not PCI_ADDRESS.fullmatch(entry.name):
            continue
        try:
            values = {name: (entry / name).read_text().strip() for name in (
                "vendor", "device", "class", "subsystem_vendor", "subsystem_device")}
        except OSError:
            incomplete = True
            continue
        widths = {"vendor": 4, "device": 4, "class": 6,
                  "subsystem_vendor": 4, "subsystem_device": 4}
        if not all(re.fullmatch(rf"0x[0-9a-f]{{{widths[name]}}}", value)
                   for name, value in values.items()):
            incomplete = True
            continue
        if values["class"].startswith("0x03"):
            devices.append({"pciAddress": entry.name, **values})
    result = {"state": "completed", "devices": devices}
    if incomplete:
        result["warning"] = (
            "Some PCI identity attributes were unavailable or invalid; incomplete entries were omitted.")
    return result


def _named_devices(inventory, paths):
    if inventory["state"] != "completed" or not inventory["devices"]:
        return inventory
    lines = None
    for path in paths:
        try:
            with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "rb") as source:
                if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                    continue
                data = source.read(5_000_001)
                if len(data) <= 5_000_000:
                    lines = data.decode("utf-8", errors="replace").splitlines()
                    break
        except OSError:
            continue
    if lines is None:
        return inventory

    models, subsystems = {}, {}
    vendor = device = None
    for line in lines:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line.startswith("\t"):
            parts = line.split(None, 1)
            vendor = parts[0].lower() if len(parts) == 2 and re.fullmatch(
                r"[0-9a-fA-F]{4}", parts[0]) else None
            device = None
        elif line.startswith("\t\t") and vendor and device:
            parts = line[2:].split(None, 2)
            if len(parts) == 3 and all(re.fullmatch(r"[0-9a-fA-F]{4}", part)
                                       for part in parts[:2]):
                subsystems[vendor, device, parts[0].lower(), parts[1].lower()] = parts[2]
        elif vendor:
            parts = line[1:].split(None, 1)
            device = parts[0].lower() if len(parts) == 2 and re.fullmatch(
                r"[0-9a-fA-F]{4}", parts[0]) else None
            if device:
                models[vendor, device] = parts[1]
    result = dict(inventory)
    result["devices"] = []
    for item in inventory["devices"]:
        named = dict(item)
        vendor, device = item["vendor"][2:], item["device"][2:]
        name = (subsystems.get((vendor, device, item["subsystem_vendor"][2:],
                                item["subsystem_device"][2:])) or models.get((vendor, device)))
        if name and not re.fullmatch(r"(?:unknown )?device(?: [0-9a-fA-F]+)?", name, re.I):
            named["displayName"] = re.sub(r"[\x00-\x1f\x7f-\x9f]", "", name)[:160]
        result["devices"].append(named)
    return result


def _vram_bytes(pci_address, root, pci_root):
    try:
        pci_device = (pci_root / pci_address).resolve()
        candidates = [card / "device" / "mem_info_vram_total" for card in root.iterdir()
                      if re.fullmatch(r"card[0-9]+", card.name) and
                      (card / "device").resolve() == pci_device]
        if len(candidates) != 1:
            return None
        value = int(candidates[0].read_text().strip())
        return value if value > 0 else None
    except (OSError, ValueError):
        return None


def detect(store, *, pci_root=Path("/sys/bus/pci/devices"),
           drm_root=Path("/sys/class/drm"), pci_ids=PCI_IDS):
    """List physical GPUs and annotate every stored card against current hardware."""
    inventory = _named_devices(_pci_inventory(pci_root), pci_ids)
    devices = []
    for detected in inventory.get("devices", ()):
        device = dict(detected)
        device["vramBytes"] = _vram_bytes(device["pciAddress"], drm_root, pci_root)
        devices.append(device)

    rows = store.connection().execute(
        "SELECT card_id, name, gpu_identity, vram_ceiling_bytes, default_gpu_layers,"
        " smoke_output_tokens, bench_prompt_tokens, bench_generation_tokens,"
        " bench_repetitions, notes, created_at FROM cards ORDER BY card_id").fetchall()
    identities = {device["pciAddress"] for device in devices}
    cards = [dict(row, detected=(None if inventory["state"] != "completed" else
                                 row["gpu_identity"] in identities)) for row in rows]
    used = {row["gpu_identity"] for row in rows}
    for device in devices:
        device["offered"] = device["pciAddress"] not in used
    return {**inventory, "devices": devices, "cards": cards}


def add(store, card, selected_gpu, *, pci_root=Path("/sys/bus/pci/devices"),
        drm_root=Path("/sys/class/drm"), pci_ids=PCI_IDS):
    """Create a card from a freshly detected device, not a typed PCI identity."""
    inventory = detect(store, pci_root=pci_root, drm_root=drm_root, pci_ids=pci_ids)
    if inventory["state"] != "completed":
        raise store_module.RecordRefusal("card cannot be created while GPU detection is unavailable")
    if not isinstance(selected_gpu, dict):
        raise store_module.RecordRefusal("choose a currently detected GPU")
    device = next((item for item in inventory["devices"]
                   if item["pciAddress"] == selected_gpu.get("pciAddress")), None)
    if device is None or any(device.get(key) != selected_gpu.get(key)
                             for key in ("vendor", "device", "class", "subsystem_vendor",
                                         "subsystem_device")):
        raise store_module.RecordRefusal("choose a currently detected GPU, not a typed identity")
    if not device["offered"]:
        raise store_module.RecordRefusal(f"GPU {device['pciAddress']} already has a card")
    if device["vramBytes"] is None:
        raise store_module.RecordRefusal("card cannot be created because detected VRAM is unknown")
    if not isinstance(card, dict):
        raise store_module.RecordRefusal("card details must be a mapping")
    vram_ceiling = card.get("vram_ceiling_bytes", device["vramBytes"])
    if type(vram_ceiling) is not int or not 0 < vram_ceiling <= device["vramBytes"]:
        raise store_module.RecordRefusal(
            "VRAM ceiling must be a positive integer no greater than detected GPU VRAM")
    record = dict(card, kind="card", gpu_identity=device["pciAddress"],
                  vram_ceiling_bytes=vram_ceiling)
    record.setdefault("name", device.get("displayName", device["pciAddress"]))
    record.setdefault("notes", "")
    record.setdefault("vram_ceiling_bytes", device["vramBytes"])
    record.setdefault("default_gpu_layers", "all")
    store_module.create_card(store, record)
    return store_module.read_card(store, record["card_id"])

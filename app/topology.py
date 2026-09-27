"""Presentation of collected ZFS topology, without estimating usable capacity."""
import re


ROLE_LABELS = {
    "data": "Data vdevs", "log": "Intent log", "cache": "Read cache",
    "special": "Special allocation", "dedup": "Dedup allocation", "spare": "Spares",
}


def _disk_for(name, drives):
    # Only exact device/partition matches: a guessed serial match can point at
    # the wrong physical disk. Unresolved by-id names remain visible as-is.
    matches = []
    for disk in drives:
        path = disk.get("path") or ""
        if not path:
            continue
        if name == path or name == path.removeprefix("/dev/"):
            matches.append(disk)
        elif name.startswith(path):
            suffix = name[len(path):]
            if re.fullmatch(r"p?\d+", suffix):
                matches.append(disk)
    return matches[0] if len(matches) == 1 else None


def pool_topology(pool, drives):
    status = pool.get("status") or {}
    sections = {}
    stacks = {}
    root = None
    for entry in status.get("vdevs") or []:
        node = dict(entry, children=[])
        role = entry.get("role") or "data"
        name = str(entry.get("name") or "Unknown device")
        node["name"] = name
        if role == "data" and (name == pool.get("name") or entry.get("vdev_type") == "root"):
            root = node
            continue
        section = sections.setdefault(role, {"label": ROLE_LABELS.get(role, role.title()), "nodes": []})
        stack = stacks.setdefault(role, [])
        depth = entry.get("indent", 0)
        while stack and stack[-1].get("indent", 0) >= depth:
            stack.pop()
        (stack[-1]["children"] if stack else section["nodes"]).append(node)
        stack.append(node)

    def enrich(node):
        children = node["children"]
        for child in children:
            enrich(child)
        node["disk"] = None if children else _disk_for(node["name"], drives)
        node["disk_count"] = sum(c["disk_count"] for c in children) if children else 1
        match = re.match(r"(raidz[123]?|draid\S*|mirror|replacing|spare)(?:-|$)", node["name"])
        node["kind"] = (match[1].upper() if match else str(node.get("vdev_type") or "Vdev").upper()) if children else "Device"
        state = str(node.get("state") or "UNKNOWN").upper()
        node["state"] = state
        node["tone"] = "good" if state in {"ONLINE", "AVAIL"} else "offline" if state == "UNKNOWN" else "critical"
        for key in ("read_errors", "write_errors", "checksum_errors"):
            node.setdefault(key, None)

    for section in sections.values():
        for node in section["nodes"]:
            enrich(node)
        section["disk_count"] = sum(n["disk_count"] for n in section["nodes"])
    return {"root": root, "sections": list(sections.values())}

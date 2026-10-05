"""Pure planning and diagnostic calculations; estimates never issue disk commands."""

import re
from statistics import median
from datetime import timedelta
from sqlalchemy import select, func
from .models import Metric, DriveLabel, Enclosure, BayAssignment, StorageSample


def match_disk(member, disks):
    """Prefer resolved device/alias identity; never guess between serial substrings."""
    from .parser import _device_matches_disk

    member_id = re.sub(r"-part\d+$", "", member.get("id", "").rsplit("/", 1)[-1])
    exact = []
    for disk in disks:
        aliases = disk.get("by_id") or []
        if isinstance(aliases, str):
            aliases = [aliases]
        path = disk.get("path") or ""
        if (
            path
            and member.get("realpath")
            and _device_matches_disk(member["realpath"], path)
        ) or (member_id and member_id in [a.rsplit("/", 1)[-1] for a in aliases]):
            exact.append(disk)
    if exact:
        return exact[0] if len(exact) == 1 else None
    matches = [
        d
        for d in disks
        if d.get("serial")
        and member_id.endswith(d["serial"])
        and (member_id == d["serial"] or member_id[-len(d["serial"]) - 1] in "_-:")
    ]
    return matches[0] if len(matches) == 1 else None


def expansion_plan(members, disks, proposed_bytes):
    """Estimates disk-replacement growth separately from RAIDZ width expansion."""
    rows = []
    for root in members:
        if root.get("parent_guid") or root.get("role", "data") != "data":
            continue
        leaves = [
            m
            for m in members
            if m.get("top_level_guid") == root["guid"] and not m["group"]
        ]
        if not root["group"]:
            leaves = [root]
        sizes, checklist = [], []
        for member in leaves:
            disk = match_disk(member, disks)
            size = int((disk or {}).get("size_bytes") or 0)
            sizes.append(size)
            checklist.append(
                {
                    "id": member["id"],
                    "size": size,
                    "needs_replacement": size < proposed_bytes if size else None,
                    "location": (disk or {}).get("location", "Location not assigned"),
                }
            )
        match = re.fullmatch(r"raidz([123])?-\d+", root["id"])
        factor = (
            1
            if root["id"].startswith("mirror-")
            else len(sizes) - int(match[1] or 1)
            if match
            else len(sizes)
            if not root["group"]
            else None
        )
        nested = any(
            m.get("group")
            and m.get("top_level_guid") == root["guid"]
            and m["guid"] != root["guid"]
            for m in members
        )
        known = (
            bool(sizes)
            and all(sizes)
            and factor is not None
            and factor > 0
            and not nested
        )
        rows.append(
            {
                "vdev": root["id"],
                "known": known,
                "current": min(sizes) * factor if known else None,
                "projected": max(min(sizes), proposed_bytes) * factor
                if known
                else None,
                "remaining": sum(0 < s < proposed_bytes for s in sizes),
                "disks": checklist,
            }
        )
    return rows


def diagnose(db, server_id, snapshot, now):
    labels = {
        r.identity: r.label
        for r in db.scalars(
            select(DriveLabel).where(DriveLabel.server_id == server_id)
        ).all()
    }
    bays = db.execute(
        select(BayAssignment, Enclosure)
        .join(Enclosure, BayAssignment.enclosure_id == Enclosure.id)
        .where(Enclosure.server_id == server_id)
    ).all()
    for bay, enclosure in bays:
        labels[bay.identity] = f"{enclosure.name} / Bay {bay.slot}"
    rows = []
    drives = snapshot.get("drives", [])
    active = [
        max(
            (d.get("io") or {}).get("read_latency_ms") or 0,
            (d.get("io") or {}).get("write_latency_ms") or 0,
        )
        for d in drives
        if ((d.get("io") or {}).get("read_iops") or 0)
        + ((d.get("io") or {}).get("write_iops") or 0)
        > 0
    ]
    typical = median(active) if active else 0
    # Batched aggregates: no per-drive queries, and no inferred failures from a single sample.
    trends = db.execute(
        select(
            Metric.scope, Metric.name, func.min(Metric.value), func.max(Metric.value)
        )
        .where(
            Metric.server_id == server_id,
            Metric.captured_at >= now - timedelta(days=7),
            Metric.name.in_(
                [
                    "drive.reallocated_sectors",
                    "drive.pending_sectors",
                    "drive.media_errors",
                    "drive.io.read_latency_ms",
                    "drive.io.write_latency_ms",
                ]
            ),
        )
        .group_by(Metric.scope, Metric.name)
    ).all()
    history = {}
    for scope, name, low, high in trends:
        history.setdefault(scope, {})[name] = {"min": low, "max": high}
    for disk in drives:
        identity = disk.get("serial") or disk.get("path") or ""
        disk["location"] = (
            labels.get(identity)
            or labels.get(disk.get("path"))
            or "Location not assigned"
        )
        io, smart = disk.get("io") or {}, disk.get("smart") or {}
        reasons = []
        latency = max(io.get("read_latency_ms") or 0, io.get("write_latency_ms") or 0)
        if latency > max(20, typical * 3):
            reasons.append("Latency exceeds 3× the host median and 20 ms")
        if (io.get("queue_depth") or 0) > 2 and (io.get("busy_pct") or 0) > 90:
            reasons.append("High queue depth and utilization")
        if smart.get("smart_passed") is False:
            reasons.append("SMART reports failure")
        for indicator in ["pending_sectors", "reallocated_sectors", "media_errors"]:
            if smart.get(indicator):
                reasons.append(f"{indicator.replace('_', ' ')}: {smart[indicator]}")
        rows.append(
            {
                "identity": identity,
                "disk": disk,
                "io": io,
                "reasons": reasons,
                "history": history.get(identity, {}),
            }
        )
    return sorted(rows, key=lambda r: (not bool(r["reasons"]), r["identity"]))


def dataset_forecasts(db, server_id, now):
    samples = db.scalars(
        select(StorageSample)
        .where(
            StorageSample.server_id == server_id,
            StorageSample.captured_at >= now - timedelta(days=30),
        )
        .order_by(StorageSample.captured_at)
    ).all()
    groups = {}
    for sample in samples:
        groups.setdefault(sample.name, {})[sample.captured_at.date()] = sample
    result = {}
    for name, days in groups.items():
        points = list(days.values())
        if len(points) < 7:
            result[name] = "Need at least 7 sampled days"
            continue
        first, last = points[0], points[-1]
        elapsed = (last.captured_at - first.captured_at).total_seconds() / 86400
        rate = (last.used - first.used) / elapsed if elapsed > 0 else 0
        result[name] = (
            f"Approximately {max(0, round(last.available / rate))} days at observed net growth"
            if rate > 0
            else "No net growth in sampled period"
        )
    return result


def vdev_diagnosis(topology, diagnostics):
    rows = []
    for pool in topology.get("pools", []):
        for root in pool["members"]:
            if root.get("parent_guid"):
                continue
            leaves = [
                m
                for m in pool["members"]
                if m.get("top_level_guid") == root["guid"] and not m["group"]
            ]
            if not root["group"]:
                leaves = [root]
            matches = []
            for member in leaves:
                disk = match_disk(member, [r["disk"] for r in diagnostics])
                found = (
                    next((r for r in diagnostics if r["disk"] is disk), None)
                    if disk is not None
                    else None
                )
                if found:
                    matches.append(found)
            rows.append(
                {
                    "pool": pool["name"],
                    "vdev": root["id"],
                    "role": root.get("role", "data"),
                    "mapped": len(matches),
                    "members": len(leaves),
                    "outliers": sum(bool(r["reasons"]) for r in matches),
                    "read_bps": sum(r["io"].get("read_bps") or 0 for r in matches),
                    "write_bps": sum(r["io"].get("write_bps") or 0 for r in matches),
                    "queue": sum(r["io"].get("queue_depth") or 0 for r in matches),
                }
            )
    return rows

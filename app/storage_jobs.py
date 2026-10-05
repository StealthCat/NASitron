"""Persistent scheduled storage jobs; no arbitrary shell commands or destructive receive."""

import json
import base64
import hashlib
import hmac
from types import SimpleNamespace
import shlex
import threading
import time
from contextlib import ExitStack
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta

from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select, update

from .alerts import _upsert_alert
from .collector import SSHCollector
from .config import REMOTE_HELPER_PATH
from .db import SessionLocal
from .maintenance import MaintenanceBusy, maintenance_lock
from .models import Server, StoragePolicy, StorageRun, Alert, MaintenanceAction
from .zfs_actions import helper_json, helper_call

_workers = None
_guard = threading.Lock()
_stopping = threading.Event()


class TransferCancelled(RuntimeError):
    """An operator deliberately stopped a resumable transfer."""


def next_due(cron, zone, now=None):
    now = (
        (now or datetime.now(timezone.utc)).replace(tzinfo=timezone.utc)
        if (now is None or now.tzinfo is None)
        else now
    )
    fields = cron.split()
    if len(fields) != 5:
        raise ValueError("Use five schedule fields: minute hour day month weekday")
    # Unix weekday numbering (0/7 Sunday), not APScheduler's Monday=0.
    days = set()
    names = {
        name: i
        for i, name in enumerate(["sun", "mon", "tue", "wed", "thu", "fri", "sat"])
    }
    for term in fields[4].lower().split(","):
        span, sep, step = term.partition("/")
        stride = int(step) if sep else 1
        if stride < 1 or stride > 7:
            raise ValueError("Weekday step must be 1–7")

        def number(value):
            return names[value] if value in names else int(value)

        if span == "*":
            low, high = 0, 6
        elif "-" in span:
            first, last = span.split("-", 1)
            low, high = number(first), number(last)
        else:
            low = number(span)
            high = 7 if sep else low
        if not 0 <= low <= high <= 7:
            raise ValueError("Weekdays must be 0–7 (Sunday=0/7), or sun–sat")
        days.update(day % 7 for day in range(low, high + 1, stride))
    fields[4] = ",".join(list(names)[day] for day in sorted(days))
    value = CronTrigger.from_crontab(
        " ".join(fields), timezone=zone
    ).get_next_fire_time(None, now + timedelta(seconds=1))
    if value is None:
        raise ValueError("Schedule has no future execution")
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def remote_inventory(server):
    with SSHCollector(server) as ssh:
        response = ssh.run(
            shlex.join(["sudo", "-n", REMOTE_HELPER_PATH, "storage", "inventory"]),
            timeout=150,
        )
    if response["exit"] or response.get("stdout_truncated"):
        raise ValueError(
            response.get("stderr")
            or "Storage inventory unavailable; update the remote helper"
        )
    data = json.loads(response["stdout"])
    if data.get("protocol") != 2:
        raise ValueError("Storage management requires the 1.0 remote helper")
    return data


def execute(server, payload, actor):
    plan = helper_json(server, "preview", payload)
    with SessionLocal() as db:
        audit = MaintenanceAction(
            server_id=server.id,
            actor=actor,
            action="zpool_" + payload["action"],
            pool=payload["pool"],
            command=plan["command"],
            state="executing",
            output="Scheduled action",
            success=False,
        )
        db.add(audit)
        db.commit()
        aid = audit.id
    try:
        result = helper_call(server, "execute", payload, plan["fingerprint"])
        code = result["exit"]
        state = (
            "accepted"
            if code == 0
            else "unknown"
            if code in {-1, 124, 255}
            else "failed"
        )
        detail = (result.get("stdout", "") + "\n" + result.get("stderr", ""))[-20000:]
    except Exception as exc:
        code, state, detail = None, "unknown", str(exc)
    with SessionLocal() as db:
        audit = db.get(MaintenanceAction, aid)
        audit.state, audit.exit_code, audit.output = state, code, detail
        audit.success, audit.completed_at = state == "accepted", datetime.utcnow()
        db.commit()
    if state != "accepted":
        raise RuntimeError(f"{state}: {detail}")
    return detail


def snapshot_payload(action, snapshot):
    return {
        "action": action,
        "pool": snapshot.split("/")[0].split("@")[0],
        "target": snapshot,
    }


def retention_candidates(snapshots, dataset, prefix, keep):
    # Exact ownership prefix, exact dataset, no holds/clones. Never recursive deletion.
    owned = [
        s
        for s in snapshots
        if s["name"].startswith(dataset + "@" + prefix)
        and s["name"].split("@")[0] == dataset
    ]
    owned.sort(key=lambda s: (int(s["creation"]), s["name"]), reverse=True)
    return [
        s["name"]
        for s in owned[max(1, keep) :]
        if str(s.get("holds", "0")) == "0" and s.get("clones", "-") in {"-", ""}
    ]


def _remote_value(ssh, dataset, prop):
    response = ssh.run(
        shlex.join(["zfs", "get", "-Hp", "-o", "value", prop, dataset]), timeout=20
    )
    if response.get("stdout_truncated"):
        raise RuntimeError("Truncated ZFS property response")
    if response["exit"] and "does not exist" not in response.get("stderr", ""):
        raise RuntimeError(response.get("stderr") or "Cannot read ZFS property")
    return response["stdout"].strip() if response["exit"] == 0 else ""


def _snapshot_guids(ssh, dataset):
    response = ssh.run(
        shlex.join(
            ["zfs", "list", "-Hp", "-r", "-t", "snapshot", "-o", "name,guid", dataset]
        ),
        timeout=30,
    )
    if response.get("stdout_truncated"):
        raise RuntimeError("Snapshot list truncated; cannot establish replication base")
    if response["exit"] and "does not exist" not in response.get("stderr", ""):
        raise RuntimeError(
            response.get("stderr") or "Cannot list replication snapshots"
        )
    return (
        {
            row.split("\t")[0]: row.split("\t")[1]
            for row in response["stdout"].splitlines()
            if len(row.split("\t")) == 2 and row.split("@")[0] == dataset
        }
        if response["exit"] == 0
        else {}
    )


def replicate(source, destination, config, snapshot, run_id):
    dataset, target, owner = config["dataset"], config["destination"], config["owner"]
    if source.id == destination.id:
        raise ValueError("Replication requires separate registered hosts")
    if not source.host_key_fingerprint or not destination.host_key_fingerprint:
        raise ValueError("Pin both SSH host key fingerprints before replication")
    source = SimpleNamespace(
        **{c.name: getattr(source, c.name) for c in Server.__table__.columns}
    )
    destination = SimpleNamespace(
        **{c.name: getattr(destination, c.name) for c in Server.__table__.columns}
    )
    source.strict_host_key = destination.strict_host_key = True
    channels = []
    with SSHCollector(source) as src, SSHCollector(destination) as dst:
        for ssh, server in [(src, source), (dst, destination)]:
            key = ssh.client.get_transport().get_remote_server_key()
            fingerprint = "SHA256:" + base64.b64encode(
                hashlib.sha256(key.asbytes()).digest()
            ).decode().rstrip("=")
            if not hmac.compare_digest(fingerprint, server.host_key_fingerprint):
                raise ValueError("Pinned SSH fingerprint no longer matches")
            ssh.collection_deadline = None
        token = _remote_value(dst, target, "receive_resume_token")
        existing_owner = _remote_value(dst, target, "org.nasitron:replication")
        if existing_owner and existing_owner != owner:
            raise RuntimeError("Destination is not dedicated to this policy")
        source_snaps, destination_snaps = (
            _snapshot_guids(src, dataset),
            _snapshot_guids(dst, target),
        )
        common = sorted(
            name
            for name, guid in source_snaps.items()
            if name.startswith(dataset + "@nasitron-" + owner + "-")
            and destination_snaps.get(target + "@" + name.split("@")[1]) == guid
        )
        base = common[-1] if common else ""
        # Keep the shared incremental base even when snapshot-retention policies run.
        if base:
            execute(
                source,
                dict(
                    snapshot_payload("snapshot-hold", base), value="nasitron-" + owner
                ),
                f"policy:{owner}",
            )
        token = token if token not in {"", "-"} else ""
        if not token:
            execute(
                source,
                dict(
                    snapshot_payload("snapshot-hold", snapshot),
                    value="nasitron-" + owner,
                ),
                f"policy:{owner}",
            )
        sender = [
            "sudo",
            "-n",
            REMOTE_HELPER_PATH,
            "stream",
            "send",
            snapshot,
            base,
            token,
        ]
        receiver = [
            "sudo",
            "-n",
            REMOTE_HELPER_PATH,
            "stream",
            "receive",
            target,
            owner,
        ]
        errors = []

        def drain(channel):
            while not channel.closed:
                if channel.recv_stderr_ready():
                    chunk = channel.recv_stderr(8192).decode("utf-8", "replace")
                    errors.append(chunk)
                    if len(errors) > 16:
                        errors.pop(0)
                elif channel.exit_status_ready():
                    break
                else:
                    time.sleep(0.05)

        try:
            send = src.client.get_transport().open_session(timeout=20)
            channels.append(send)
            receive = dst.client.get_transport().open_session(timeout=20)
            channels.append(receive)
            for channel in channels:
                channel.settimeout(30)
            receive.exec_command(shlex.join(receiver))
            send.exec_command(shlex.join(sender))
            drains = [
                threading.Thread(target=drain, args=(c,), daemon=True) for c in channels
            ]
            for thread in drains:
                thread.start()
            count, started, saved = 0, time.monotonic(), time.monotonic()
            rate = int(config.get("bandwidth_mib", 0)) * 1024**2

            def check_progress(force=False):
                nonlocal saved
                if _stopping.is_set():
                    raise RuntimeError(
                        "Service stopping; partial receive retained for resume"
                    )
                if force or time.monotonic() - saved > 2:
                    with SessionLocal() as db:
                        run = db.get(StorageRun, run_id)
                        if run is None or run.state == "cancel_requested":
                            raise TransferCancelled(
                                "Transfer cancelled; partial receive retained for resume"
                            )
                        run.bytes_sent = count
                        db.commit()
                    saved = time.monotonic()

            check_progress(force=True)
            while True:
                check_progress()
                if send.recv_ready():
                    data = send.recv(262144)
                    if not data:
                        break
                    receive.sendall(data)
                    count += len(data)
                    if rate:
                        delay = count / rate - (time.monotonic() - started)
                        if delay > 0:
                            time.sleep(min(delay, 1))
                elif send.exit_status_ready():
                    break
                else:
                    time.sleep(0.05)
                if time.monotonic() - started > 86400:
                    raise RuntimeError(
                        "Transfer exceeded 24 hours; resumable receive retained"
                    )
            receive.shutdown_write()
            deadline = time.monotonic() + 120
            while not all(c.exit_status_ready() for c in channels):
                check_progress()
                if time.monotonic() > deadline:
                    raise RuntimeError(
                        "Transfer acknowledgement timed out; inspect destination before retrying"
                    )
                time.sleep(0.1)
            codes = [c.recv_exit_status() for c in channels]
            for thread in drains:
                thread.join(timeout=2)
            if any(codes):
                raise RuntimeError("Replication failed: " + "".join(errors)[-8000:])
            with SessionLocal() as db:
                db.get(StorageRun, run_id).bytes_sent = count
                db.commit()
            # A resumed stream may finish an earlier snapshot. Verify by GUID, then
            # leave this job's newest snapshot for the next incremental run.
            target_snaps = _snapshot_guids(dst, target)
            matched = [
                name
                for name, guid in source_snaps.items()
                if name.startswith(dataset + "@nasitron-" + owner + "-")
                and target_snaps.get(target + "@" + name.split("@")[1]) == guid
            ]
            if not matched:
                raise RuntimeError("No matching snapshot GUID verified after transfer")
            if not token and snapshot not in matched:
                raise RuntimeError(
                    "The requested snapshot GUID was not verified at the destination"
                )
            if token and not any(
                destination_snaps.get(target + "@" + name.split("@")[1])
                != source_snaps[name]
                for name in matched
            ):
                raise RuntimeError(
                    "Resumed transfer did not verify a newly received snapshot"
                )
            newest = sorted(matched)[-1]
            execute(
                source,
                dict(
                    snapshot_payload("snapshot-hold", newest), value="nasitron-" + owner
                ),
                f"policy:{owner}",
            )
            if base and base != newest:
                execute(
                    source,
                    dict(
                        snapshot_payload("snapshot-release", base),
                        value="nasitron-" + owner,
                    ),
                    f"policy:{owner}",
                )
            stamp = newest.split("@nasitron-" + owner + "-")[1].split("-")[0]
            verified_at = datetime.strptime(stamp, "%Y%m%dT%H%M%S")
            return (
                f"Verified destination snapshot GUID for {newest}; {count} bytes transferred.",
                verified_at,
            )
        finally:
            for channel in channels:
                channel.close()


def run_policy(policy_id, run_id):
    with SessionLocal() as db:
        policy = db.get(StoragePolicy, policy_id)
        run = db.get(StorageRun, run_id)
        if not policy or not run:
            return
        source = db.get(Server, policy.server_id)
        config = json.loads(policy.config_json)
        destination = (
            db.get(Server, config.get("destination_server"))
            if policy.kind == "replication"
            else None
        )
        if run.state == "cancel_requested":
            run.state, run.completed_at, run.detail = (
                "cancelled",
                datetime.utcnow(),
                "Cancelled before starting",
            )
            db.commit()
            return
        run.state = "running"
        db.commit()
        actor = f"policy:{policy.id}"
        verified_at = None
        try:
            with ExitStack() as stack:
                for sid in sorted(
                    {source.id} | ({destination.id} if destination else set())
                ):
                    stack.enter_context(maintenance_lock(sid))
                if policy.kind == "replication" and destination is None:
                    raise ValueError("Destination server no longer exists")
                if policy.kind in {"snapshot", "replication"}:
                    prefix = "nasitron-" + config["owner"] + "-"
                    snapshot = (
                        config["dataset"]
                        + "@"
                        + prefix
                        + datetime.utcnow().strftime("%Y%m%dT%H%M%S")
                        + "-"
                        + str(run_id)
                    )
                    execute(
                        source, snapshot_payload("snapshot-create", snapshot), actor
                    )
                    if policy.kind == "replication":
                        if not destination:
                            raise ValueError("Destination server no longer exists")
                        detail, verified_at = replicate(
                            source, destination, config, snapshot, run_id
                        )
                        dest_inventory = remote_inventory(destination)
                        for old in retention_candidates(
                            dest_inventory["snapshots"],
                            config["destination"],
                            prefix,
                            int(config["keep"]),
                        ):
                            execute(
                                destination,
                                snapshot_payload("snapshot-destroy", old),
                                actor,
                            )
                    else:
                        detail = "Snapshot created: " + snapshot
                    inventory = remote_inventory(source)
                    removed = 0
                    for name in retention_candidates(
                        inventory["snapshots"],
                        config["dataset"],
                        prefix,
                        int(config["keep"]),
                    ):
                        execute(
                            source, snapshot_payload("snapshot-destroy", name), actor
                        )
                        removed += 1
                    detail += f" Retired {removed} owned, unheld snapshots."
                elif policy.kind == "scrub":
                    detail = (
                        execute(
                            source, {"action": "scrub", "pool": config["pool"]}, actor
                        )
                        or "Scrub accepted; monitor Operations for completion."
                    )
                else:
                    # One policy per disk enables staggered cron schedules without
                    # launching all extended tests together.
                    detail = execute(
                        source,
                        {
                            "action": "smart-" + config["test"],
                            "pool": "host",
                            "target": config["disk"],
                        },
                        actor,
                    )
            run.state, run.detail = "success", detail
            policy.last_success = verified_at or datetime.utcnow()
            alert = db.scalar(
                select(Alert).where(
                    Alert.server_id == source.id,
                    Alert.key == f"storage-policy:{policy.id}",
                )
            )
            if alert:
                alert.active, alert.resolved_at = False, datetime.utcnow()
        except TransferCancelled as exc:
            run.state, run.detail = "cancelled", str(exc)
        except MaintenanceBusy as exc:
            run.state, run.detail = "deferred", str(exc)
            policy.next_run = min(
                policy.next_run, datetime.utcnow() + timedelta(minutes=5)
            )
        except Exception as exc:
            run.state, run.detail = "failed", str(exc)[:20000]
            existing = {
                a.key: a
                for a in db.scalars(
                    select(Alert).where(Alert.server_id == source.id)
                ).all()
            }
            _upsert_alert(
                db,
                existing,
                source,
                f"storage-policy:{policy.id}",
                "warning",
                "Storage job needs attention: " + policy.name,
                run.detail,
            )
        run.completed_at = datetime.utcnow()
        db.commit()


def enqueue(policy_id):
    global _workers
    with _guard, SessionLocal() as db:
        policy = db.get(StoragePolicy, policy_id)
        if not policy:
            raise ValueError("Policy not found")
        active = db.scalar(
            select(StorageRun.id).where(
                StorageRun.policy_id == policy_id,
                StorageRun.state.in_(["queued", "running", "cancel_requested"]),
            )
        )
        if active:
            raise ValueError("This policy already has an active run")
        run = StorageRun(policy_id=policy_id)
        db.add(run)
        policy.next_run = next_due(policy.cron, policy.timezone)
        db.commit()
        if _workers is None:
            _workers = ThreadPoolExecutor(
                max_workers=2, thread_name_prefix="nasitron-storage"
            )
        _workers.submit(run_policy, policy_id, run.id)
        return run.id


def tick():
    due_ids = []
    with SessionLocal() as db:
        now = datetime.utcnow()
        policies = db.scalars(
            select(StoragePolicy).where(StoragePolicy.enabled.is_(True))
        ).all()
        for policy in policies:
            if policy.next_run <= now:
                due_ids.append(policy.id)
            config = json.loads(policy.config_json)
            max_age = int(config.get("max_age_hours", 48))
            if (
                now - (policy.last_success or policy.created_at)
            ).total_seconds() > max_age * 3600:
                server = db.get(Server, policy.server_id)
                existing = {
                    a.key: a
                    for a in db.scalars(
                        select(Alert).where(Alert.server_id == server.id)
                    ).all()
                }
                _upsert_alert(
                    db,
                    existing,
                    server,
                    f"storage-policy:{policy.id}",
                    "warning",
                    "Storage job overdue: " + policy.name,
                    f"No successful run in {max_age} hours. Check schedule and job history.",
                )
        db.commit()
    # Enqueue opens its own write transaction; never call it while this session
    # has pending alert writes (SQLite allows only one writer).
    for policy_id in due_ids:
        try:
            enqueue(policy_id)
        except ValueError:
            pass


def recover_interrupted():
    _stopping.clear()
    with SessionLocal() as db:
        db.execute(
            update(StorageRun)
            .where(StorageRun.state.in_(["queued", "running", "cancel_requested"]))
            .values(
                state="unknown",
                completed_at=datetime.utcnow(),
                detail="NASitron restarted before a confirmed outcome. Inspect the NAS; replication can resume retained partial receives.",
            )
        )
        db.commit()


def shutdown():
    global _workers
    _stopping.set()
    if _workers is not None:
        _workers.shutdown(wait=True, cancel_futures=True)
        _workers = None

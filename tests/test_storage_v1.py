import json
from datetime import datetime, timedelta
from subprocess import CompletedProcess
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from remote import nasitron_root_helper as helper
from app.storage_jobs import retention_candidates, next_due, snapshot_payload
from app.storage_admin import validate_policy
from app.storage_insights import expansion_plan
from app.main import app
from app.db import SessionLocal
from app.models import Server, StorageHostCache, StoragePolicy, StorageRun
from app.security import csrf_token


def test_retention_cannot_touch_other_policies_holds_clones_or_children():
    rows = [
        dict(name=f"tank/data@nasitron-a-{n}", creation=str(n), holds="0", clones="-")
        for n in range(6)
    ]
    rows += [
        dict(name="tank/data@manual", creation="0", holds="0", clones="-"),
        dict(name="tank/data/child@nasitron-a-0", creation="0", holds="0", clones="-"),
    ]
    rows[0]["holds"] = "1"
    rows[1]["clones"] = "tank/recovery"
    assert retention_candidates(rows, "tank/data", "nasitron-a-", 2) == [
        "tank/data@nasitron-a-3",
        "tank/data@nasitron-a-2",
    ]


def test_cron_zone_and_bounds():
    assert next_due(
        "0 2 * * *", "America/New_York", datetime(2026, 10, 5, 0, 0)
    ) == datetime(2026, 10, 5, 6, 0)
    with pytest.raises(ValueError):
        next_due("bad cron", "UTC")
    config = dict(dataset="tank/data", keep=0, max_age_hours=48)
    with pytest.raises(ValueError):
        validate_policy("snapshot", config, "0 * * * *", "UTC")
    config.update(keep=24, dataset="tank/../data")
    with pytest.raises(ValueError):
        validate_policy("snapshot", config, "0 * * * *", "UTC")


def test_expansion_estimate_uses_smallest_member_and_parity():
    members = [dict(id="raidz2-0", guid="100", parent_guid="", group=True, role="data")]
    drives = []
    for n in range(6):
        members.append(
            dict(
                id=f"scsi-SERIAL{n}",
                guid=str(n),
                parent_guid="100",
                top_level_guid="100",
                group=False,
            )
        )
        drives.append(
            dict(
                serial=f"SERIAL{n}",
                size_bytes=(12 if n == 0 else 6) * 10**12,
                location=f"Bay {n}",
            )
        )
    result = expansion_plan(members, drives, 12 * 10**12)[0]
    assert result["current"] == 24 * 10**12 and result["projected"] == 48 * 10**12
    assert result["remaining"] == 5
    assert result["disks"][0]["location"] == "Bay 0"
    drives.pop()
    assert not expansion_plan(members, drives, 12 * 10**12)[0]["known"]


@pytest.fixture
def remote(monkeypatch):
    monkeypatch.setattr(helper, "_validate_pool", lambda name: None)
    monkeypatch.setattr(
        helper,
        "_get_property",
        lambda name, prop: "filesystem" if prop == "type" else "123",
    )
    monkeypatch.setattr(helper, "_zfs", lambda args: "")
    return lambda action, **extra: helper.action_plan(
        helper._action_request(json.dumps(dict(action=action, pool="tank", **extra)))
    )


@pytest.mark.parametrize(
    "action,extra,args",
    [
        ("dataset-create", dict(target="tank/new"), ["create", "tank/new"]),
        (
            "zvol-create",
            dict(target="tank/vol", value="100G"),
            ["create", "-V", "100G", "tank/vol"],
        ),
        (
            "dataset-set",
            dict(target="tank/data", property="compression", value="zstd"),
            ["set", "compression=zstd", "tank/data"],
        ),
        (
            "dataset-inherit",
            dict(target="tank/data", property="quota"),
            ["inherit", "quota", "tank/data"],
        ),
        (
            "snapshot-create",
            dict(target="tank/data@daily"),
            ["snapshot", "tank/data@daily"],
        ),
        (
            "snapshot-destroy",
            dict(target="tank/data@daily"),
            ["destroy", "tank/data@daily"],
        ),
        (
            "snapshot-rollback",
            dict(target="tank/data@daily"),
            ["rollback", "tank/data@daily"],
        ),
        (
            "snapshot-clone",
            dict(target="tank/data@daily", new_pool="tank/recovery"),
            [
                "clone",
                "-o",
                "canmount=noauto",
                "-o",
                "mountpoint=none",
                "tank/data@daily",
                "tank/recovery",
            ],
        ),
        ("dataset-mount", dict(target="tank/recovery"), ["mount", "tank/recovery"]),
    ],
)
def test_dataset_commands_are_structured_and_unforced(remote, action, extra, args):
    plan = remote(action, **extra)
    assert plan["args"] == args
    assert "-f" not in args and "-r" not in args


@pytest.mark.parametrize(
    "target",
    [
        "-a",
        "tank/data;reboot",
        "tank/../data",
        "tank/data with spaces",
        "/tank/data",
        "other/data",
    ],
)
def test_dataset_names_rejected(remote, target):
    with pytest.raises(helper.HelperError):
        remote("dataset-set", target=target, property="compression", value="lz4")


@pytest.mark.parametrize(
    "prop,value",
    [
        ("exec", "on"),
        ("sync", "disabled"),
        ("mountpoint", "/etc"),
        ("mountpoint", "/mnt/../etc"),
        ("recordsize", "999K"),
    ],
)
def test_property_allowlist(remote, prop, value):
    with pytest.raises(helper.HelperError):
        remote("dataset-set", target="tank/data", property=prop, value=value)


def test_hold_is_idempotent_without_removing_foreign_tags(remote, monkeypatch):
    monkeypatch.setattr(
        helper,
        "_zfs",
        lambda args: "tank/data@s\tnasitron\t123\ntank/data@s\tbackup-other\t123",
    )
    plan = remote("snapshot-hold", target="tank/data@s")
    assert plan["noop"]
    runner = Mock()
    monkeypatch.setattr(helper, "_run", runner)
    assert helper._execute_plan(plan).returncode == 0
    assert not runner.called
    plan = remote("snapshot-release", target="tank/data@s")
    assert plan["args"] == ["release", "nasitron", "tank/data@s"]


def test_receive_rejects_existing_foreign_dataset(monkeypatch, tmp_path):
    monkeypatch.setattr(helper, "LOCK_PATH", str(tmp_path / "lock"))
    monkeypatch.setattr(helper, "ZFS", lambda: "zfs")
    monkeypatch.setattr(
        helper, "_run", lambda *a, **kw: CompletedProcess([], 0, "tank/existing", "")
    )
    monkeypatch.setattr(helper, "_get_property", lambda *a: "someone-else")
    execute = Mock()
    monkeypatch.setattr(helper.subprocess, "call", execute)
    with pytest.raises(helper.HelperError, match="not owned"):
        helper.cmd_stream(["receive", "tank/existing", "a" * 32])
    assert not execute.called


def test_storage_pages_and_policy_authorization(monkeypatch):
    from app import storage_admin

    monkeypatch.setattr(storage_admin, "request_refresh", lambda sid: None)
    with TestClient(app) as client:
        client.post(
            "/login",
            data=dict(
                username="ci-admin",
                password="ci-password-strong",
                csrf_token=csrf_token(),
                next="/",
            ),
        )
        with SessionLocal() as db:
            server = Server(
                name="storage-ui-test", host="192.0.2.20", username="nas", enabled=False
            )
            db.add(server)
            db.commit()
            sid = server.id
            data = dict(
                helper_version="1.0.0",
                datasets=[
                    dict(
                        name="tank/data",
                        type="filesystem",
                        used="1000",
                        available="2000",
                        referenced="900",
                        usedbysnapshots="100",
                        usedbydataset="900",
                        usedbychildren="0",
                        usedbyrefreservation="0",
                        quota="0",
                        refquota="0",
                        mountpoint="/mnt/data",
                    )
                ],
                snapshots=[
                    dict(
                        name="tank/data@daily",
                        creation="1791158400",
                        used="100",
                        referenced="900",
                        holds="0",
                        clones="-",
                    )
                ],
                properties=[
                    dict(
                        name="tank/data",
                        property="compression",
                        value="lz4",
                        source="local",
                    )
                ],
                capabilities={},
                topology={"pools": []},
                actions=[
                    dict(id=k, label=v[0], warning=v[1])
                    for k, v in helper.STORAGE_ACTIONS.items()
                ],
            )
            db.add(
                StorageHostCache(
                    server_id=sid,
                    payload_json=json.dumps(data),
                    captured_at=datetime.utcnow(),
                )
            )
            db.commit()
        try:
            for tab in [
                "overview",
                "planner",
                "datasets",
                "snapshots",
                "jobs",
                "events",
                "host",
            ]:
                response = client.get(f"/servers/{sid}/storage?tab={tab}")
                assert response.status_code == 200, response.text
            payload = dict(
                name="Daily",
                kind="snapshot",
                dataset="tank/data",
                cron="0 2 * * *",
                timezone="UTC",
                keep="7",
                max_age_hours="48",
                confirm_text="ENABLE Daily",
                csrf_token=csrf_token(),
            )
            assert (
                client.post(
                    f"/servers/{sid}/storage/policies",
                    data={**payload, "csrf_token": "bad"},
                ).status_code
                == 403
            )
            assert (
                client.post(
                    f"/servers/{sid}/storage/policies",
                    data=payload,
                    follow_redirects=False,
                ).status_code
                == 303
            )
            with SessionLocal() as db:
                policy = db.query(StoragePolicy).filter_by(server_id=sid).one()
                assert len(json.loads(policy.config_json)["owner"]) == 32
        finally:
            with SessionLocal() as db:
                db.delete(db.get(Server, sid))
                db.commit()


def test_interrupted_runs_are_never_silently_replayed():
    from app.storage_jobs import recover_interrupted

    with SessionLocal() as db:
        server = Server(
            name="restart-test", host="192.0.2.30", username="nas", enabled=False
        )
        db.add(server)
        db.commit()
        sid = server.id
        policy = StoragePolicy(
            server_id=sid,
            name="test",
            kind="snapshot",
            cron="0 * * * *",
            timezone="UTC",
            next_run=datetime.utcnow() + timedelta(hours=1),
        )
        db.add(policy)
        db.commit()
        run = StorageRun(policy_id=policy.id, state="running")
        db.add(run)
        db.commit()
        rid = run.id
    recover_interrupted()
    with SessionLocal() as db:
        assert db.get(StorageRun, rid).state == "unknown"
        db.delete(db.get(Server, sid))
        db.commit()


def test_snapshot_pool_extraction():
    assert snapshot_payload("snapshot-create", "tank@s")["pool"] == "tank"


def test_stream_argv_has_no_force_or_shell(monkeypatch, tmp_path):
    monkeypatch.setattr(helper, "LOCK_PATH", str(tmp_path / "lock"))
    monkeypatch.setattr(helper, "ZFS", lambda: "/usr/sbin/zfs")
    monkeypatch.setattr(
        helper, "_run", lambda *a, **kw: CompletedProcess([], 1, "", "does not exist")
    )
    call = Mock(return_value=0)
    monkeypatch.setattr(helper.subprocess, "call", call)
    assert helper.cmd_stream(["receive", "backup/new", "a" * 32]) == 0
    args = call.call_args[0][0]
    assert "-u" in args and "-s" in args and "-F" not in args
    assert "readonly=on" in args and args[-1] == "backup/new"
    assert not call.call_args[1].get("shell")
    assert helper.cmd_stream(["send", "tank/data@new", "tank/data@old", ""]) == 0
    assert call.call_args[0][0] == [
        "/usr/sbin/zfs",
        "send",
        "-w",
        "-i",
        "tank/data@old",
        "tank/data@new",
    ]
    assert helper.cmd_stream(["send", "tank/data@new", "", "1-abcd-1234"]) == 0
    assert call.call_args[0][0] == ["/usr/sbin/zfs", "send", "-t", "1-abcd-1234"]
    with pytest.raises(helper.HelperError):
        helper.cmd_stream(["receive", "backup", "a" * 32])
    with pytest.raises(helper.HelperError):
        helper.cmd_stream(["send", "tank/data@new", "other/data@old", ""])


def test_helper_update_pins_digest_and_preserves_previous(monkeypatch, tmp_path):
    import hashlib

    path = tmp_path / "nasitron-root-helper"
    path.write_bytes(b"old helper")
    monkeypatch.setattr(helper, "__file__", str(path))
    content = b'print("new helper")\n'
    identity = {"commit": "a" * 40, "sha256": hashlib.sha256(content).hexdigest()}
    monkeypatch.setattr(helper, "_release_candidate", lambda: (content, identity))
    monkeypatch.setattr(helper.os, "chown", lambda *a: None)
    plan = helper.action_plan(dict(action="helper-update", pool="host"))
    changed = {**identity, "sha256": "0" * 64}
    monkeypatch.setattr(helper, "_release_candidate", lambda: (content, changed))
    with pytest.raises(helper.HelperError, match="Release changed"):
        helper._execute_plan(plan)
    assert path.read_bytes() == b"old helper"
    monkeypatch.setattr(helper, "_release_candidate", lambda: (content, identity))
    assert helper._execute_plan(plan).returncode == 0
    assert path.read_bytes() == content
    assert path.with_suffix(".previous").read_bytes() == b"old helper"


def test_replication_without_pinned_identity_cannot_connect():
    from app.storage_jobs import replicate

    source = Server(id=1, host_key_fingerprint=None)
    destination = Server(id=2, host_key_fingerprint=None)
    with pytest.raises(ValueError, match="Pin both"):
        replicate(
            source,
            destination,
            dict(dataset="tank/data", destination="backup/data", owner="a" * 32),
            "tank/data@s",
            1,
        )


@pytest.mark.parametrize("weekday", ["0", "7", "sun"])
def test_cron_sunday_uses_unix_numbering(weekday):
    assert next_due(f"0 3 * * {weekday}", "UTC", datetime(2026, 10, 5)) == datetime(
        2026, 10, 11, 3
    )


def test_cron_weekday_ranges_and_steps():
    assert next_due("0 3 * * 1-5/2", "UTC", datetime(2026, 10, 5, 4)) == datetime(
        2026, 10, 7, 3
    )
    with pytest.raises(ValueError):
        next_due("0 3 * * 8", "UTC")


def test_replication_remote_errors_are_not_missing_datasets():
    from app.storage_jobs import _remote_value, _snapshot_guids

    ssh = Mock()
    ssh.run.return_value = dict(exit=255, stdout="", stderr="SSH disconnected")
    with pytest.raises(RuntimeError, match="disconnected"):
        _remote_value(ssh, "tank/backup", "receive_resume_token")
    with pytest.raises(RuntimeError, match="disconnected"):
        _snapshot_guids(ssh, "tank/backup")


@pytest.mark.parametrize("resume", [False, True])
@pytest.mark.parametrize("advance", [False, True])
def test_replication_stream_verifies_guids_and_preserves_base(
    monkeypatch, resume, advance
):
    import base64
    import hashlib
    import shlex
    from types import SimpleNamespace
    from app import storage_jobs as jobs

    owner = "a" * 32
    old = f"tank/data@nasitron-{owner}-20261004T020000-1"
    new = f"tank/data@nasitron-{owner}-20261005T020000-2"
    fingerprint = "SHA256:" + base64.b64encode(
        hashlib.sha256(b"host-key").digest()
    ).decode().rstrip("=")
    source = Server(id=1, host_key_fingerprint=fingerprint)
    destination = Server(id=2, host_key_fingerprint=fingerprint)

    class Channel:
        closed = False

        def __init__(self, chunks):
            self.chunks, self.received, self.command = chunks, b"", ""

        def settimeout(self, value):
            pass

        def exec_command(self, value):
            self.command = value

        def recv_ready(self):
            return bool(self.chunks)

        def recv(self, size):
            return self.chunks.pop(0)

        def sendall(self, value):
            self.received += value

        def recv_stderr_ready(self):
            return False

        def exit_status_ready(self):
            return not self.chunks

        def recv_exit_status(self):
            return 0

        def shutdown_write(self):
            pass

        def close(self):
            self.closed = True

    send, receive = Channel([b"zfs-stream", b"-data"]), Channel([])

    def collector(server):
        ssh = Mock()
        ssh.__enter__ = Mock(return_value=ssh)
        ssh.__exit__ = Mock(return_value=False)
        transport = ssh.client.get_transport.return_value
        transport.get_remote_server_key.return_value.asbytes.return_value = b"host-key"
        transport.open_session.return_value = send if server.id == 1 else receive
        return ssh

    run = SimpleNamespace(bytes_sent=0, state="running")
    db = Mock()
    db.__enter__ = Mock(return_value=db)
    db.__exit__ = Mock(return_value=False)
    db.get.return_value = run
    monkeypatch.setattr(jobs, "SessionLocal", lambda: db)
    monkeypatch.setattr(jobs, "SSHCollector", collector)
    monkeypatch.setattr(
        jobs,
        "_remote_value",
        lambda ssh, target, prop: (
            owner
            if prop == "org.nasitron:replication"
            else ("resume-token" if resume else "-")
        ),
    )
    snapshots = Mock(
        side_effect=[
            {old: "101", new: "202"},
            {"backup/data@" + old.split("@")[1]: "101"},
            {
                "backup/data@" + (new if advance else old).split("@")[1]: "202"
                if advance
                else "101"
            },
        ]
    )
    monkeypatch.setattr(jobs, "_snapshot_guids", snapshots)
    actions = Mock()
    monkeypatch.setattr(jobs, "execute", actions)
    jobs._stopping.clear()
    if not advance:
        with pytest.raises(RuntimeError, match="requested snapshot|newly received"):
            jobs.replicate(
                source,
                destination,
                dict(dataset="tank/data", destination="backup/data", owner=owner),
                new,
                2,
            )
        assert send.closed and receive.closed
        assert not any(
            call.args[1]["action"] == "snapshot-release"
            for call in actions.call_args_list
        )
        return
    detail, stamp = jobs.replicate(
        source,
        destination,
        dict(dataset="tank/data", destination="backup/data", owner=owner),
        new,
        2,
    )
    assert receive.received == b"zfs-stream-data" and run.bytes_sent == 15
    assert send.closed and receive.closed
    args = shlex.split(send.command)
    assert args[-2:] == [old, "resume-token" if resume else ""]
    assert stamp == datetime(2026, 10, 5, 2)
    assert "Verified" in detail
    assert any(
        call.args[1]["action"] == "snapshot-release" and call.args[1]["target"] == old
        for call in actions.call_args_list
    )


def test_smart_test_history_and_status_survive_collection():
    from app.parser import parse_smart

    data = dict(
        ata_smart_data=dict(
            self_test=dict(
                status=dict(string="Self-test in progress"),
                polling_minutes=dict(short=2, extended=600),
            )
        ),
        ata_smart_self_test_log=dict(
            standard=dict(
                table=[
                    dict(
                        type=dict(string="Extended offline"),
                        status=dict(string="Completed without error"),
                        lifetime_hours=1200,
                    )
                ]
            )
        ),
    )
    parsed = parse_smart(json.dumps(data))
    assert parsed["self_test_status"] == "Self-test in progress"
    assert parsed["self_test_polling_minutes"]["extended"] == 600
    assert parsed["self_test_history"][0]["lifetime_hours"] == 1200


def test_smart_action_rejects_nonphysical_targets(monkeypatch):
    monkeypatch.setattr(
        helper, "_device", lambda *args, **kwargs: ("scsi-test", "/dev/dm-0")
    )
    monkeypatch.setattr(helper, "_lsblk_disk", lambda path: dict(type="lvm"))
    with pytest.raises(helper.HelperError, match="whole physical"):
        helper.action_plan(dict(action="smart-long", pool="host", target="scsi-test"))


def test_receive_checks_destination_after_lock_and_fails_closed(monkeypatch, tmp_path):
    monkeypatch.setattr(helper, "LOCK_PATH", str(tmp_path / "lock"))
    monkeypatch.setattr(helper, "ZFS", lambda: "zfs")
    locked = []
    monkeypatch.setattr(helper.fcntl, "flock", lambda *args: locked.append(True))

    def read(*args, **kwargs):
        assert locked
        return CompletedProcess([], 1, "", "permission denied")

    monkeypatch.setattr(helper, "_run", read)
    execute = Mock()
    monkeypatch.setattr(helper.subprocess, "call", execute)
    with pytest.raises(helper.HelperError, match="permission denied"):
        helper.cmd_stream(["receive", "backup/new", "a" * 32])
    assert not execute.called


def test_disk_matching_uses_resolved_paths_and_rejects_ambiguous_serials():
    from app.storage_insights import match_disk

    disks = [
        dict(path="/dev/sda", serial="ABC"),
        dict(path="/dev/sdb", serial="LONG_ABC"),
    ]
    assert match_disk(dict(id="wwn-123", realpath="/dev/sdb1"), disks) is disks[1]
    assert match_disk(dict(id="scsi-LONG_ABC"), disks) is None
    assert match_disk(dict(id="scsi-ABC123"), disks) is None


def test_expansion_nested_replacement_cannot_inflate_capacity():
    members = [
        dict(id="raidz2-0", guid="100", parent_guid="", group=True, role="data"),
        dict(
            id="replacing-0",
            guid="200",
            parent_guid="100",
            top_level_guid="100",
            group=True,
        ),
    ]
    drives = []
    for i in range(4):
        members.append(
            dict(
                id=f"scsi-SERIAL{i}",
                guid=str(300 + i),
                parent_guid="200" if i < 2 else "100",
                top_level_guid="100",
                group=False,
            )
        )
        drives.append(dict(serial=f"SERIAL{i}", size_bytes=6 * 10**12))
    assert not expansion_plan(members, drives, 12 * 10**12)[0]["known"]


def test_storage_tick_commits_alerts_before_enqueuing(monkeypatch):
    from app import storage_jobs as jobs
    from app.models import Alert

    ids = []
    with SessionLocal() as db:
        server = Server(
            name="tick-lock-test", host="192.0.2.100", username="nas", enabled=False
        )
        db.add(server)
        db.flush()
        sid = server.id
        for n in range(2):
            policy = StoragePolicy(
                server_id=sid,
                name=f"overdue-{n}",
                kind="snapshot",
                cron="0 * * * *",
                timezone="UTC",
                next_run=datetime.utcnow() - timedelta(hours=2),
                created_at=datetime.utcnow() - timedelta(days=3),
            )
            db.add(policy)
            db.flush()
            ids.append(policy.id)
        db.commit()
    calls = []

    def enqueue(pid):
        if pid not in ids:
            return
        with SessionLocal() as db:
            assert db.query(Alert).filter_by(server_id=sid).count() == 2
            db.add(StorageRun(policy_id=pid))
            db.commit()
        calls.append(pid)

    monkeypatch.setattr(jobs, "enqueue", enqueue)
    try:
        jobs.tick()
        assert calls == ids
    finally:
        with SessionLocal() as db:
            db.delete(db.get(Server, sid))
            db.commit()


def test_diagnostics_accept_null_io_samples():
    from app.storage_insights import diagnose

    db = Mock()
    db.scalars.return_value.all.return_value = []
    db.execute.return_value.all.return_value = []
    rows = diagnose(
        db,
        1,
        dict(drives=[dict(serial="ABC", io=dict(read_iops=None, write_iops=None))]),
        datetime.utcnow(),
    )
    assert len(rows) == 1 and rows[0]["reasons"] == []


def test_cancelled_replication_does_not_raise_failure_alert(monkeypatch):
    from app import storage_jobs as jobs
    from app.models import Alert

    with SessionLocal() as db:
        source = Server(
            name="cancel-source", host="192.0.2.110", username="nas", enabled=False
        )
        destination = Server(
            name="cancel-dest", host="192.0.2.111", username="nas", enabled=False
        )
        db.add_all([source, destination])
        db.flush()
        sid, did = source.id, destination.id
        policy = StoragePolicy(
            server_id=sid,
            name="cancel-test",
            kind="replication",
            cron="0 * * * *",
            timezone="UTC",
            next_run=datetime.utcnow() + timedelta(hours=1),
            config_json=json.dumps(
                dict(
                    owner="a" * 32,
                    dataset="tank/data",
                    destination_server=did,
                    destination="backup/data",
                    keep=7,
                )
            ),
        )
        db.add(policy)
        db.flush()
        run = StorageRun(policy_id=policy.id)
        db.add(run)
        db.commit()
        pid, rid = policy.id, run.id
    monkeypatch.setattr(jobs, "execute", Mock())
    monkeypatch.setattr(
        jobs,
        "replicate",
        Mock(
            side_effect=jobs.TransferCancelled(
                "Transfer cancelled; partial receive retained"
            )
        ),
    )
    try:
        jobs.run_policy(pid, rid)
        with SessionLocal() as db:
            assert db.get(StorageRun, rid).state == "cancelled"
            assert db.get(StoragePolicy, pid).last_success is None
            assert db.query(Alert).filter_by(server_id=sid).count() == 0
    finally:
        with SessionLocal() as db:
            db.delete(db.get(Server, sid))
            db.delete(db.get(Server, did))
            db.commit()

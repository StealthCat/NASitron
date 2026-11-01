"""Regression tests for storage-operation and remote-helper review fixes."""
import base64
import hashlib
import json
import subprocess
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import Mock

import pytest

from remote import nasitron_root_helper as helper
from app import storage_jobs as jobs
from app.db import SessionLocal, init_db
from app.maintenance import _is_path_on_disk
from app.models import Server, StoragePolicy, StorageRun

OWNER = "a" * 32


def test_embedded_helper_matches_source_after_review_changes():
    text = Path("scripts/install-remote.sh").read_text()
    start = text.index("__NASITRON_ROOT_HELPER__'\n") + len("__NASITRON_ROOT_HELPER__'\n")
    end = text.index("\n__NASITRON_ROOT_HELPER__\n", start)
    assert text[start:end] == Path("remote/nasitron_root_helper.py").read_text().rstrip()


def test_stream_lock_contention_is_a_helper_error(monkeypatch, tmp_path):
    monkeypatch.setattr(helper, "LOCK_PATH", str(tmp_path / "missing" / "lock"))

    def busy(*args):
        raise BlockingIOError()

    monkeypatch.setattr(helper.fcntl, "flock", busy)
    with pytest.raises(helper.HelperError, match="already|running"):
        helper.cmd_stream(["receive", "backup/new", OWNER])


def _receive_host(monkeypatch, tmp_path, listing, set_code=0):
    monkeypatch.setattr(helper, "LOCK_PATH", str(tmp_path / "lock"))
    monkeypatch.setattr(helper, "ZFS", lambda: "zfs")
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        if args[1] == "list" and "name,type" in args:
            return CompletedProcess(args, 0, listing, "")
        if args[1] == "list":
            return CompletedProcess(args, 1, "", "dataset does not exist")
        if args[1] == "set":
            return CompletedProcess(args, set_code, "", "set failed" if set_code else "")
        raise AssertionError(args)

    monkeypatch.setattr(helper, "_run", run)
    monkeypatch.setattr(helper.subprocess, "call", Mock(return_value=0))
    return calls


def test_received_filesystems_never_automount_or_honour_setuid(monkeypatch, tmp_path):
    calls = _receive_host(
        monkeypatch,
        tmp_path,
        "backup/new\tfilesystem\nbackup/new/child\tfilesystem\nbackup/new/vol\tvolume\n",
    )
    assert helper.cmd_stream(["receive", "backup/new", OWNER]) == 0
    sets = [c for c in calls if c[1] == "set"]
    assert [c[-1] for c in sets] == ["backup/new", "backup/new/child"]
    for c in sets:
        assert {"canmount=noauto", "setuid=off", "devices=off"} <= set(c)


def test_received_dataset_hardening_failure_fails_closed(monkeypatch, tmp_path):
    _receive_host(monkeypatch, tmp_path, "backup/new\tfilesystem\n", set_code=1)
    with pytest.raises(helper.HelperError, match="set failed"):
        helper.cmd_stream(["receive", "backup/new", OWNER])


@pytest.fixture
def dataset_host(monkeypatch):
    props = {}
    monkeypatch.setattr(helper, "_validate_pool", lambda name: None)
    monkeypatch.setattr(
        helper, "_get_property", lambda name, prop: props.get((name, prop), "-")
    )
    sources = {}
    monkeypatch.setattr(
        helper,
        "_zfs",
        lambda args: sources.get(args[-1], "inherited from tank") if args[:2] == ["get", "-H"] else "",
    )

    def plan(action, **extra):
        return helper.action_plan(
            helper._action_request(json.dumps(dict(action=action, pool="tank", **extra)))
        )

    return props, sources, plan


def test_replicated_dataset_cannot_mount_at_inherited_path(dataset_host):
    props, sources, plan = dataset_host
    props[("tank/etc", "org.nasitron:replication")] = OWNER
    props[("tank/etc", "mountpoint")] = "/tank/etc"
    with pytest.raises(helper.HelperError, match="Replicated data"):
        plan("dataset-mount", target="tank/etc")
    with pytest.raises(helper.HelperError, match="Replicated data"):
        plan("dataset-inherit", target="tank/etc", property="mountpoint")
    props[("tank/etc", "mountpoint")] = "/mnt/backup-view"
    sources["tank/etc"] = "local"
    assert plan("dataset-mount", target="tank/etc")["args"] == ["mount", "tank/etc"]


def test_clone_of_replicated_data_is_also_restricted(dataset_host):
    props, sources, plan = dataset_host
    props[("tank/backup", "org.nasitron:replication")] = OWNER
    props[("tank/clone2", "origin")] = "tank/clone1@s"
    props[("tank/clone1", "origin")] = "tank/backup@nasitron-x"
    props[("tank/clone2", "mountpoint")] = "/tank/clone2"
    with pytest.raises(helper.HelperError, match="Replicated data"):
        plan("dataset-mount", target="tank/clone2")
    # Ordinary datasets keep their normal mount behaviour.
    props[("tank/data", "mountpoint")] = "/tank/data"
    assert plan("dataset-mount", target="tank/data")["args"] == ["mount", "tank/data"]


def test_mountpoints_cannot_traverse_symlinks(dataset_host, monkeypatch):
    props, _sources, plan = dataset_host
    real = helper.os.path.realpath
    monkeypatch.setattr(
        helper.os.path,
        "realpath",
        lambda p: "/etc/systemd/system/x.service.d" if p == "/mnt/a/b" else real(p),
    )
    with pytest.raises(helper.HelperError, match="symbolic"):
        plan("dataset-set", target="tank/data", property="mountpoint", value="/mnt/a/b")
    props[("tank/data", "mountpoint")] = "/mnt/a/b"
    with pytest.raises(helper.HelperError, match="symbolic"):
        plan("dataset-mount", target="tank/data")


def test_recovery_clone_disables_setuid_and_devices(monkeypatch):
    monkeypatch.setattr(helper, "ZFS", lambda: "zfs")
    calls = []
    monkeypatch.setattr(
        helper, "_run", lambda args, **kw: calls.append(args) or CompletedProcess(args, 0, "", "")
    )
    args = ["clone", "-o", "canmount=noauto", "-o", "mountpoint=none", "tank/b@s", "tank/r"]
    assert helper._execute_plan({"kind": "zfs", "args": args}).returncode == 0
    assert calls[-1] == ["zfs", "set", "setuid=off", "devices=off", "tank/r"]
    calls.clear()
    monkeypatch.setattr(
        helper,
        "_run",
        lambda args, **kw: calls.append(args)
        or CompletedProcess(args, 1 if args[1] == "set" else 0, "", "nope"),
    )
    assert helper._execute_plan({"kind": "zfs", "args": args}).returncode == 1
    # Volume clones (readonly) need no filesystem-only properties.
    calls.clear()
    volume = ["clone", "-o", "readonly=on", "tank/v@s", "tank/rv"]
    helper._execute_plan({"kind": "zfs", "args": volume})
    assert all(c[1] != "set" for c in calls)


@pytest.mark.parametrize(
    "token,real,member",
    [
        ("/dev/sda1", "/dev/sda", True),
        ("/dev/sda", "/dev/sda", True),
        ("/dev/sdap", "/dev/sda", False),
        ("/dev/sdap1", "/dev/sda", False),
        ("/dev/nvme0n1p1", "/dev/nvme0n1", True),
        ("/dev/nvme0n10", "/dev/nvme0n1", False),
        ("/dev/nvme0n10p1", "/dev/nvme0n1", False),
    ],
)
def test_partition_matching_is_exact(monkeypatch, token, real, member):
    assert _is_path_on_disk(token, real) is member
    monkeypatch.setattr(helper, "_device", lambda *a, **kw: (a[0], real))
    monkeypatch.setattr(
        helper, "_lsblk_disk", lambda p: {"type": "disk", "size": 6000, "kname": real[5:]}
    )
    monkeypatch.setattr(helper, "WIPEFS", lambda: "wipefs")
    monkeypatch.setattr(helper, "ZPOOL", lambda: "zpool")
    monkeypatch.setattr(
        helper,
        "_run",
        lambda args, **kw: CompletedProcess(args, 0, f"  tank ONLINE\n    {token} ONLINE\n" if args[0] == "zpool" else "", ""),
    )
    if member:
        with pytest.raises(helper.HelperError, match="already a ZFS member"):
            helper._validate_blank_disk("/dev/disk/by-id/scsi-new")
    else:
        assert helper._validate_blank_disk("/dev/disk/by-id/scsi-new") == (real, 6000)


def test_snapshot_guid_listing_is_not_recursive():
    from app.storage_jobs import _snapshot_guids

    ssh = Mock()
    ssh.run.return_value = dict(exit=0, stdout="tank/d@s\t1\n", stderr="")
    assert _snapshot_guids(ssh, "tank/d") == {"tank/d@s": "1"}
    command = ssh.run.call_args[0][0]
    assert "-d 1" in command and " -r " not in command


def test_replication_does_not_drop_data_buffered_before_exit_status(monkeypatch):
    owner = OWNER
    old = f"tank/data@nasitron-{owner}-20261004T020000-1"
    new = f"tank/data@nasitron-{owner}-20261005T020000-2"
    fingerprint = "SHA256:" + base64.b64encode(
        hashlib.sha256(b"host-key").digest()
    ).decode().rstrip("=")

    class Sender:
        closed = False

        def __init__(self):
            self.chunks, self.polls = [b"late-stream-bytes"], 0

        def settimeout(self, value):
            pass

        def exec_command(self, value):
            pass

        def recv_ready(self):
            # The first poll misses data that arrives together with exit status.
            self.polls += 1
            return self.polls > 1 and bool(self.chunks)

        def recv(self, size):
            return self.chunks.pop(0) if self.chunks else b""

        def recv_stderr_ready(self):
            return False

        def exit_status_ready(self):
            return True

        def recv_exit_status(self):
            return 0

        def close(self):
            self.closed = True

    class Receiver(Sender):
        def __init__(self):
            self.chunks, self.polls, self.received = [], 0, b""

        def sendall(self, value):
            self.received += value

        def shutdown_write(self):
            pass

    send, receive = Sender(), Receiver()

    def collector(server):
        ssh = Mock()
        ssh.__enter__ = Mock(return_value=ssh)
        ssh.__exit__ = Mock(return_value=False)
        transport = ssh.client.get_transport.return_value
        transport.get_remote_server_key.return_value.asbytes.return_value = b"host-key"
        transport.open_session.return_value = send if server.id == 1 else receive
        return ssh

    run = Mock(bytes_sent=0, state="running")
    db = Mock()
    db.__enter__ = Mock(return_value=db)
    db.__exit__ = Mock(return_value=False)
    db.get.return_value = run
    monkeypatch.setattr(jobs, "SessionLocal", lambda: db)
    monkeypatch.setattr(jobs, "SSHCollector", collector)
    monkeypatch.setattr(
        jobs,
        "_remote_value",
        lambda ssh, target, prop: owner if prop == "org.nasitron:replication" else "-",
    )
    monkeypatch.setattr(
        jobs,
        "_snapshot_guids",
        Mock(
            side_effect=[
                {old: "101", new: "202"},
                {"backup/data@" + old.split("@")[1]: "101"},
                {
                    "backup/data@" + old.split("@")[1]: "101",
                    "backup/data@" + new.split("@")[1]: "202",
                },
            ]
        ),
    )
    monkeypatch.setattr(jobs, "execute", Mock())
    jobs._stopping.clear()
    jobs.replicate(
        Server(id=1, host_key_fingerprint=fingerprint),
        Server(id=2, host_key_fingerprint=fingerprint),
        dict(dataset="tank/data", destination="backup/data", owner=owner),
        new,
        2,
    )
    assert receive.received == b"late-stream-bytes"


@pytest.mark.parametrize("state,expected", [("cancel_requested", "cancelled"), ("unknown", "unknown")])
def test_run_policy_only_starts_queued_runs(monkeypatch, state, expected):
    init_db()
    with SessionLocal() as db:
        server = Server(name=f"review-ops-{state}-{uuid.uuid4().hex[:8]}", host="192.0.2.120", username="nas", enabled=False)
        db.add(server)
        db.flush()
        policy = StoragePolicy(
            server_id=server.id,
            name="review",
            kind="snapshot",
            cron="0 * * * *",
            timezone="UTC",
            next_run=datetime(2030, 1, 1) + timedelta(hours=1),
            config_json=json.dumps(dict(owner=OWNER, dataset="tank/data", keep=2)),
        )
        db.add(policy)
        db.flush()
        run = StorageRun(policy_id=policy.id, state=state)
        db.add(run)
        db.commit()
        sid, pid, rid = server.id, policy.id, run.id
    execute = Mock()
    monkeypatch.setattr(jobs, "execute", execute)
    try:
        jobs.run_policy(pid, rid)
        assert not execute.called
        with SessionLocal() as db:
            assert db.get(StorageRun, rid).state == expected
    finally:
        with SessionLocal() as db:
            db.delete(db.get(Server, sid))
            db.commit()


@pytest.mark.parametrize(
    "path,ok",
    [
        ("/usr/local/sbin/nasitron-root-helper", True),
        ("/opt/nasitron/helper.py", True),
        ("/usr/local/sbin/x,ALL", False),
        ("/usr/local/sbin/a:b", False),
        ("/usr/local/../tmp/helper", False),
        ("/usr/local/./sbin/helper", False),
        ("/usr/local/sbin/x\\", False),
        ("/usr/local/sbin/", False),
    ],
)
def test_installer_rejects_sudoers_metacharacters_in_helper_path(path, ok):
    line = next(
        line
        for line in Path("scripts/install-remote.sh").read_text().splitlines()
        if '"$HELPER_PATH" =~' in line
    )
    script = 'die() { exit 3; }\nHELPER_PATH="$1"\n' + line + "\n"
    result = subprocess.run(["bash", "-c", script, "check", path])
    assert (result.returncode == 0) is ok

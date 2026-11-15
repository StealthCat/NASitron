"""Regressions for the November 2026 security alert remediation."""
import hashlib
import stat
from pathlib import Path

import paramiko
import pytest

from app.security import local_redirect, safe_next_url
from app.storage_admin import valid_dataset_name
from remote import nasitron_root_helper as helper


@pytest.mark.parametrize("value", [
    "https://outside.example/", "//outside.example/", "///outside.example/",
    "/\\outside.example/", "/\t/outside.example/", "/\r/outside.example/",
    "/\n/outside.example/", "/path\x00", "javascript:alert(1)", "relative/path",
])
def test_redirects_reject_external_and_browser_normalized_urls(value):
    assert local_redirect(value).headers["location"] == "/"


def test_redirects_preserve_local_query_and_fragment():
    value = "/servers/7/drive?identity=disk%2Fone&message=https%3A%2F%2Fexample.com#health"
    response = local_redirect(value, 303)
    assert response.status_code == 303
    assert response.headers["location"] == value
    assert safe_next_url("/" + "a" * 4096) == "/" + "a" * 2047


@pytest.mark.parametrize("value", ["tank/data", "tank/backup/2026-11", "tank-1/data:archive"])
def test_replication_accepts_exact_child_datasets(value):
    assert valid_dataset_name(value, child=True)


@pytest.mark.parametrize("value", [
    "tank", "tank/", "tank//data", "tank/../data", "tank/@snapshot", "tank/*",
    "tank/data\n", "tank/" + "a" * 100000, "A" * 100000 + "!", None,
])
def test_replication_rejects_invalid_or_unbounded_destinations(value):
    assert not valid_dataset_name(value, child=True)


def test_helper_rollback_keeps_installed_and_backup_files_root_only(tmp_path, monkeypatch):
    installed = tmp_path / "nasitron-root-helper"
    installed.write_bytes(b"current helper")
    backup = installed.with_suffix(".previous")
    backup.write_bytes(b"previous helper")
    monkeypatch.setattr(helper, "__file__", str(installed))
    monkeypatch.setattr(helper, "_helper_backup", lambda: backup)
    owners = []
    monkeypatch.setattr(helper.os, "chown", lambda path, uid, gid: owners.append((uid, gid)))
    result = helper._execute_plan({
        "kind": "helper", "request": {"action": "helper-rollback"},
        "identity": {"sha256": hashlib.sha256(b"previous helper").hexdigest()},
    })
    assert result.returncode == 0
    assert installed.read_bytes() == b"previous helper"
    assert backup.read_bytes() == b"current helper"
    assert owners == [(0, 0), (0, 0)]
    for path in (installed, backup):
        assert stat.S_IMODE(path.stat().st_mode) == 0o700
    assert not list(tmp_path.glob(".nasitron-*"))


def test_paramiko_rsa_uses_sha2_and_rejects_sha1():
    key = paramiko.RSAKey.generate(2048)
    signature = key.sign_ssh_data(b"NASitron SSH regression")
    assert paramiko.Message(signature.asbytes()).get_text() == "rsa-sha2-512"
    assert key.verify_ssh_sig(b"NASitron SSH regression", paramiko.Message(signature.asbytes()))
    with pytest.raises(KeyError):
        key.sign_ssh_data(b"NASitron SSH regression", algorithm="ssh-rsa")


def test_packaged_helper_matches_installer():
    installer = Path("scripts/install-remote.sh").read_text()
    embedded = installer.split("__NASITRON_ROOT_HELPER__'\n", 1)[1].split("\n__NASITRON_ROOT_HELPER__\n", 1)[0]
    assert embedded == Path("remote/nasitron_root_helper.py").read_text().rstrip("\n")

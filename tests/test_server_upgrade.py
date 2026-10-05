import hashlib
import html
import os
from pathlib import Path
import re
import subprocess

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.main import app
from app.db import SessionLocal
from app.models import Server, RemoteEnrollment, WebUser
from app.security import csrf_token
from app.settings_store import set_setting, get_setting


@pytest.fixture
def upgrade_web():
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
                name="Upgrade NAS",
                host="192.0.2.123",
                username="custom-monitor",
                enabled=False,
            )
            db.add(server)
            db.commit()
            sid = server.id
        yield client, sid
        with SessionLocal() as db:
            db.delete(db.get(Server, sid))
            db.commit()


@pytest.mark.parametrize("mode,flag", [("internal", "-kfsSL"), ("acme", "-fsSL")])
def test_upgrade_command_preserves_existing_registration(
    upgrade_web, mode, flag, tmp_path
):
    client, sid = upgrade_web
    with SessionLocal() as db:
        original = get_setting(db, "tls_mode")
        set_setting(db, "tls_mode", mode)
        db.commit()
        count = db.query(RemoteEnrollment).count()
    try:
        index = client.get("/servers")
        assert 'href="/servers/upgrade">Upgrade Server</a>' in index.text
        response = client.get(f"/servers/upgrade?server_id={sid}")
        assert response.status_code == 200
        command = html.unescape(
            re.search(r'<code id="upgrade-command">(.*?)</code>', response.text, re.S)[
                1
            ]
        )
        assert "--upgrade-only --user custom-monitor" in command
        assert "--enroll" not in command and "--public-key" not in command
        assert (
            hashlib.sha256(Path("scripts/install-remote.sh").read_bytes()).hexdigest()
            in command
        )
        assert flag in command
        assert (
            subprocess.run(
                ["bash", "-n"], input=command, text=True, capture_output=True
            ).returncode
            == 0
        )
        # Execute only the generated downloader/verification wrapper. Fake curl
        # supplies the packaged bytes; fake sudo records that verification succeeded.
        bindir = tmp_path / "bin"
        bindir.mkdir()
        curl = bindir / "curl"
        curl.write_text(
            "#!/usr/bin/env python3\nimport shutil,sys\nshutil.copyfile("
            + repr(str(Path("scripts/install-remote.sh").resolve()))
            + ",sys.argv[sys.argv.index('-o')+1])\n"
        )
        curl.chmod(0o755)
        sudo = bindir / "sudo"
        marker = tmp_path / "verified"
        sudo.write_text(
            "#!/usr/bin/env python3\nfrom pathlib import Path\nPath("
            + repr(str(marker))
            + ").write_text('verified')\n"
        )
        sudo.chmod(0o755)
        result = subprocess.run(
            ["bash", "-c", command],
            env={**os.environ, "PATH": str(bindir) + ":" + os.environ["PATH"]},
            text=True,
            capture_output=True,
        )
        assert result.returncode == 0, result.stderr
        assert marker.read_text() == "verified"
        assert "PRIVATE KEY" not in response.text
        with SessionLocal() as db:
            assert db.query(RemoteEnrollment).count() == count
            assert db.get(Server, sid).username == "custom-monitor"
        assert client.get("/servers/upgrade?server_id=999999999").status_code == 404
    finally:
        with SessionLocal() as db:
            set_setting(db, "tls_mode", original)
            db.commit()


@pytest.mark.parametrize("role", ["operator", "viewer"])
def test_upgrade_is_admin_only(upgrade_web, role):
    client, sid = upgrade_web
    with SessionLocal() as db:
        user = db.scalar(select(WebUser).where(WebUser.username == "ci-admin"))
        user.is_admin = False
        user.role = role
        db.commit()
    try:
        assert client.get(f"/servers/upgrade?server_id={sid}").status_code == 403
        assert 'href="/servers/upgrade"' not in client.get("/servers").text
    finally:
        with SessionLocal() as db:
            user = db.scalar(select(WebUser).where(WebUser.username == "ci-admin"))
            user.is_admin = True
            user.role = "admin"
            db.commit()


def test_upgrade_only_rejects_key_rotation():
    response = subprocess.run(
        [
            "bash",
            "scripts/install-remote.sh",
            "--upgrade-only",
            "--public-key",
            "ssh-ed25519 example",
        ],
        capture_output=True,
        text=True,
    )
    assert response.returncode != 0
    assert "cannot change SSH keys or enroll" in response.stderr


@pytest.mark.parametrize("existing", [True, False])
def test_upgrade_installer_preserves_keys_and_never_enrolls(tmp_path, existing):
    # Isolate every write in a temporary directory. Simulate root ownership commands
    # so this regression test also runs as an unprivileged CI user.
    bindir = tmp_path / "bin"
    bindir.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    keys = home / "authorized_keys"
    keys.write_text("existing-key-that-must-remain\n")
    target = tmp_path / "sbin" / "nasitron-root-helper"
    target.parent.mkdir()
    if existing:
        target.write_text("old helper")
    sudoers = tmp_path / "nasitron.sudoers"

    def stub(name, body):
        p = bindir / name
        p.write_text("#!/usr/bin/env python3\n" + body)
        p.chmod(0o755)

    for name in [
        "apt-get",
        "sshd",
        "ssh-keygen",
        "sudo",
        "zpool",
        "zfs",
        "smartctl",
        "useradd",
        "passwd",
        "systemctl",
    ]:
        stub(
            name,
            "raise SystemExit('Unexpected account, package, SSH, or enrollment operation')\n",
        )
    stub("visudo", "raise SystemExit(0)\n")
    stub("getent", f'print("custom-monitor:x:1000:1000::{home}:/bin/bash")\n')
    stub(
        "install",
        """import subprocess,sys
args=sys.argv[1:]; clean=[]
while args:
    item=args.pop(0)
    if item in ['-o','-g']: args.pop(0)
    else: clean.append(item)
raise SystemExit(subprocess.call(['/usr/bin/install',*clean]))
""",
    )
    script = Path("scripts/install-remote.sh").read_text()
    script = script.replace('[[ "$EUID" -eq 0 ]]', "true")
    script = script.replace("/etc/sudoers.d/nasitron", str(sudoers))
    script = script.replace(
        "exec 9>/run/lock/nasitron-zpool-replace.lock", f"exec 9>{tmp_path}/helper.lock"
    )
    fixture = tmp_path / "installer.sh"
    fixture.write_text(script)
    env = {**os.environ, "PATH": str(bindir) + ":" + os.environ["PATH"]}
    for key in [
        "NASITRON_SSH_PUBLIC_KEY",
        "NASITRON_ENROLL_URL",
        "NASITRON_ENROLL_SECRET",
    ]:
        env.pop(key, None)
    response = subprocess.run(
        [
            "bash",
            str(fixture),
            "--upgrade-only",
            "--user",
            "custom-monitor",
            "--helper-path",
            str(target),
        ],
        env=env,
        capture_output=True,
        text=True,
    )
    assert response.returncode == 0, response.stderr
    assert keys.read_text() == "existing-key-that-must-remain\n"
    assert target.read_text() == Path("remote/nasitron_root_helper.py").read_text()
    assert f"custom-monitor ALL=(root) NOPASSWD: {target}" in sudoers.read_text()
    assert "preserved" in response.stdout
    if existing:
        assert Path(str(target) + ".previous").read_text() == "old helper"
    assert not list(target.parent.glob(".nasitron-helper.*"))

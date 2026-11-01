"""Regression tests for core web/security fixes found during code review."""

from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import re
import secrets
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import app.main as main
import app.security as security
from app.db import SessionLocal, utcnow
from app.main import app
from app.models import RemoteEnrollment, Server, Setting, WebUser
from app.security import (
    create_session_token,
    csrf_token,
    hash_password,
    safe_next_url,
    verify_csrf,
    verify_session_token,
)
from app.support import support_bundle_lock
from app.tls_manager import TLSConfigurationError, build_caddyfile, validate_acme_directory_url


def _login(client, username="ci-admin", password="ci-password-strong"):
    response = client.post(
        "/login",
        data={"username": username, "password": password, "csrf_token": csrf_token(), "next": "/"},
        follow_redirects=False,
    )
    assert response.status_code == 303


@pytest.fixture
def admin_client():
    with TestClient(app) as client:
        _login(client)
        yield client


@pytest.fixture
def disabled_server():
    with SessionLocal() as db:
        server = Server(
            name="review-core-" + secrets.token_hex(4), host="127.0.0.1",
            username="nasitron", enabled=False,
        )
        db.add(server)
        db.commit()
        sid = server.id
    yield sid
    with SessionLocal() as db:
        row = db.get(Server, sid)
        if row:
            db.delete(row)
            db.commit()


def test_utcnow_is_naive_utc():
    value = utcnow()
    assert value.tzinfo is None
    assert abs(datetime.now(timezone.utc).replace(tzinfo=None) - value) < timedelta(seconds=5)


@pytest.mark.parametrize("value", ["/\\evil.example", "/\\/evil.example", "/\t/evil.example",
                                   "/\n/evil.example", "/ok\x7f"])
def test_safe_next_url_rejects_browser_normalised_offsite_targets(value):
    assert safe_next_url(value) == "/"


def test_safe_next_url_keeps_local_paths():
    assert safe_next_url("/pools?server=1") == "/pools?server=1"


def test_login_ignores_backslash_next():
    with TestClient(app) as client:
        response = client.post(
            "/login",
            data={"username": "ci-admin", "password": "ci-password-strong",
                  "csrf_token": csrf_token(), "next": "/\\evil.example"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/"


def test_non_ascii_csrf_token_is_rejected_not_500(admin_client):
    assert verify_csrf("é" * 64) is False
    response = admin_client.post("/alerts/1/ack", data={"csrf_token": "é" * 64})
    assert response.status_code == 403


def test_session_token_with_non_ascii_username_does_not_crash():
    with SessionLocal() as db:
        user = WebUser(username="rëview-" + secrets.token_hex(3), password_hash=hash_password("x" * 12),
                       role="viewer", enabled=True, session_version=1)
        db.add(user)
        db.commit()
        try:
            token = create_session_token(user)
            assert verify_session_token(token, db).id == user.id
        finally:
            db.delete(user)
            db.commit()


def test_auth_redirects_and_role_denials_carry_security_headers():
    with TestClient(app) as client:
        redirect = client.get("/", follow_redirects=False)
        assert redirect.status_code == 303
        assert redirect.headers["cache-control"] == "no-store"
        assert "frame-ancestors 'none'" in redirect.headers["content-security-policy"]
    username = "review-viewer-" + secrets.token_hex(3)
    with SessionLocal() as db:
        user = WebUser(username=username, password_hash=hash_password("viewer-password-1"),
                       role="viewer", enabled=True, session_version=1)
        db.add(user)
        db.commit()
        uid = user.id
    try:
        with TestClient(app) as client:
            _login(client, username, "viewer-password-1")
            denied = client.get("/settings")
            assert denied.status_code == 403
            assert denied.headers["x-frame-options"] == "DENY"
            assert denied.headers["cache-control"] == "no-store"
    finally:
        with SessionLocal() as db:
            db.delete(db.get(WebUser, uid))
            db.commit()


def test_cross_site_state_change_is_refused(admin_client):
    for site in ("cross-site", "same-site"):
        response = admin_client.post(
            "/logout", data={"csrf_token": csrf_token()},
            headers={"Sec-Fetch-Site": site}, follow_redirects=False,
        )
        assert response.status_code == 403
    # Same-origin browser requests and non-browser clients are unaffected.
    assert admin_client.get("/", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 200
    same = admin_client.post("/logout", data={"csrf_token": csrf_token()},
                             headers={"Sec-Fetch-Site": "same-origin"}, follow_redirects=False)
    assert same.status_code == 303


def _new_enrollment(client):
    page = client.get("/servers/new")
    assert page.status_code == 200
    return re.search(r'data-enrollment-id="([A-Za-z0-9_-]+)"', page.text).group(1)


def test_completed_enrollment_drops_secrets_and_stale_ones_are_pruned(admin_client):
    stale_id = "stale-" + secrets.token_hex(10)
    with SessionLocal() as db:
        db.add(RemoteEnrollment(id=stale_id, secret_enc="x", public_key="k", private_key_enc="x",
                                expires_at=utcnow() - timedelta(days=2)))
        db.commit()
    enrollment_id = _new_enrollment(admin_client)
    server_id = None
    try:
        with SessionLocal() as db:
            assert db.get(RemoteEnrollment, stale_id) is None
            secret = main.decrypt(db.get(RemoteEnrollment, enrollment_id).secret_enc)
        payload = json.dumps({"host": "127.0.0.253", "hostname": "review-enrolled-" + secrets.token_hex(3),
                              "port": 22, "username": "nasitron"}).encode()
        signature = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
        headers = {"Content-Type": "application/json", "X-NASitron-Enrollment-Signature": signature}
        done = admin_client.post(f"/api/enroll/{enrollment_id}/complete", content=payload, headers=headers)
        assert done.status_code == 200
        server_id = done.json()["server_id"]
        with SessionLocal() as db:
            row = db.get(RemoteEnrollment, enrollment_id)
            assert row.used_at is not None and row.server_id == server_id
            assert row.secret_enc == "" and row.private_key_enc == ""
            assert main.decrypt(db.get(Server, server_id).private_key_enc)
        assert admin_client.post(f"/api/enroll/{enrollment_id}/complete", content=payload,
                                 headers=headers).status_code == 409
    finally:
        with SessionLocal() as db:
            for model, key in ((RemoteEnrollment, enrollment_id), (RemoteEnrollment, stale_id),
                               (Server, server_id)):
                row = db.get(model, key) if key is not None else None
                if row:
                    db.delete(row)
            db.commit()


def test_enrollment_claim_is_atomic(admin_client, monkeypatch):
    """A callback that lost the race (row claimed after it was read) gets 409."""
    enrollment_id = _new_enrollment(admin_client)
    with SessionLocal() as db:
        secret = main.decrypt(db.get(RemoteEnrollment, enrollment_id).secret_enc)
    payload = json.dumps({"host": "127.0.0.252", "hostname": "review-race", "port": 22}).encode()
    signature = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    real_parse = main.SSHCollector.parse_private_key

    def claim_elsewhere(text, passphrase):
        with SessionLocal() as other:
            other.get(RemoteEnrollment, enrollment_id).used_at = utcnow()
            other.commit()
        return real_parse(text, passphrase)

    monkeypatch.setattr(main.SSHCollector, "parse_private_key", staticmethod(claim_elsewhere))
    try:
        response = admin_client.post(
            f"/api/enroll/{enrollment_id}/complete", content=payload,
            headers={"Content-Type": "application/json", "X-NASitron-Enrollment-Signature": signature},
        )
        assert response.status_code == 409
        with SessionLocal() as db:
            assert db.query(Server).filter(Server.host == "127.0.0.252").count() == 0
    finally:
        with SessionLocal() as db:
            row = db.get(RemoteEnrollment, enrollment_id)
            if row:
                db.delete(row)
            db.commit()


def test_support_bundle_busy_and_ssh_failure_are_client_errors(admin_client, disabled_server, monkeypatch):
    with support_bundle_lock(disabled_server):
        busy = admin_client.post(f"/servers/{disabled_server}/support-bundle",
                                 data={"csrf_token": csrf_token()})
    assert busy.status_code == 409

    class Failing:
        def __init__(self, server):
            pass

        def __enter__(self):
            raise main.CollectorError("connection refused")

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(main, "SSHCollector", Failing)
    failed = admin_client.post(f"/servers/{disabled_server}/support-bundle",
                               data={"csrf_token": csrf_token()})
    assert failed.status_code == 502
    assert "connection refused" in failed.json()["detail"]


def test_settings_page_survives_undecryptable_secret(admin_client):
    with SessionLocal() as db:
        row = db.get(Setting, "smtp_password")
        original = (row.value, row.secret) if row else None
        if row is None:
            row = Setting(key="smtp_password", value="", secret=True)
            db.add(row)
        row.value, row.secret = "not-a-fernet-token", True
        db.commit()
    try:
        assert admin_client.get("/settings").status_code == 200
    finally:
        with SessionLocal() as db:
            row = db.get(Setting, "smtp_password")
            if original is None:
                db.delete(row)
            else:
                row.value, row.secret = original
            db.commit()


def test_metric_range_overflow_is_bad_request(admin_client, disabled_server):
    params = {"name": "pool.capacity_pct", "start": "0001-01-01T00:00:00+05:00",
              "end": "0001-01-02T00:00:00+05:00"}
    if params["name"] not in main.METRIC_NAMES:
        params["name"] = sorted(main.METRIC_NAMES)[0]
    response = admin_client.get(f"/api/servers/{disabled_server}/metrics", params=params)
    assert response.status_code == 400
    batch = admin_client.get(f"/api/servers/{disabled_server}/metrics/batch",
                             params={"names": params["name"], "scopes": "", "start": params["start"],
                                     "end": params["end"]})
    assert batch.status_code == 400


def test_caddyfile_rejects_placeholder_injection():
    with pytest.raises(TLSConfigurationError):
        validate_acme_directory_url("https://ca.example/{env.NASITRON_SECRET_KEY}")
    with pytest.raises(TLSConfigurationError):
        build_caddyfile("acme", domain="nas.example.com", email="a{$SECRET}@example.com",
                        acme_ca="https://ca.example/directory")
    assert "nas.example.com" in build_caddyfile(
        "acme", domain="nas.example.com", email="ops@example.com", acme_ca="https://ca.example/directory")


def test_invalid_timezone_path_is_a_config_error(monkeypatch):
    import app.config as config
    monkeypatch.setattr(config, "TIMEZONE", "/etc/localtime")
    with pytest.raises(RuntimeError, match="NASITRON_TIMEZONE"):
        config.validate_runtime_config()


def test_login_failure_table_is_pruned(monkeypatch):
    monkeypatch.setattr(security, "_LOGIN_TRACKED_CLIENTS", 10)
    monkeypatch.setattr(security, "_login_failures", {f"10.0.0.{i}": [0.0] for i in range(20)})
    monkeypatch.setattr(security, "_login_blocked_until", {f"10.1.0.{i}": 1.0 for i in range(20)})
    request = SimpleNamespace(client=SimpleNamespace(host="192.0.2.1"))
    security.record_login_failure(request, now=10_000.0)
    assert set(security._login_failures) == {"192.0.2.1"}
    assert security._login_blocked_until == {}


def test_body_limit_still_applies_inside_authenticated_routes(admin_client):
    big = b"csrf_token=" + b"a" * (2 * 1024 * 1024)
    declared = admin_client.post("/settings", content=big,
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert declared.status_code == 413

    def chunks():
        for _ in range(64):
            yield b"a" * 65536

    streamed = admin_client.post("/settings", content=chunks(),
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert streamed.status_code == 413

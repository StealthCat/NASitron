from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone

from fastapi import Form, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import (
    ALLOW_INSECURE_MAINTENANCE,
    SECRET_KEY,
    SESSION_TTL_SECONDS,
    WEB_PASSWORD,
    WEB_USERNAME,
)
from .models import WebUser

SESSION_COOKIE_NAME = "nasitron_session"
_LOGIN_WINDOW_SECONDS = 5 * 60
_LOGIN_FAILURE_LIMIT = 5
_LOGIN_BLOCK_SECONDS = 5 * 60
_PASSWORD_SCHEME = "scrypt-v1"
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 32
_login_lock = threading.Lock()
_login_failures: dict[str, list[float]] = {}
_login_blocked_until: dict[str, float] = {}


def _token_for(offset_hours: int = 0) -> str:
    if not SECRET_KEY:
        raise RuntimeError("NASITRON_SECRET_KEY is required for CSRF protection")
    stamp = (datetime.now(timezone.utc) + timedelta(hours=offset_hours)).strftime("%Y%m%d%H")
    return hmac.new(
        SECRET_KEY.encode("utf-8"),
        f"csrf:{stamp}".encode("utf-8"),
        "sha256",
    ).hexdigest()


def csrf_token() -> str:
    return _token_for(0)


def verify_csrf(token: str) -> bool:
    if not token:
        return False
    return any(hmac.compare_digest(token, _token_for(offset)) for offset in (0, -1))


def require_csrf(csrf_token: str = Form(...)) -> None:
    if not verify_csrf(csrf_token):
        raise HTTPException(status_code=403, detail="CSRF token expired or invalid.")


def _b64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_SCRYPT_DKLEN,
    )
    return "$".join(
        [
            _PASSWORD_SCHEME,
            str(_SCRYPT_N),
            str(_SCRYPT_R),
            str(_SCRYPT_P),
            _b64url_encode(salt),
            _b64url_encode(digest),
        ]
    )


def verify_password(password: str, encoded: str) -> bool:
    try:
        scheme, n_raw, r_raw, p_raw, salt_raw, digest_raw = encoded.split("$", 5)
        if scheme != _PASSWORD_SCHEME:
            return False
        n = int(n_raw)
        r = int(r_raw)
        p = int(p_raw)
        if n != _SCRYPT_N or r != _SCRYPT_R or p != _SCRYPT_P:
            return False
        salt = _b64url_decode(salt_raw)
        expected = _b64url_decode(digest_raw)
        if len(salt) != 16 or len(expected) != _SCRYPT_DKLEN:
            return False
        actual = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=n,
            r=r,
            p=p,
            dklen=len(expected),
        )
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def _dummy_password_check(password: str) -> None:
    hashlib.scrypt(
        password.encode("utf-8"),
        salt=b"nasitron-login-v1",
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_SCRYPT_DKLEN,
    )


def ensure_bootstrap_admin(db: Session) -> None:
    if db.scalar(select(WebUser.id).limit(1)) is not None:
        return
    user = WebUser(
        username=WEB_USERNAME,
        password_hash=hash_password(WEB_PASSWORD),
        is_admin=True,
        enabled=True,
        session_version=1,
    )
    db.add(user)
    db.commit()


def authenticate_web_credentials(
    db: Session,
    username: str,
    password: str,
) -> WebUser | None:
    user = db.scalar(select(WebUser).where(WebUser.username == username))
    if user is None:
        _dummy_password_check(password)
        return None
    if not verify_password(password, user.password_hash):
        return None
    if not user.enabled:
        return None
    return user


def create_session_token(user: WebUser, *, now: int | None = None) -> str:
    issued = int(time.time()) if now is None else int(now)
    payload = {
        "id": int(user.id),
        "u": user.username,
        "iat": issued,
        "exp": issued + SESSION_TTL_SECONDS,
        "v": int(user.session_version),
    }
    encoded = _b64url_encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    )
    signature = hmac.new(
        SECRET_KEY.encode("utf-8"),
        ("session:" + encoded).encode("ascii"),
        "sha256",
    ).digest()
    return encoded + "." + _b64url_encode(signature)


def verify_session_token(
    token: str | None,
    db: Session,
    *,
    now: int | None = None,
) -> WebUser | None:
    if not token or "." not in token or len(token) > 4096:
        return None
    encoded, supplied_signature = token.split(".", 1)
    expected_signature = hmac.new(
        SECRET_KEY.encode("utf-8"),
        ("session:" + encoded).encode("ascii", errors="ignore"),
        "sha256",
    ).digest()
    try:
        decoded_signature = _b64url_decode(supplied_signature)
    except Exception:
        return None
    if not hmac.compare_digest(expected_signature, decoded_signature):
        return None
    try:
        payload = json.loads(_b64url_decode(encoded))
    except Exception:
        return None

    current = int(time.time()) if now is None else int(now)
    try:
        user_id = int(payload["id"])
        issued = int(payload["iat"])
        expires = int(payload["exp"])
        session_version = int(payload["v"])
    except (KeyError, TypeError, ValueError):
        return None

    if issued > current + 60 or expires <= current or expires - issued != SESSION_TTL_SECONDS:
        return None

    user = db.get(WebUser, user_id)
    if user is None or not user.enabled:
        return None
    if not hmac.compare_digest(str(payload.get("u", "")), user.username):
        return None
    if session_version != int(user.session_version):
        return None
    return user


def request_user(request: Request, db: Session) -> WebUser | None:
    return verify_session_token(request.cookies.get(SESSION_COOKIE_NAME), db)


def request_is_authenticated(request: Request, db: Session) -> bool:
    return request_user(request, db) is not None


def safe_next_url(value: str | None) -> str:
    if not value:
        return "/"
    if not value.startswith("/") or value.startswith("//") or "\x00" in value:
        return "/"
    return value[:2048]


def _client_key(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def login_is_rate_limited(request: Request, *, now: float | None = None) -> bool:
    current = time.monotonic() if now is None else now
    key = _client_key(request)
    with _login_lock:
        blocked_until = _login_blocked_until.get(key, 0.0)
        if blocked_until > current:
            return True
        if blocked_until:
            _login_blocked_until.pop(key, None)
        failures = [
            stamp
            for stamp in _login_failures.get(key, [])
            if current - stamp <= _LOGIN_WINDOW_SECONDS
        ]
        if failures:
            _login_failures[key] = failures
        else:
            _login_failures.pop(key, None)
        return False


def record_login_failure(request: Request, *, now: float | None = None) -> None:
    current = time.monotonic() if now is None else now
    key = _client_key(request)
    with _login_lock:
        failures = [
            stamp
            for stamp in _login_failures.get(key, [])
            if current - stamp <= _LOGIN_WINDOW_SECONDS
        ]
        failures.append(current)
        _login_failures[key] = failures
        if len(failures) >= _LOGIN_FAILURE_LIMIT:
            _login_blocked_until[key] = current + _LOGIN_BLOCK_SECONDS
            _login_failures.pop(key, None)


def clear_login_failures(request: Request) -> None:
    key = _client_key(request)
    with _login_lock:
        _login_failures.pop(key, None)
        _login_blocked_until.pop(key, None)


def require_secure_maintenance(request: Request) -> None:
    if ALLOW_INSECURE_MAINTENANCE:
        return
    if request.url.scheme.lower() != "https":
        raise HTTPException(
            status_code=403,
            detail=(
                "Remote maintenance requires HTTPS. Terminate TLS at Caddy or "
                "set NASITRON_ALLOW_INSECURE_MAINTENANCE=true only on a trusted network."
            ),
        )

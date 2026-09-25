from __future__ import annotations

import base64
import hmac
import json
import threading
import time
from datetime import datetime, timedelta, timezone

from fastapi import Form, HTTPException, Request

from .config import (
    ALLOW_INSECURE_MAINTENANCE,
    SECRET_KEY,
    SESSION_TTL_SECONDS,
    WEB_PASSWORD,
    WEB_USERNAME,
)

SESSION_COOKIE_NAME = "nasitron_session"
_LOGIN_WINDOW_SECONDS = 5 * 60
_LOGIN_FAILURE_LIMIT = 5
_LOGIN_BLOCK_SECONDS = 5 * 60
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


def _credential_version() -> str:
    return hmac.new(
        SECRET_KEY.encode("utf-8"),
        ("credentials\0" + WEB_USERNAME + "\0" + WEB_PASSWORD).encode("utf-8"),
        "sha256",
    ).hexdigest()[:32]


def authenticate_web_credentials(username: str, password: str) -> bool:
    user_ok = hmac.compare_digest(username, WEB_USERNAME)
    password_ok = hmac.compare_digest(password, WEB_PASSWORD)
    return user_ok and password_ok


def create_session_token(*, now: int | None = None) -> str:
    issued = int(time.time()) if now is None else int(now)
    payload = {
        "u": WEB_USERNAME,
        "iat": issued,
        "exp": issued + SESSION_TTL_SECONDS,
        "v": _credential_version(),
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


def verify_session_token(token: str | None, *, now: int | None = None) -> bool:
    if not token or "." not in token or len(token) > 4096:
        return False
    encoded, supplied_signature = token.split(".", 1)
    expected_signature = hmac.new(
        SECRET_KEY.encode("utf-8"),
        ("session:" + encoded).encode("ascii", errors="ignore"),
        "sha256",
    ).digest()
    try:
        decoded_signature = _b64url_decode(supplied_signature)
    except Exception:
        return False
    if not hmac.compare_digest(expected_signature, decoded_signature):
        return False
    try:
        payload = json.loads(_b64url_decode(encoded))
    except Exception:
        return False
    current = int(time.time()) if now is None else int(now)
    try:
        issued = int(payload["iat"])
        expires = int(payload["exp"])
    except (KeyError, TypeError, ValueError):
        return False
    if issued > current + 60 or expires <= current or expires - issued != SESSION_TTL_SECONDS:
        return False
    if not hmac.compare_digest(str(payload.get("u", "")), WEB_USERNAME):
        return False
    return hmac.compare_digest(str(payload.get("v", "")), _credential_version())


def request_is_authenticated(request: Request) -> bool:
    return verify_session_token(request.cookies.get(SESSION_COOKIE_NAME))


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

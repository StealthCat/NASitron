from __future__ import annotations

import hmac
from datetime import datetime, timedelta, timezone

from fastapi import Form, HTTPException, Request

from .config import ALLOW_INSECURE_MAINTENANCE, SECRET_KEY


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


def require_secure_maintenance(request: Request) -> None:
    if ALLOW_INSECURE_MAINTENANCE:
        return
    proto = request.headers.get("x-forwarded-proto", request.url.scheme).split(",", 1)[0].strip().lower()
    if proto != "https":
        raise HTTPException(
            status_code=403,
            detail=(
                "Remote maintenance requires HTTPS. Terminate TLS at a trusted reverse proxy "
                "or set NASITRON_ALLOW_INSECURE_MAINTENANCE=true only on a trusted network."
            ),
        )

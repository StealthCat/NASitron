import pytest
from fastapi import HTTPException

from app.db import SessionLocal, init_db
from app.models import WebUser
from app.security import (
    create_session_token,
    csrf_token,
    hash_password,
    safe_next_url,
    verify_csrf,
    verify_session_token,
)
from app.validation import validate_threshold_pair


def test_csrf_token_verifies_and_rejects_tampering():
    token = csrf_token()
    assert verify_csrf(token)
    assert not verify_csrf(token[:-1] + ("0" if token[-1] != "0" else "1"))


def test_signed_session_verifies_and_rejects_tampering_and_expiry():
    init_db()
    with SessionLocal() as db:
        user = WebUser(
            username="session-token-test",
            password_hash=hash_password("session-test-password"),
            is_admin=False,
            enabled=True,
            session_version=1,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        user_id = user.id

        token = create_session_token(user, now=1_000_000)
        assert verify_session_token(token, db, now=1_000_001) is not None
        assert verify_session_token(token + "x", db, now=1_000_001) is None
        assert verify_session_token(token, db, now=2_000_000) is None

        user.session_version += 1
        db.commit()
        assert verify_session_token(token, db, now=1_000_001) is None

        db.delete(user)
        db.commit()

    with SessionLocal() as db:
        assert db.get(WebUser, user_id) is None


def test_safe_next_url_blocks_external_redirects():
    assert safe_next_url("/servers/1?tab=health") == "/servers/1?tab=health"
    assert safe_next_url("https://evil.example/") == "/"
    assert safe_next_url("//evil.example/") == "/"


def test_threshold_pair_requires_warning_below_critical():
    assert validate_threshold_pair(80, 90, "Pool capacity", 1, 100) == (80, 90)
    with pytest.raises(HTTPException):
        validate_threshold_pair(90, 80, "Pool capacity", 1, 100)

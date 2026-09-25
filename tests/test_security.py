import pytest
from fastapi import HTTPException

from app.security import csrf_token, verify_csrf
from app.validation import validate_threshold_pair


def test_csrf_token_verifies_and_rejects_tampering():
    token = csrf_token()
    assert verify_csrf(token)
    assert not verify_csrf(token[:-1] + ("0" if token[-1] != "0" else "1"))


def test_threshold_pair_requires_warning_below_critical():
    assert validate_threshold_pair(80, 90, "Pool capacity", 1, 100) == (80, 90)
    with pytest.raises(HTTPException):
        validate_threshold_pair(90, 80, "Pool capacity", 1, 100)

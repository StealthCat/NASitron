from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

from .config import SECRET_KEY


class SecretError(RuntimeError):
    pass


def _fernet() -> Fernet:
    if not SECRET_KEY:
        raise SecretError("NASITRON_SECRET_KEY is required before secrets can be stored or used")
    digest = hashlib.sha256(SECRET_KEY.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt(value: str | None) -> str | None:
    if value is None or value == "":
        return None
    return _fernet().encrypt(value.encode("utf-8")).decode("ascii")


def decrypt(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return _fernet().decrypt(value.encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        raise SecretError(
            "Unable to decrypt a stored secret. NASITRON_SECRET_KEY may have changed."
        ) from exc

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import DEFAULT_SETTINGS, SECRET_SETTING_KEYS
from .crypto import decrypt, encrypt
from .models import Setting


def ensure_defaults(db: Session) -> None:
    changed = False
    for key, value in DEFAULT_SETTINGS.items():
        if db.get(Setting, key) is None:
            db.add(Setting(key=key, value=value, secret=key in SECRET_SETTING_KEYS))
            changed = True
    if changed:
        db.commit()


def get_setting(db: Session, key: str, default: str | None = None, reveal_secret: bool = True) -> str:
    row = db.get(Setting, key)
    if row is None:
        return DEFAULT_SETTINGS.get(key, default or "")
    if row.secret and reveal_secret:
        return decrypt(row.value) or ""
    return row.value or ""


def get_bool(db: Session, key: str, default: bool = False) -> bool:
    value = get_setting(db, key, str(default).lower())
    return value.strip().lower() in {"1", "true", "yes", "on"}


def get_int(db: Session, key: str, default: int) -> int:
    try:
        return int(get_setting(db, key, str(default)))
    except (TypeError, ValueError):
        return default


def set_setting(db: Session, key: str, value: str, secret: bool | None = None) -> None:
    is_secret = key in SECRET_SETTING_KEYS if secret is None else secret
    row = db.get(Setting, key)
    if row is None:
        row = Setting(key=key, value="", secret=is_secret)
        db.add(row)
    row.secret = is_secret
    if is_secret:
        if value:
            row.value = encrypt(value) or ""
    else:
        row.value = value


def get_many(db: Session, keys: list[str]) -> dict[str, str]:
    rows = db.scalars(select(Setting).where(Setting.key.in_(keys))).all()
    by_key = {row.key: row for row in rows}
    values: dict[str, str] = {}
    for key in keys:
        row = by_key.get(key)
        if row is None:
            values[key] = DEFAULT_SETTINGS.get(key, "")
        elif row.secret:
            values[key] = decrypt(row.value) or ""
        else:
            values[key] = row.value or ""
    return values

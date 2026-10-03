from __future__ import annotations

import os
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

APP_NAME = "NASitron"
APP_VERSION = "0.10.1"

_CONFIG_ERRORS: list[str] = []


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw in (None, ""):
        return default
    try:
        return int(raw)
    except ValueError:
        _CONFIG_ERRORS.append(f"{name} must be an integer")
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    _CONFIG_ERRORS.append(f"{name} must be true/false")
    return default


DATA_DIR = Path(os.getenv("NASITRON_DATA_DIR", "/data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DATABASE_URL = os.getenv("NASITRON_DATABASE_URL", f"sqlite:///{DATA_DIR / 'nasitron.db'}")
SECRET_KEY = os.getenv("NASITRON_SECRET_KEY", "")
TIMEZONE = os.getenv("NASITRON_TIMEZONE", "UTC")
WEB_USERNAME = os.getenv("NASITRON_WEB_USERNAME", "")
WEB_PASSWORD = os.getenv("NASITRON_WEB_PASSWORD", "")
TLS_HOST = os.getenv("NASITRON_TLS_HOST", "localhost").strip()
CADDY_ADMIN_URL = os.getenv(
    "NASITRON_CADDY_ADMIN_URL", "http://caddy:2019/load"
).strip()
CADDY_SHARED_TLS_DIR = os.getenv(
    "NASITRON_CADDY_SHARED_TLS_DIR", "/nasitron-data/tls"
).rstrip("/")
KNOWN_HOSTS_PATH = Path(os.getenv("NASITRON_KNOWN_HOSTS", str(DATA_DIR / "known_hosts")))
REMOTE_HELPER_PATH = os.getenv(
    "NASITRON_REMOTE_HELPER_PATH", "/usr/local/sbin/nasitron-root-helper"
)

ALLOW_INSECURE_HTTP = _env_bool("NASITRON_ALLOW_INSECURE_HTTP", False)
ALLOW_INSECURE_MAINTENANCE = _env_bool(
    "NASITRON_ALLOW_INSECURE_MAINTENANCE", False
)
MAX_REMOTE_OUTPUT_BYTES = _env_int(
    "NASITRON_MAX_REMOTE_OUTPUT_BYTES", 8 * 1024 * 1024
)
MAX_DIAGNOSTIC_OUTPUT_BYTES = _env_int(
    "NASITRON_MAX_DIAGNOSTIC_OUTPUT_BYTES", 4 * 1024 * 1024
)
MAX_METRIC_POINTS = _env_int("NASITRON_MAX_METRIC_POINTS", 1200)
COLLECTOR_WORKERS = _env_int("NASITRON_COLLECTOR_WORKERS", 2)
MAX_REQUEST_BODY_BYTES = _env_int("NASITRON_MAX_REQUEST_BODY_BYTES", 256 * 1024)
SESSION_TTL_SECONDS = _env_int("NASITRON_SESSION_TTL_SECONDS", 12 * 60 * 60)
WEB_CONCURRENCY = _env_int("WEB_CONCURRENCY", 1)

DEFAULT_SETTINGS = {
    "smtp_enabled": "false",
    "email_transport": "smtp",
    "mailjet_api_url": "https://api.mailjet.com/v3.1/send",
    "mailjet_api_key": "",
    "mailjet_secret_key": "",
    "smtp_host": "",
    "smtp_port": "587",
    "smtp_username": "",
    "smtp_password": "",
    "smtp_from": "",
    "smtp_to": "",
    "smtp_starttls": "true",
    "smtp_ssl": "false",
    "pool_capacity_warning": "80",
    "pool_capacity_critical": "90",
    "temperature_unit": "C",
    "drive_temp_warning_c": "45",
    "drive_temp_critical_c": "55",
    "nvme_percentage_used_warning": "80",
    "nvme_percentage_used_critical": "95",
    "scrub_age_warning_days": "35",
    "collection_failure_threshold": "2",
    "metric_retention_days": "90",
    "snapshot_retention_days": "30",
    "full_snapshot_interval_minutes": "15",
    "tls_mode": "internal",
    "tls_domain": TLS_HOST,
    "tls_acme_email": "",
    "tls_acme_ca": "https://acme-v02.api.letsencrypt.org/directory",
}

SECRET_SETTING_KEYS = {"smtp_password", "mailjet_api_key", "mailjet_secret_key"}


def validate_runtime_config() -> None:
    errors = list(_CONFIG_ERRORS)
    if not DATABASE_URL.startswith("sqlite"):
        errors.append("NASITRON_DATABASE_URL currently supports SQLite only")
    if len(SECRET_KEY) < 32:
        errors.append("NASITRON_SECRET_KEY must contain at least 32 characters")
    if not WEB_USERNAME:
        errors.append("NASITRON_WEB_USERNAME is required")
    if len(WEB_USERNAME) > 120:
        errors.append("NASITRON_WEB_USERNAME must be at most 120 characters")
    if len(WEB_PASSWORD) < 12:
        errors.append("NASITRON_WEB_PASSWORD must contain at least 12 characters")
    if MAX_REMOTE_OUTPUT_BYTES < 1024 * 1024:
        errors.append("NASITRON_MAX_REMOTE_OUTPUT_BYTES must be at least 1 MiB")
    if MAX_DIAGNOSTIC_OUTPUT_BYTES < 1024 * 1024:
        errors.append("NASITRON_MAX_DIAGNOSTIC_OUTPUT_BYTES must be at least 1 MiB")
    if not 100 <= MAX_METRIC_POINTS <= 10000:
        errors.append("NASITRON_MAX_METRIC_POINTS must be between 100 and 10000")
    if not 1 <= COLLECTOR_WORKERS <= 16:
        errors.append("NASITRON_COLLECTOR_WORKERS must be between 1 and 16")
    if not 16 * 1024 <= MAX_REQUEST_BODY_BYTES <= 8 * 1024 * 1024:
        errors.append("NASITRON_MAX_REQUEST_BODY_BYTES must be between 16 KiB and 8 MiB")
    if not 300 <= SESSION_TTL_SECONDS <= 7 * 24 * 60 * 60:
        errors.append(
            "NASITRON_SESSION_TTL_SECONDS must be between 300 and 604800 seconds"
        )
    if WEB_CONCURRENCY != 1:
        errors.append("NASitron requires WEB_CONCURRENCY=1; its scheduler is single-instance")
    if not REMOTE_HELPER_PATH.startswith("/") or len(REMOTE_HELPER_PATH) > 512:
        errors.append("NASITRON_REMOTE_HELPER_PATH must be a short absolute path")
    if (
        not TLS_HOST
        or len(TLS_HOST) > 253
        or "://" in TLS_HOST
        or "/" in TLS_HOST
        or any(ord(ch) < 33 for ch in TLS_HOST)
    ):
        errors.append("NASITRON_TLS_HOST must be a hostname or IP address without a scheme")
    if not CADDY_ADMIN_URL.startswith(("http://", "https://")):
        errors.append("NASITRON_CADDY_ADMIN_URL must be an HTTP(S) URL")
    if not CADDY_SHARED_TLS_DIR.startswith("/") or len(CADDY_SHARED_TLS_DIR) > 512:
        errors.append("NASITRON_CADDY_SHARED_TLS_DIR must be a short absolute path")
    try:
        ZoneInfo(TIMEZONE)
    except ZoneInfoNotFoundError:
        errors.append(f"NASITRON_TIMEZONE is not a valid IANA timezone: {TIMEZONE}")
    if errors:
        raise RuntimeError("Invalid NASitron configuration: " + "; ".join(errors))

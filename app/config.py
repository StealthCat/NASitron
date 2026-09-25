from __future__ import annotations

import os
from pathlib import Path

APP_NAME = "NASitron"
APP_VERSION = "0.4.0"

DATA_DIR = Path(os.getenv("NASITRON_DATA_DIR", "/data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DATABASE_URL = os.getenv("NASITRON_DATABASE_URL", f"sqlite:///{DATA_DIR / 'nasitron.db'}")
SECRET_KEY = os.getenv("NASITRON_SECRET_KEY", "")
TIMEZONE = os.getenv("NASITRON_TIMEZONE", "UTC")
WEB_USERNAME = os.getenv("NASITRON_WEB_USERNAME", "")
WEB_PASSWORD = os.getenv("NASITRON_WEB_PASSWORD", "")
KNOWN_HOSTS_PATH = Path(os.getenv("NASITRON_KNOWN_HOSTS", str(DATA_DIR / "known_hosts")))
ALLOW_INSECURE_MAINTENANCE = os.getenv(
    "NASITRON_ALLOW_INSECURE_MAINTENANCE", "false"
).strip().lower() in {"1", "true", "yes", "on"}

MAX_REMOTE_OUTPUT_BYTES = int(os.getenv("NASITRON_MAX_REMOTE_OUTPUT_BYTES", str(8 * 1024 * 1024)))
MAX_DIAGNOSTIC_OUTPUT_BYTES = int(
    os.getenv("NASITRON_MAX_DIAGNOSTIC_OUTPUT_BYTES", str(4 * 1024 * 1024))
)
MAX_METRIC_POINTS = int(os.getenv("NASITRON_MAX_METRIC_POINTS", "1200"))
COLLECTOR_WORKERS = int(os.getenv("NASITRON_COLLECTOR_WORKERS", "2"))

DEFAULT_SETTINGS = {
    "smtp_enabled": "false",
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
    "drive_temp_warning_c": "45",
    "drive_temp_critical_c": "55",
    "scrub_age_warning_days": "35",
    "collection_failure_threshold": "2",
    "metric_retention_days": "90",
    "snapshot_retention_days": "30",
    "full_snapshot_interval_minutes": "15",
}

SECRET_SETTING_KEYS = {"smtp_password"}


def validate_runtime_config() -> None:
    errors: list[str] = []
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
    if errors:
        raise RuntimeError("Invalid NASitron configuration: " + "; ".join(errors))

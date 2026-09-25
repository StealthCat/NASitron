from __future__ import annotations

import os
from pathlib import Path

APP_NAME = "NASitron"
APP_VERSION = "0.1.0"

DATA_DIR = Path(os.getenv("NASITRON_DATA_DIR", "/data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DATABASE_URL = os.getenv("NASITRON_DATABASE_URL", f"sqlite:///{DATA_DIR / 'nasitron.db'}")
SECRET_KEY = os.getenv("NASITRON_SECRET_KEY", "")
TIMEZONE = os.getenv("NASITRON_TIMEZONE", "UTC")
WEB_USERNAME = os.getenv("NASITRON_WEB_USERNAME", "")
WEB_PASSWORD = os.getenv("NASITRON_WEB_PASSWORD", "")
KNOWN_HOSTS_PATH = Path(os.getenv("NASITRON_KNOWN_HOSTS", str(DATA_DIR / "known_hosts")))

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
    "snapshot_retention_days": "7",
}

SECRET_SETTING_KEYS = {"smtp_password"}

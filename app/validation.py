from __future__ import annotations

import ipaddress
import re
from email.utils import parseaddr

from fastapi import HTTPException

_HOST_LABEL = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")


def bad_request(message: str) -> None:
    raise HTTPException(status_code=400, detail=message)


def _has_control(value: str) -> bool:
    return any(ord(ch) < 32 or ord(ch) == 127 for ch in value)


def bounded_text(
    value: str,
    field: str,
    *,
    minimum: int = 1,
    maximum: int = 255,
    strip: bool = True,
) -> str:
    result = value.strip() if strip else value
    if len(result) < minimum:
        bad_request(f"{field} is required.")
    if len(result) > maximum:
        bad_request(f"{field} must be at most {maximum} characters.")
    if _has_control(result):
        bad_request(f"{field} contains invalid control characters.")
    return result


def bounded_multiline(
    value: str,
    field: str,
    *,
    maximum: int = 65536,
) -> str:
    if len(value) > maximum:
        bad_request(f"{field} is too large.")
    if any((ord(ch) < 32 and ch not in {"\t", "\r", "\n"}) or ord(ch) == 127 for ch in value):
        bad_request(f"{field} contains invalid control characters.")
    return value


def bounded_secret(value: str, field: str, maximum: int = 65536) -> str:
    if len(value) > maximum:
        bad_request(f"{field} is too large.")
    if "\x00" in value:
        bad_request(f"{field} contains an invalid NUL character.")
    return value


def bounded_int(value: int, field: str, minimum: int, maximum: int) -> int:
    if value < minimum or value > maximum:
        bad_request(f"{field} must be between {minimum} and {maximum}.")
    return value


def validate_host(value: str, field: str = "Host") -> str:
    host = bounded_text(value, field, maximum=253)
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    try:
        ipaddress.ip_address(host)
        return host
    except ValueError:
        pass

    if "://" in host or "/" in host:
        bad_request(f"{field} must be a hostname or IP address, not a URL.")
    if host.endswith("."):
        host = host[:-1]
    labels = host.split(".")
    if not labels or any(not _HOST_LABEL.fullmatch(label) for label in labels):
        bad_request(f"{field} is not a valid hostname or IP address.")
    return host


def validate_email(value: str, field: str) -> str:
    address = bounded_text(value, field, maximum=320)
    parsed = parseaddr(address)[1]
    if parsed != address or "@" not in address or address.startswith("@") or address.endswith("@"):
        bad_request(f"{field} is not a valid email address.")
    return address


def validate_recipient_list(value: str) -> str:
    recipients = [item.strip() for item in value.replace(";", ",").split(",") if item.strip()]
    if len(recipients) > 50:
        bad_request("SMTP recipients are limited to 50 addresses.")
    return ", ".join(validate_email(item, "SMTP recipient") for item in recipients)


def validate_threshold_pair(
    warning: int,
    critical: int,
    field: str,
    minimum: int,
    maximum: int,
) -> tuple[int, int]:
    warning = bounded_int(warning, f"{field} warning", minimum, maximum)
    critical = bounded_int(critical, f"{field} critical", minimum, maximum)
    if warning >= critical:
        bad_request(f"{field} warning threshold must be lower than the critical threshold.")
    return warning, critical

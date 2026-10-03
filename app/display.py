"""Display units; telemetry and alert thresholds remain in Celsius."""
import math
import re

from jinja2 import pass_context


def temperature_value(value, unit="C"):
    number = float(value)
    return number * 9 / 5 + 32 if unit == "F" else number


def format_temperature(value, unit="C"):
    try:
        number = temperature_value(value, unit)
        if not math.isfinite(number):
            return "—"
        return f"{number:.1f}".rstrip("0").rstrip(".") + f"°{unit}"
    except (TypeError, ValueError):
        return "—"


@pass_context
def temperature(context, value):
    return format_temperature(value, context.get("temperature_unit", "C"))


def temperature_message(message, unit="C"):
    # Alerts are stored in Celsius, so old and new alerts follow the preference.
    return re.sub(r"(-?\d+(?:\.\d+)?) (?:°?C)\b",
                  lambda m: format_temperature(m.group(1), unit), message)


@pass_context
def alert_temperature(context, message):
    return temperature_message(message, context.get("temperature_unit", "C"))


def power_on_duration(value):
    try:
        hours = float(value)
        if not math.isfinite(hours) or hours < 0:
            return "—"
    except (TypeError, ValueError):
        return "—"
    for divisor, unit in [(8760, "year"), (720, "month"), (24, "day"), (1, "hour")]:
        if hours >= divisor or divisor == 1:
            amount = round(hours / divisor, 1)
            return f"{amount:g} {unit}{'' if amount == 1 else 's'}"

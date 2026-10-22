"""Input validation shared by routes and series operations."""
from __future__ import annotations
import datetime as dt

from timezone import InvalidTimezone, localize_and_convert, validate_timezone


MIN_SUPPORTED = dt.datetime(1900, 1, 1)
MAX_SUPPORTED = dt.datetime(2200, 1, 1)


class InvalidInput(ValueError):
    """Client-supplied data is invalid. Routes map this to HTTP 422."""


def require_timezone(tz_name: str) -> str:
    try:
        return validate_timezone(tz_name)
    except InvalidTimezone as exc:
        raise InvalidInput(str(exc)) from None


def to_utc(value: str, tz_name: str, field: str) -> dt.datetime:
    """Parse an ISO datetime (naive = local to tz_name) into naive UTC."""
    try:
        result = localize_and_convert(value, tz_name)
    except (TypeError, ValueError, OverflowError):
        raise InvalidInput(f"{field} must be an ISO 8601 datetime") from None
    if not MIN_SUPPORTED <= result < MAX_SUPPORTED:
        raise InvalidInput(f"{field} must be between years 1900 and 2199")
    return result


def require_order(start_utc: dt.datetime, end_utc: dt.datetime) -> None:
    if end_utc <= start_utc:
        raise InvalidInput("end must be after start")

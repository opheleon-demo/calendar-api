"""
DST-aware timezone arithmetic on top of the stdlib zoneinfo database.

zoneinfo applies each zone's POSIX rule after the last explicit transition, so
recurrences far in the future keep the right offset. Gap (spring-forward) and
ambiguous (fall-back) local times are resolved explicitly:

- ambiguous: prefer_dst=True picks the earlier UTC instant (DST side)
- gap: snaps forward to the transition instant (first valid local time)
"""
from __future__ import annotations
import datetime as dt
from functools import lru_cache
from typing import NamedTuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_UTC = dt.timezone.utc


class ConversionResult(NamedTuple):
    utc_dt: dt.datetime
    is_ambiguous: bool
    is_gap: bool


class InvalidTimezone(ValueError):
    pass


@lru_cache(maxsize=256)
def get_zone(tz_name: str) -> ZoneInfo:
    """Return the ZoneInfo for tz_name or raise InvalidTimezone."""
    if not isinstance(tz_name, str) or not tz_name or tz_name != tz_name.strip():
        raise InvalidTimezone(f"Unknown time zone: {tz_name!r}")
    try:
        return ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        raise InvalidTimezone(f"Unknown time zone: {tz_name!r}") from None


def validate_timezone(tz_name: str) -> str:
    get_zone(tz_name)
    return tz_name


def utc_to_local(utc_dt: dt.datetime, tz_name: str) -> dt.datetime:
    """Convert a naive UTC datetime to naive local wall-clock time."""
    return utc_dt.replace(tzinfo=_UTC).astimezone(get_zone(tz_name)).replace(tzinfo=None)


def _to_utc(local_naive: dt.datetime, zone: ZoneInfo, fold: int) -> dt.datetime:
    return local_naive.replace(tzinfo=zone, fold=fold).astimezone(_UTC).replace(tzinfo=None)


def local_to_utc(local_naive: dt.datetime, tz_name: str, prefer_dst: bool = True) -> ConversionResult:
    """Convert a naive local datetime to naive UTC, resolving gaps and ambiguities."""
    zone = get_zone(tz_name)
    early = _to_utc(local_naive, zone, fold=0)
    late = _to_utc(local_naive, zone, fold=1)
    early_ok = utc_to_local(early, tz_name) == local_naive
    late_ok = utc_to_local(late, tz_name) == local_naive

    if early_ok and late_ok and early != late:
        pick = min(early, late) if prefer_dst else max(early, late)
        return ConversionResult(pick, True, False)
    if early_ok or late_ok:
        return ConversionResult(early if early_ok else late, False, False)

    # Gap: the transition lies between the two candidate instants. Binary
    # search for the first instant whose local time is past the gap.
    lo, hi = min(early, late), max(early, late)
    while hi - lo > dt.timedelta(seconds=1):
        mid = lo + (hi - lo) / 2
        if utc_to_local(mid, tz_name) >= local_naive:
            hi = mid
        else:
            lo = mid
    snapped = hi.replace(microsecond=0)
    if utc_to_local(snapped, tz_name) < local_naive:
        snapped += dt.timedelta(seconds=1)
    return ConversionResult(snapped, False, True)


def parse_datetime(value: str) -> dt.datetime:
    """Parse an ISO 8601 datetime; raises ValueError on bad input."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Datetime must be a non-empty ISO 8601 string")
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    parsed = dt.datetime.fromisoformat(text)
    return parsed


def localize_and_convert(iso_value: str, tz_name: str) -> dt.datetime:
    """
    Parse an ISO string and return a naive UTC datetime.

    Naive values are interpreted as wall-clock time in tz_name; values with an
    explicit offset are converted directly.
    """
    parsed = parse_datetime(iso_value)
    if parsed.tzinfo is not None:
        return parsed.astimezone(_UTC).replace(tzinfo=None)
    return local_to_utc(parsed, tz_name).utc_dt

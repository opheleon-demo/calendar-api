"""
DST-aware timezone arithmetic using binary search over pytz transition tables.

Instead of delegating to pytz.localize(), we extract the raw transition timestamps
and UTC offsets, then use bisect for O(log T) lookups. This gives us explicit
control over ambiguous (fall-back) and gap (spring-forward) times.
"""
from __future__ import annotations
import datetime as dt
from bisect import bisect_right
from typing import NamedTuple

import pytz


class ConversionResult(NamedTuple):
    utc_dt: dt.datetime
    is_ambiguous: bool
    is_gap: bool


def _get_transitions(tz_name: str):
    """Extract sorted UTC transition timestamps and their resulting offsets."""
    tz = pytz.timezone(tz_name)
    if not hasattr(tz, "_utc_transition_times"):
        # Fixed-offset zone (e.g. UTC) — no transitions
        offset = tz.utcoffset(dt.datetime(2020, 1, 1))
        return [], [], offset
    trans_utc = [t.replace(tzinfo=None) for t in tz._utc_transition_times]
    trans_timestamps = [int(t.replace(tzinfo=dt.timezone.utc).timestamp()) for t in trans_utc]
    offsets = []
    for info in tz._transition_info:
        utcoff, dst_off, abbr = info
        offsets.append(utcoff)
    default = tz._transition_info[0][0] if tz._transition_info else dt.timedelta(0)
    return trans_timestamps, offsets, default


_tz_cache: dict[str, tuple] = {}


def _cached_tz(tz_name: str):
    if tz_name not in _tz_cache:
        _tz_cache[tz_name] = _get_transitions(tz_name)
    return _tz_cache[tz_name]


def utc_to_local(utc_dt: dt.datetime, tz_name: str) -> dt.datetime:
    """Convert a UTC datetime to local wall-clock time. O(log T) via binary search."""
    trans_ts, offsets, default_off = _cached_tz(tz_name)
    ts = int(utc_dt.replace(tzinfo=dt.timezone.utc).timestamp())

    if not trans_ts:
        return utc_dt + default_off

    idx = bisect_right(trans_ts, ts) - 1
    if idx < 0:
        offset = default_off
    else:
        offset = offsets[idx]

    return utc_dt + offset


def local_to_utc(local_naive: dt.datetime, tz_name: str, prefer_dst: bool = True) -> ConversionResult:
    """
    Convert a naive local datetime to UTC. Handles gaps and ambiguities.

    For ambiguous times (fall-back): prefer_dst=True picks the DST offset (earlier UTC),
    prefer_dst=False picks the standard offset (later UTC).

    For gap times (spring-forward): shifts forward to the first valid time after the gap.

    Returns a ConversionResult with the resolved UTC datetime and flags.
    """
    trans_ts, offsets, default_off = _cached_tz(tz_name)

    if not trans_ts:
        return ConversionResult(local_naive - default_off, False, False)

    # The local time `lt` maps to UTC `lt - offset`. But the correct offset depends
    # on which period we're in (which depends on UTC). We try all candidate periods.
    local_ts = int(local_naive.replace(tzinfo=dt.timezone.utc).timestamp())

    candidates = []
    # Check default period (before first transition)
    utc_cand = local_ts - int(default_off.total_seconds())
    if utc_cand < trans_ts[0]:
        candidates.append((utc_cand, default_off))

    for i in range(len(trans_ts)):
        offset = offsets[i]
        utc_cand = local_ts - int(offset.total_seconds())
        period_start = trans_ts[i]
        period_end = trans_ts[i + 1] if i + 1 < len(trans_ts) else float("inf")
        if period_start <= utc_cand < period_end:
            candidates.append((utc_cand, offset))

    if len(candidates) == 1:
        utc_ts, _ = candidates[0]
        return ConversionResult(
            dt.datetime.fromtimestamp(utc_ts, tz=dt.timezone.utc).replace(tzinfo=None),
            False, False,
        )

    if len(candidates) == 2:
        # Ambiguous: two valid UTC interpretations (fall-back)
        candidates.sort()
        # Earlier UTC = DST side, later UTC = standard side
        pick = candidates[0] if prefer_dst else candidates[1]
        return ConversionResult(
            dt.datetime.fromtimestamp(pick[0], tz=dt.timezone.utc).replace(tzinfo=None),
            True, False,
        )

    # Zero candidates: gap (spring-forward)
    # Find the transition that caused the gap and snap to it
    for i in range(len(trans_ts)):
        off_before = offsets[i - 1] if i > 0 else default_off
        off_after = offsets[i]
        gap_local_start = trans_ts[i] + int(off_before.total_seconds())
        gap_local_end = trans_ts[i] + int(off_after.total_seconds())
        if gap_local_start <= local_ts < gap_local_end:
            # Snap forward to the end of the gap
            utc_snap = trans_ts[i]
            return ConversionResult(
                dt.datetime.fromtimestamp(utc_snap, tz=dt.timezone.utc).replace(tzinfo=None),
                False, True,
            )

    # Fallback: shouldn't reach here; use pytz as safety net
    tz = pytz.timezone(tz_name)
    localized = tz.localize(local_naive, is_dst=prefer_dst)
    return ConversionResult(localized.astimezone(pytz.utc).replace(tzinfo=None), False, False)


def localize_and_convert(naive_iso: str, tz_name: str) -> dt.datetime:
    """Parse an ISO string as local time in tz_name, return UTC naive datetime."""
    local_naive = dt.datetime.fromisoformat(naive_iso)
    result = local_to_utc(local_naive, tz_name)
    return result.utc_dt

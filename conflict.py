"""
Conflict reporting. Overlaps never block a save; they are reported.

Sweep line for batch conflict detection. O(N log N + k) vs naive O(N²).
Maintains two active sets (new vs existing) — only detects cross-set overlaps.
CLOSE sorts before OPEN at the same instant, so intervals that only touch at a
boundary never conflict.

Candidates are always limited to the saving user's own events.
"""
from __future__ import annotations
import datetime as dt
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from interval_tree import Interval
from models import ConflictCoverage, ConflictInfo, Event
from occurrences import Occurrence, expand_event, owned_occurrences, series_bounds

# Unbounded series are checked over a finite window. The window always
# reaches at least DEFAULT_HORIZON past the event's first occurrence and
# extends to cover the owner's latest finite event, up to MAX_HORIZON.
DEFAULT_HORIZON = dt.timedelta(days=366)
MAX_HORIZON = dt.timedelta(days=5 * 366)


@dataclass
class Conflict:
    new_interval: Interval; existing_interval: Interval


def sweep_line_conflicts(new_ivs: list[Interval], existing_ivs: list[Interval]) -> list[Conflict]:
    CLOSE, OPEN = 0, 1
    events: list[tuple[float, int, str, Interval]] = []
    for iv in new_ivs: iv.source = "new"; events += [(iv.low, OPEN, "new", iv), (iv.high, CLOSE, "new", iv)]
    for iv in existing_ivs: iv.source = "ex"; events += [(iv.low, OPEN, "ex", iv), (iv.high, CLOSE, "ex", iv)]
    events.sort(key=lambda e: (e[0], e[1]))

    active_new: list[Interval] = []; active_ex: list[Interval] = []; conflicts: list[Conflict] = []
    for _, etype, src, iv in events:
        if etype == OPEN:
            if src == "new":
                conflicts += [Conflict(iv, e) for e in active_ex]; active_new.append(iv)
            else:
                conflicts += [Conflict(n, iv) for n in active_new]; active_ex.append(iv)
        else:
            active = active_new if src == "new" else active_ex
            active.pop(next(i for i, a in enumerate(active) if a is iv))
    return conflicts


def _ts(value: dt.datetime) -> float:
    return value.replace(tzinfo=dt.timezone.utc).timestamp()


def _iso(value: dt.datetime) -> str:
    return value.isoformat() + "Z"


def _to_intervals(occs: list[Occurrence]) -> list[Interval]:
    ivs = []
    for o in occs:
        iv = Interval(low=_ts(o.start_utc), high=_ts(o.end_utc), event_id=o.event.id, title=o.title)
        iv.occurrence = o  # type: ignore[attr-defined]
        ivs.append(iv)
    return ivs


def detect_conflicts(
    db: Session,
    owner_id: int,
    subject: list[Occurrence],
    ws: dt.datetime,
    we: dt.datetime,
    exclude_event_id: Optional[int] = None,
    exclude_occurrence: Optional[tuple[int, dt.date]] = None,
) -> list[ConflictInfo]:
    """
    Report the owner's occurrences in [ws, we) that strictly overlap any
    subject occurrence. The saved event's previous version is excluded either
    as a whole (exclude_event_id) or as a single occurrence.
    """
    excluded_ids = [exclude_event_id] if exclude_event_id is not None else []
    existing = [
        o for o in owned_occurrences(db, owner_id, ws, we, exclude_ids=excluded_ids)
        if o.key != exclude_occurrence
    ]
    subject = [o for o in subject if o.start_utc < we and o.end_utc > ws]

    seen: set[tuple[int, Optional[dt.date]]] = set()
    results: list[ConflictInfo] = []
    for c in sweep_line_conflicts(_to_intervals(subject), _to_intervals(existing)):
        ex: Occurrence = c.existing_interval.occurrence  # type: ignore[attr-defined]
        new: Occurrence = c.new_interval.occurrence  # type: ignore[attr-defined]
        if ex.key in seen:
            continue
        seen.add(ex.key)
        results.append(ConflictInfo(
            event_id=ex.event.id, title=ex.title,
            start=_iso(ex.start_utc), end=_iso(ex.end_utc),
            is_recurring=ex.event.is_recurring,
            original_date=ex.original_date.isoformat() if ex.original_date else None,
            overlaps_start=_iso(new.start_utc), overlaps_end=_iso(new.end_utc),
            overlaps_original_date=new.original_date.isoformat() if new.original_date else None,
        ))
    results.sort(key=lambda r: (r.start, r.event_id))
    return results


def coverage_window(db: Session, owner_id: int, event: Event) -> tuple[dt.datetime, dt.datetime, bool]:
    """
    UTC window over which conflicts for `event` are checked, and whether that
    window is known to contain every possible conflict.
    """
    first, last = series_bounds(event)
    if first is None:
        first = event.start_utc
        last = event.end_utc
    if last is not None:
        ws, we, complete = first, last, True
    else:
        # Unbounded: cover every finite event of the owner; only other
        # unbounded series can still conflict after the window.
        latest_single = db.scalar(
            select(func.max(Event.end_utc))
            .where(Event.owner_id == owner_id, Event.is_recurring.is_(False), Event.id != event.id)
        )
        other_series = db.scalars(
            select(Event)
            .options(selectinload(Event.recurrence_rule), selectinload(Event.exceptions))
            .where(Event.owner_id == owner_id, Event.is_recurring.is_(True), Event.id != event.id)
        ).all()
        latest = first + DEFAULT_HORIZON
        if latest_single is not None:
            latest = max(latest, latest_single)
        complete = True
        for other in other_series:
            other_first, other_last = series_bounds(other)
            if other_first is None:
                continue
            if other_last is None:
                complete = False
            else:
                latest = max(latest, other_last)
        ws, we = first, latest

    if we - ws > MAX_HORIZON:
        we, complete = ws + MAX_HORIZON, False
    return ws, we, complete


def report_for_event(
    db: Session, owner_id: int, event: Event,
) -> tuple[list[ConflictInfo], ConflictCoverage]:
    """Conflicts for a whole (single or recurring) event, excluding itself."""
    ws, we, complete = coverage_window(db, owner_id, event)
    subject = expand_event(event, ws, we)
    conflicts = detect_conflicts(db, owner_id, subject, ws, we, exclude_event_id=event.id)
    return conflicts, ConflictCoverage(start=_iso(ws), end=_iso(we), complete=complete)


def report_for_occurrence(
    db: Session, owner_id: int, occ: Occurrence,
) -> tuple[list[ConflictInfo], ConflictCoverage]:
    """Conflicts for one occurrence, excluding only that occurrence's previous version."""
    conflicts = detect_conflicts(
        db, owner_id, [occ], occ.start_utc, occ.end_utc,
        exclude_occurrence=occ.key,
    )
    return conflicts, ConflictCoverage(start=_iso(occ.start_utc), end=_iso(occ.end_utc), complete=True)

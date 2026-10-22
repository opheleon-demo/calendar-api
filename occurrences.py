"""
Occurrence expansion for single and recurring events.

Recurring events are expanded in the event's local time zone (so the local
clock time survives DST changes) and converted to UTC. Exceptions are applied
by their *effective* interval: an occurrence moved into a window appears even
when its original date is outside it, and one moved out of the window does not.

Every database lookup here is scoped to a single owner.
"""
from __future__ import annotations
import datetime as dt
from dataclasses import dataclass
from typing import Iterable, Optional

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, selectinload

from models import Event, RecurrenceException
from rrule_engine import RRule, build_rrule, expand
from timezone import local_to_utc, utc_to_local

_DAY = dt.timedelta(days=1)


@dataclass
class Occurrence:
    event: Event
    start_utc: dt.datetime
    end_utc: dt.datetime
    title: str
    original_date: Optional[dt.date] = None
    is_exception: bool = False

    @property
    def key(self) -> tuple[int, Optional[dt.date]]:
        return (self.event.id, self.original_date)


def overlaps(s1: dt.datetime, e1: dt.datetime, s2: dt.datetime, e2: dt.datetime) -> bool:
    """Strict overlap: intervals that only touch at a boundary do not overlap."""
    return s1 < e2 and s2 < e1


def series_duration(event: Event) -> dt.timedelta:
    return event.end_utc - event.start_utc


def event_rrule(event: Event) -> RRule:
    rule = event.recurrence_rule
    tz_name = event.timezone
    return build_rrule(
        dtstart_local=utc_to_local(event.start_utc, tz_name),
        freq=rule.freq, interval=rule.interval,
        count=rule.count,
        until_local=utc_to_local(rule.until_utc, tz_name) if rule.until_utc else None,
        byday=rule.byday, bymonthday=rule.bymonthday, bymonth=rule.bymonth,
    )


_MAX_LOCAL = dt.datetime(9000, 1, 1)


def _max_gap(rrule: RRule) -> dt.timedelta:
    """
    Upper bound on the time between consecutive occurrences. Monthly series
    skip months without their day (e.g. the 31st, or Feb 29), so the months
    visited can repeat a full 12-step cycle of short months; Feb 29 only
    recurs every 4 (at worst 8) years.
    """
    per = {"DAILY": 1, "WEEKLY": 7, "MONTHLY": 31 * 12 * 8, "YEARLY": 366 * 8}[rrule.freq]
    return dt.timedelta(days=per * rrule.interval + 1)


def _clamped_add(value: dt.datetime, delta: dt.timedelta) -> dt.datetime:
    return value + delta if _MAX_LOCAL - value > delta else _MAX_LOCAL


def base_occurrence_local(event: Event, day: dt.date, rrule: Optional[RRule] = None) -> Optional[dt.datetime]:
    """Local start of the unmodified occurrence on `day`, or None if the series has none."""
    rrule = rrule or event_rrule(event)
    start = dt.datetime.combine(day, dt.time())
    found = expand(rrule, start, start + _DAY)
    return found[0] if found else None


def next_occurrence_local(event: Event, day: dt.date, rrule: Optional[RRule] = None) -> Optional[dt.datetime]:
    """First unmodified occurrence on or after `day` (local), or None."""
    rrule = rrule or event_rrule(event)
    ws = max(dt.datetime.combine(day, dt.time()), rrule.dtstart)
    found = expand(rrule, ws, _clamped_add(ws, _max_gap(rrule)))
    return found[0] if found else None


def previous_occurrence_local(event: Event, before: dt.datetime, rrule: Optional[RRule] = None) -> Optional[dt.datetime]:
    """Last unmodified occurrence strictly before `before` (local), or None."""
    rrule = rrule or event_rrule(event)
    if before <= rrule.dtstart:
        return None
    ws = max(rrule.dtstart, before - _max_gap(rrule))
    found = expand(rrule, ws, before)
    return found[-1] if found else None


def _exception_interval(event: Event, exc: RecurrenceException, base_local: dt.datetime) -> tuple[dt.datetime, dt.datetime]:
    base_start = local_to_utc(base_local, event.timezone).utc_dt
    start = exc.start_utc or base_start
    end = exc.end_utc or (start + series_duration(event))
    return start, end


def expand_event(event: Event, ws: dt.datetime, we: dt.datetime) -> list[Occurrence]:
    """All occurrences of `event` whose UTC interval overlaps [ws, we)."""
    if not event.is_recurring:
        if overlaps(event.start_utc, event.end_utc, ws, we):
            return [Occurrence(event, event.start_utc, event.end_utc, event.title)]
        return []

    tz_name = event.timezone
    duration = series_duration(event)
    rrule = event_rrule(event)
    exc_map = {exc.original_date: exc for exc in event.exceptions}

    # Expand a slightly wider local window so occurrences that start before
    # ws but are still running, and DST offset shifts, are not missed.
    local_ws = utc_to_local(ws, tz_name) - duration - _DAY
    local_we = utc_to_local(we, tz_name) + _DAY

    results: list[Occurrence] = []
    for occ_local in expand(rrule, local_ws, local_we):
        day = occ_local.date()
        if day in exc_map:
            continue  # handled below by effective interval
        start = local_to_utc(occ_local, tz_name).utc_dt
        end = start + duration
        if overlaps(start, end, ws, we):
            results.append(Occurrence(event, start, end, event.title, day))

    for exc in event.exceptions:
        if exc.is_deleted:
            continue
        base_local = base_occurrence_local(event, exc.original_date, rrule)
        if base_local is None:
            continue  # stale exception: no longer an occurrence of the series
        start, end = _exception_interval(event, exc, base_local)
        if overlaps(start, end, ws, we):
            results.append(Occurrence(
                event, start, end, exc.title or event.title, exc.original_date, True,
            ))

    results.sort(key=lambda o: (o.start_utc, o.event.id))
    return results


def get_occurrence(event: Event, day: dt.date) -> Optional[Occurrence]:
    """The (possibly modified) occurrence originally on `day`, or None if absent/deleted."""
    base_local = base_occurrence_local(event, day)
    if base_local is None:
        return None
    exc = next((e for e in event.exceptions if e.original_date == day), None)
    if exc is not None and exc.is_deleted:
        return None
    if exc is not None:
        start, end = _exception_interval(event, exc, base_local)
        return Occurrence(event, start, end, exc.title or event.title, day, True)
    start = local_to_utc(base_local, event.timezone).utc_dt
    return Occurrence(event, start, start + series_duration(event), event.title, day)


def series_bounds(event: Event) -> tuple[Optional[dt.datetime], Optional[dt.datetime]]:
    """
    Conservative (first start, last end) in UTC covering every occurrence.

    The last end is None for an unbounded series. Both are None when the
    series has no occurrences at all.
    """
    if not event.is_recurring:
        return event.start_utc, event.end_utc

    rrule = event_rrule(event)
    tz_name = event.timezone
    first_local = next_occurrence_local(event, rrule.dtstart.date(), rrule)
    if first_local is None:
        return None, None

    if rrule.until is not None:
        last_local = previous_occurrence_local(event, rrule.until + dt.timedelta(microseconds=1), rrule)
    elif rrule.count is not None:
        found = expand(rrule, rrule.dtstart, _clamped_add(rrule.dtstart, _max_gap(rrule) * rrule.count))
        last_local = found[-1] if found else None
    else:
        last_local = None

    starts = [local_to_utc(first_local, tz_name).utc_dt]
    ends = [local_to_utc(last_local, tz_name).utc_dt + series_duration(event)] if last_local else []
    for exc in event.exceptions:
        if exc.is_deleted:
            continue
        base_local = base_occurrence_local(event, exc.original_date, rrule)
        if base_local is None:
            continue
        s, e = _exception_interval(event, exc, base_local)
        starts.append(s)
        ends.append(e)

    if last_local is None:
        return min(starts), None
    return min(starts), max(ends)


def owned_candidate_events(
    db: Session, owner_id: int, ws: dt.datetime, we: dt.datetime,
    exclude_ids: Iterable[int] = (),
) -> list[Event]:
    """Owner's events that may have an occurrence overlapping [ws, we)."""
    moved_into_window = (
        select(RecurrenceException.event_id)
        .where(
            RecurrenceException.is_deleted.is_(False),
            RecurrenceException.start_utc < we,
            RecurrenceException.end_utc > ws,
        )
    )
    query = (
        select(Event)
        .options(selectinload(Event.recurrence_rule), selectinload(Event.exceptions))
        .where(Event.owner_id == owner_id)
        .where(or_(
            (Event.is_recurring.is_(False) & (Event.start_utc < we) & (Event.end_utc > ws)),
            (Event.is_recurring.is_(True) & (Event.start_utc < we)),
            (Event.is_recurring.is_(True) & Event.id.in_(moved_into_window)),
        ))
    )
    excluded = set(exclude_ids)
    return [ev for ev in db.scalars(query).all() if ev.id not in excluded]


def owned_occurrences(
    db: Session, owner_id: int, ws: dt.datetime, we: dt.datetime,
    exclude_ids: Iterable[int] = (),
) -> list[Occurrence]:
    results: list[Occurrence] = []
    for ev in owned_candidate_events(db, owner_id, ws, we, exclude_ids):
        results.extend(expand_event(ev, ws, we))
    results.sort(key=lambda o: (o.start_utc, o.event.id))
    return results

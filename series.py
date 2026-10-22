"""
Recurring series operations: rule handling, whole-series edits, and the series
split used for "edit this and future occurrences".

Whole-series edits apply to every occurrence. Exceptions are reconciled so no
stale values survive:
- exceptions whose original date is no longer an occurrence are dropped
- a changed title clears per-occurrence title overrides
- changed timing (start, end, time zone or rule) clears per-occurrence moves
- deleted occurrences stay deleted while their date remains in the series

Nothing here commits; callers commit once so a validation failure leaves no
partial write.
"""
from __future__ import annotations
import datetime as dt
from dataclasses import dataclass, replace
from typing import Optional

from sqlalchemy.orm import Session

from models import Event, RecurrenceException, RecurrenceRule, RecurrenceRuleSchema, EventUpdate
from occurrences import (
    base_occurrence_local, event_rrule, next_occurrence_local,
    previous_occurrence_local, series_bounds, series_duration,
)
from rrule_engine import count_occurrences_before
from timezone import local_to_utc, utc_to_local
from validation import InvalidInput, require_order, require_timezone, to_utc


@dataclass(frozen=True)
class RuleSpec:
    freq: str
    interval: int = 1
    until: Optional[dt.date] = None   # inclusive, local to the series time zone
    byday: Optional[str] = None
    bymonthday: Optional[str] = None
    count: Optional[int] = None       # legacy rows only; not accepted from clients


def spec_from_schema(schema: RecurrenceRuleSchema) -> RuleSpec:
    return RuleSpec(
        freq=schema.freq, interval=schema.interval, until=schema.until,
        byday=schema.byday, bymonthday=schema.bymonthday,
    )


def spec_from_rule(rule: RecurrenceRule, tz_name: str) -> RuleSpec:
    return RuleSpec(
        freq=rule.freq, interval=rule.interval,
        until=utc_to_local(rule.until_utc, tz_name).date() if rule.until_utc else None,
        byday=rule.byday, bymonthday=rule.bymonthday, count=rule.count,
    )


def apply_rule(event: Event, spec: RuleSpec) -> None:
    """Write spec onto the event's rule; UNTIL is stored as the end of the local day in UTC."""
    rule = event.recurrence_rule
    if rule is None:
        rule = RecurrenceRule()
        event.recurrence_rule = rule
    rule.freq = spec.freq
    rule.interval = spec.interval
    rule.count = spec.count
    rule.byday = spec.byday
    rule.bymonthday = spec.bymonthday
    rule.bymonth = None
    rule.until_utc = (
        local_to_utc(dt.datetime.combine(spec.until, dt.time.max), event.timezone).utc_dt
        if spec.until else None
    )


def validate_series(event: Event) -> None:
    require_order(event.start_utc, event.end_utc)
    rule = event.recurrence_rule
    if rule.until_utc is not None:
        local_start = utc_to_local(event.start_utc, event.timezone)
        if utc_to_local(rule.until_utc, event.timezone).date() < local_start.date():
            raise InvalidInput("recurrence until must be on or after the start date")
    first, _ = series_bounds(event)
    if first is None:
        raise InvalidInput("recurrence produces no occurrences")


def reconcile_exceptions(event: Event, title_changed: bool, timing_changed: bool) -> None:
    rrule = event_rrule(event)
    duration = series_duration(event)
    for exc in list(event.exceptions):
        base_local = base_occurrence_local(event, exc.original_date, rrule)
        if base_local is None:
            event.exceptions.remove(exc)
            continue
        if exc.is_deleted:
            exc.title = exc.start_utc = exc.end_utc = None
            continue
        if title_changed or exc.title == event.title:
            exc.title = None
        base_start = local_to_utc(base_local, event.timezone).utc_dt
        if timing_changed or (exc.start_utc == base_start and exc.end_utc == base_start + duration):
            exc.start_utc = exc.end_utc = None
        if exc.title is None and exc.start_utc is None and exc.end_utc is None:
            event.exceptions.remove(exc)


def edit_whole_series(event: Event, body: EventUpdate) -> Event:
    old_tz = event.timezone
    tz_name = require_timezone(body.timezone) if body.timezone is not None else old_tz
    old_spec = spec_from_rule(event.recurrence_rule, old_tz)
    spec = spec_from_schema(body.recurrence) if body.recurrence else old_spec
    start = to_utc(body.start, tz_name, "start") if body.start is not None else event.start_utc
    end = to_utc(body.end, tz_name, "end") if body.end is not None else event.end_utc

    title_changed = body.title is not None and body.title != event.title
    timing_changed = (
        start != event.start_utc or end != event.end_utc
        or tz_name != old_tz or spec != old_spec
    )

    if body.title is not None:
        event.title = body.title
    event.timezone = tz_name
    event.start_utc, event.end_utc = start, end
    apply_rule(event, spec)
    validate_series(event)
    reconcile_exceptions(event, title_changed, timing_changed)
    return event


def split_series(db: Session, event: Event, from_date: dt.date, body: EventUpdate) -> Event:
    """
    Edit occurrences on or after from_date (local) by ending the original series
    at the last earlier occurrence and starting a new series. Returns the event
    holding the edited occurrences (the original when nothing precedes from_date).
    """
    rrule = event_rrule(event)
    split_occ = next_occurrence_local(event, from_date, rrule)
    if split_occ is None:
        raise InvalidInput("No occurrences on or after from_date")
    prev = previous_occurrence_local(event, split_occ, rrule)
    if prev is None:
        return edit_whole_series(event, body)

    old_tz = event.timezone
    tz_name = require_timezone(body.timezone) if body.timezone is not None else old_tz
    duration = series_duration(event)
    split_start = local_to_utc(split_occ, old_tz).utc_dt
    start = to_utc(body.start, tz_name, "start") if body.start is not None else split_start
    end = to_utc(body.end, tz_name, "end") if body.end is not None else start + duration

    old_spec = spec_from_rule(event.recurrence_rule, old_tz)
    continued = old_spec
    if old_spec.count is not None:
        continued = replace(old_spec, count=old_spec.count - count_occurrences_before(rrule, split_occ))
    spec = spec_from_schema(body.recurrence) if body.recurrence else continued

    new_event = Event(
        owner_id=event.owner_id,
        title=body.title if body.title is not None else event.title,
        start_utc=start, end_utc=end, timezone=tz_name, is_recurring=True,
    )
    apply_rule(new_event, spec)
    validate_series(new_event)

    title_changed = new_event.title != event.title
    timing_changed = (
        start != split_start or end != split_start + duration
        or tz_name != old_tz or spec != continued
    )

    # Terminate the original at its last occurrence before the split.
    event.recurrence_rule.until_utc = local_to_utc(prev, old_tz).utc_dt
    event.recurrence_rule.count = None

    for exc in list(event.exceptions):
        if exc.original_date >= from_date:
            new_event.exceptions.append(RecurrenceException(
                original_date=exc.original_date, is_deleted=exc.is_deleted,
                title=exc.title, start_utc=exc.start_utc, end_utc=exc.end_utc,
            ))
            event.exceptions.remove(exc)

    reconcile_exceptions(event, title_changed=False, timing_changed=False)
    reconcile_exceptions(new_event, title_changed, timing_changed)
    db.add(new_event)
    return new_event

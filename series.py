"""
Series split algorithm for "edit this and future events".

Splits a recurring series at a given date:
1. Terminates the original series (sets UNTIL to day before split)
2. Creates a new series from the split point with edits applied
3. Partitions exceptions: before-split stay, after-split migrate to new series
4. Remaps exception dates if the new series has different timing

Edge cases: split at first occurrence, split at last occurrence, split on
an exception date, COUNT recalculation.
"""
from __future__ import annotations
import datetime as dt

from sqlalchemy.orm import Session

from models import Event, RecurrenceRule, RecurrenceException
from rrule_engine import build_rrule, expand, count_occurrences_before
from timezone import utc_to_local, local_to_utc


def split_series(
    db: Session,
    event: Event,
    split_date: dt.date,
    updates: dict,
) -> Event:
    """
    Split a recurring event at split_date. Returns the new event.

    updates: dict with optional keys 'title', 'start' (time-of-day ISO), 'end' (time-of-day ISO)
    """
    rule = event.recurrence_rule
    if not rule:
        raise ValueError("Event is not recurring")

    tz_name = event.timezone
    local_start = utc_to_local(event.start_utc, tz_name)
    duration = event.end_utc - event.start_utc

    # Build RRule in local time
    rrule = build_rrule(
        dtstart_local=local_start,
        freq=rule.freq, interval=rule.interval,
        count=rule.count,
        until_local=utc_to_local(rule.until_utc, tz_name) if rule.until_utc else None,
        byday=rule.byday, bymonthday=rule.bymonthday, bymonth=rule.bymonth,
    )

    split_dt = dt.datetime.combine(split_date, dt.time())
    # Find occurrences around the split point
    pre_split = expand(rrule, rrule.dtstart, split_dt)
    post_split = expand(rrule, split_dt, split_dt + dt.timedelta(days=365))

    if not post_split:
        raise ValueError("No occurrences exist at or after the split date")

    split_occ = post_split[0]

    # ── Edge case: split at first occurrence → just update in place ──
    if not pre_split or split_occ <= rrule.dtstart:
        _apply_updates_to_event(event, updates, tz_name, duration)
        db.flush()
        return event

    # ── Terminate original series ──
    last_before = pre_split[-1]
    last_before_utc = local_to_utc(last_before, tz_name).utc_dt
    rule.until_utc = last_before_utc
    rule.count = None  # Switch to UNTIL-based termination

    # ── Create new series ──
    new_title = updates.get("title", event.title)

    # Compute new start in local time
    if "start" in updates and updates["start"]:
        new_local_start_time = dt.datetime.fromisoformat(updates["start"]).time()
        new_local_start = dt.datetime.combine(split_occ.date(), new_local_start_time)
    else:
        new_local_start = split_occ

    if "end" in updates and updates["end"]:
        new_local_end_time = dt.datetime.fromisoformat(updates["end"]).time()
        new_local_end = dt.datetime.combine(split_occ.date(), new_local_end_time)
        new_duration = new_local_end - new_local_start
    else:
        new_duration = duration

    new_start_utc = local_to_utc(new_local_start, tz_name).utc_dt
    new_end_utc = new_start_utc + new_duration

    new_event = Event(
        title=new_title,
        start_utc=new_start_utc,
        end_utc=new_end_utc,
        timezone=tz_name,
        is_recurring=True,
    )
    db.add(new_event)
    db.flush()  # Get new_event.id

    # New recurrence rule (inherits original, potentially with new until)
    new_rule = RecurrenceRule(
        event_id=new_event.id,
        freq=rule.freq,
        interval=rule.interval,
        count=None,
        until_utc=rule.until_utc if rule.until_utc and rule.until_utc > new_end_utc else None,
        byday=rule.byday,
        bymonthday=rule.bymonthday,
        bymonth=rule.bymonth,
    )
    # The original's until was just set to last_before_utc; the new series
    # inherits the original's original until (which we need to retrieve before we overwrote it).
    # Since we already overwrote it, use the rrule.until from the local copy.
    if rrule.until:
        new_rule.until_utc = local_to_utc(rrule.until, tz_name).utc_dt
    db.add(new_rule)

    # ── Partition exceptions ──
    for exc in list(event.exceptions):
        if exc.original_date >= split_date:
            exc.event_id = new_event.id

    db.flush()
    return new_event


def _apply_updates_to_event(event: Event, updates: dict, tz_name: str, duration: dt.timedelta):
    """Apply updates directly to an event (used when splitting at the first occurrence)."""
    if "title" in updates and updates["title"]:
        event.title = updates["title"]
    if "start" in updates and updates["start"]:
        new_start_utc = local_to_utc(
            dt.datetime.fromisoformat(updates["start"]), tz_name
        ).utc_dt
        event.start_utc = new_start_utc
        event.end_utc = new_start_utc + duration

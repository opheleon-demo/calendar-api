"""
All 7 API endpoints for the calendar service.

Endpoints handle both single and recurring events. Recurring event expansion
is done on-the-fly using the custom RRULE engine. Conflict detection uses
the sweep line algorithm.
"""
from __future__ import annotations
import datetime as dt

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from database import get_db
from models import (
    Event, RecurrenceRule, RecurrenceException,
    EventCreate, EventUpdate, OccurrenceUpdate, SeriesUpdate,
    EventResponse, ConflictInfo,
)
from rrule_engine import build_rrule, expand
from timezone import utc_to_local, local_to_utc, localize_and_convert
from interval_tree import Interval
from conflict import detect_conflicts_for_event
from series import split_series

router = APIRouter()


# ── Helpers ─────────────────────────────────────────────────────────────────

def _expand_event(event: Event, start_utc: dt.datetime, end_utc: dt.datetime) -> list[EventResponse]:
    """Expand a recurring event into individual occurrences within a UTC range."""
    rule = event.recurrence_rule
    if not rule:
        return []

    tz_name = event.timezone
    local_start = utc_to_local(event.start_utc, tz_name)
    duration = event.end_utc - event.start_utc

    rrule = build_rrule(
        dtstart_local=local_start,
        freq=rule.freq, interval=rule.interval,
        count=rule.count,
        until_local=utc_to_local(rule.until_utc, tz_name) if rule.until_utc else None,
        byday=rule.byday, bymonthday=rule.bymonthday, bymonth=rule.bymonth,
    )

    # Convert window to local time for expansion
    local_ws = utc_to_local(start_utc, tz_name)
    local_we = utc_to_local(end_utc, tz_name)

    occurrences = expand(rrule, local_ws, local_we)

    # Build exception lookup
    exc_map: dict[dt.date, RecurrenceException] = {
        exc.original_date: exc for exc in event.exceptions
    }

    results = []
    for occ_local in occurrences:
        occ_date = occ_local.date()

        if occ_date in exc_map:
            exc = exc_map[occ_date]
            if exc.is_deleted:
                continue
            # Modified occurrence
            occ_start_utc = exc.start_utc if exc.start_utc else local_to_utc(occ_local, tz_name).utc_dt
            occ_end_utc = exc.end_utc if exc.end_utc else occ_start_utc + duration
            occ_title = exc.title if exc.title else event.title
            results.append(EventResponse(
                id=event.id, title=occ_title,
                start=occ_start_utc.isoformat() + "Z",
                end=occ_end_utc.isoformat() + "Z",
                timezone=tz_name, is_recurring=True,
                original_date=occ_date.isoformat(), is_exception=True,
            ))
        else:
            occ_utc = local_to_utc(occ_local, tz_name).utc_dt
            results.append(EventResponse(
                id=event.id, title=event.title,
                start=occ_utc.isoformat() + "Z",
                end=(occ_utc + duration).isoformat() + "Z",
                timezone=tz_name, is_recurring=True,
                original_date=occ_date.isoformat(), is_exception=False,
            ))

    return results


def _event_to_intervals(event: Event, start_utc: dt.datetime, end_utc: dt.datetime) -> list[Interval]:
    """Convert event (single or recurring) to Interval objects for conflict detection."""
    if event.is_recurring:
        responses = _expand_event(event, start_utc, end_utc)
        intervals = []
        for r in responses:
            s = dt.datetime.fromisoformat(r.start.rstrip("Z")).timestamp()
            e = dt.datetime.fromisoformat(r.end.rstrip("Z")).timestamp()
            intervals.append(Interval(low=s, high=e, event_id=event.id, title=event.title))
        return intervals
    else:
        return [Interval(
            low=event.start_utc.timestamp(), high=event.end_utc.timestamp(),
            event_id=event.id, title=event.title,
        )]


# ── POST /events ────────────────────────────────────────────────────────────

@router.post("/events")
def create_event(body: EventCreate, db: Session = Depends(get_db)):
    tz_name = body.timezone
    start_utc = localize_and_convert(body.start, tz_name)
    end_utc = localize_and_convert(body.end, tz_name)

    event = Event(
        title=body.title,
        start_utc=start_utc,
        end_utc=end_utc,
        timezone=tz_name,
        is_recurring=body.recurrence is not None,
    )
    db.add(event)
    db.flush()

    if body.recurrence:
        r = body.recurrence
        until_utc = None
        if r.until:
            until_utc = localize_and_convert(r.until + "T23:59:59", tz_name)
        rule = RecurrenceRule(
            event_id=event.id,
            freq=r.freq.upper(),
            interval=r.interval,
            count=r.count,
            until_utc=until_utc,
            byday=r.byday,
            bymonthday=r.bymonthday,
            bymonth=r.bymonth,
        )
        db.add(rule)

    db.flush()

    # Conflict detection: check first 90 days
    window_end = start_utc + dt.timedelta(days=90)
    new_intervals = _event_to_intervals(event, start_utc, window_end)

    existing_events = db.query(Event).filter(Event.id != event.id).all()
    existing_intervals: list[Interval] = []
    for ev in existing_events:
        existing_intervals.extend(_event_to_intervals(ev, start_utc, window_end))

    conflicts = detect_conflicts_for_event(new_intervals, existing_intervals)

    db.commit()
    db.refresh(event)

    return {
        "event": EventResponse(
            id=event.id, title=event.title,
            start=event.start_utc.isoformat() + "Z",
            end=event.end_utc.isoformat() + "Z",
            timezone=event.timezone, is_recurring=event.is_recurring,
        ),
        "conflicts": conflicts,
    }


# ── GET /events ─────────────────────────────────────────────────────────────

@router.get("/events", response_model=list[EventResponse])
def get_events(
    start: str = Query(..., description="ISO date or datetime"),
    end: str = Query(..., description="ISO date or datetime"),
    tz: str = Query("UTC"),
    db: Session = Depends(get_db),
):
    # Parse range
    try:
        start_dt = dt.datetime.fromisoformat(start)
    except ValueError:
        start_dt = dt.datetime.fromisoformat(start + "T00:00:00")
    try:
        end_dt = dt.datetime.fromisoformat(end)
    except ValueError:
        end_dt = dt.datetime.fromisoformat(end + "T23:59:59")

    start_utc = localize_and_convert(start_dt.isoformat(), tz)
    end_utc = localize_and_convert(end_dt.isoformat(), tz)

    results: list[EventResponse] = []

    # Non-recurring: SQL overlap query
    single_events = (
        db.query(Event)
        .filter(Event.is_recurring == False, Event.start_utc < end_utc, Event.end_utc > start_utc)
        .all()
    )
    for ev in single_events:
        results.append(EventResponse(
            id=ev.id, title=ev.title,
            start=ev.start_utc.isoformat() + "Z",
            end=ev.end_utc.isoformat() + "Z",
            timezone=ev.timezone, is_recurring=False,
        ))

    # Recurring: expand each one
    recurring_events = (
        db.query(Event)
        .filter(Event.is_recurring == True, Event.start_utc <= end_utc)
        .all()
    )
    for ev in recurring_events:
        results.extend(_expand_event(ev, start_utc, end_utc))

    results.sort(key=lambda r: r.start)
    return results


# ── PUT /events/:id ─────────────────────────────────────────────────────────

@router.put("/events/{event_id}")
def update_event(event_id: int, body: EventUpdate, db: Session = Depends(get_db)):
    event = db.query(Event).get(event_id)
    if not event:
        raise HTTPException(404, "Event not found")

    if body.title is not None:
        event.title = body.title
    if body.start is not None:
        event.start_utc = localize_and_convert(body.start, event.timezone)
    if body.end is not None:
        event.end_utc = localize_and_convert(body.end, event.timezone)

    db.commit()
    db.refresh(event)
    return EventResponse(
        id=event.id, title=event.title,
        start=event.start_utc.isoformat() + "Z",
        end=event.end_utc.isoformat() + "Z",
        timezone=event.timezone, is_recurring=event.is_recurring,
    )


# ── PUT /events/:id/occurrence/:date ────────────────────────────────────────

@router.put("/events/{event_id}/occurrence/{date_str}")
def update_occurrence(event_id: int, date_str: str, body: OccurrenceUpdate, db: Session = Depends(get_db)):
    event = db.query(Event).get(event_id)
    if not event:
        raise HTTPException(404, "Event not found")
    if not event.is_recurring:
        raise HTTPException(400, "Event is not recurring")

    occ_date = dt.date.fromisoformat(date_str)

    # Upsert exception
    exc = (
        db.query(RecurrenceException)
        .filter_by(event_id=event_id, original_date=occ_date)
        .first()
    )
    if not exc:
        exc = RecurrenceException(event_id=event_id, original_date=occ_date, is_deleted=False)
        db.add(exc)

    exc.is_deleted = False
    if body.title is not None:
        exc.title = body.title
    if body.start is not None:
        exc.start_utc = localize_and_convert(body.start, event.timezone)
    if body.end is not None:
        exc.end_utc = localize_and_convert(body.end, event.timezone)

    db.commit()
    return {"status": "ok", "original_date": date_str}


# ── PUT /events/:id/series ──────────────────────────────────────────────────

@router.put("/events/{event_id}/series")
def update_series(event_id: int, body: SeriesUpdate, db: Session = Depends(get_db)):
    event = db.query(Event).get(event_id)
    if not event:
        raise HTTPException(404, "Event not found")
    if not event.is_recurring:
        raise HTTPException(400, "Event is not recurring")

    split_date = dt.date.fromisoformat(body.from_date)
    updates = {}
    if body.title is not None:
        updates["title"] = body.title
    if body.start is not None:
        updates["start"] = body.start
    if body.end is not None:
        updates["end"] = body.end

    new_event = split_series(db, event, split_date, updates)
    db.commit()
    db.refresh(new_event)

    return EventResponse(
        id=new_event.id, title=new_event.title,
        start=new_event.start_utc.isoformat() + "Z",
        end=new_event.end_utc.isoformat() + "Z",
        timezone=new_event.timezone, is_recurring=new_event.is_recurring,
    )


# ── DELETE /events/:id/occurrence/:date ─────────────────────────────────────

@router.delete("/events/{event_id}/occurrence/{date_str}")
def delete_occurrence(event_id: int, date_str: str, db: Session = Depends(get_db)):
    event = db.query(Event).get(event_id)
    if not event:
        raise HTTPException(404, "Event not found")
    if not event.is_recurring:
        raise HTTPException(400, "Event is not recurring")

    occ_date = dt.date.fromisoformat(date_str)

    exc = (
        db.query(RecurrenceException)
        .filter_by(event_id=event_id, original_date=occ_date)
        .first()
    )
    if exc:
        exc.is_deleted = True
        exc.title = None
        exc.start_utc = None
        exc.end_utc = None
    else:
        exc = RecurrenceException(
            event_id=event_id, original_date=occ_date, is_deleted=True,
        )
        db.add(exc)

    db.commit()
    return {"status": "deleted", "original_date": date_str}


# ── DELETE /events/:id ──────────────────────────────────────────────────────

@router.delete("/events/{event_id}")
def delete_event(event_id: int, db: Session = Depends(get_db)):
    event = db.query(Event).get(event_id)
    if not event:
        raise HTTPException(404, "Event not found")

    db.delete(event)
    db.commit()
    return {"status": "deleted"}

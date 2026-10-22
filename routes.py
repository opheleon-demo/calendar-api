"""
Calendar event endpoints.

Every endpoint is scoped to the authenticated user: events are created with the
caller as owner, and lookups, lists and conflict candidates only ever consider
the caller's events. Another user's event is indistinguishable from a missing
one (404).

Writes validate the final values before committing once; any validation or
database failure rolls back the whole request.
"""
from __future__ import annotations
import datetime as dt

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, selectinload

from auth import get_current_user
from conflict import report_for_event, report_for_occurrence
from database import get_db
from models import (
    Event, RecurrenceException, User,
    EventCreate, EventUpdate, OccurrenceUpdate, SeriesUpdate,
    EventResponse, EventDetail, EventSaveResponse, ExceptionResponse, RecurrenceResponse,
    ConflictInfo, ConflictCoverage,
)
from occurrences import Occurrence, get_occurrence, owned_occurrences
from series import apply_rule, edit_whole_series, spec_from_schema, split_series, validate_series
from timezone import utc_to_local
from validation import InvalidInput, require_order, require_timezone, to_utc

router = APIRouter(dependencies=[Depends(get_current_user)])

MAX_QUERY_RANGE = dt.timedelta(days=5 * 366)


class OccurrenceSaveResponse(BaseModel):
    occurrence: EventResponse
    conflicts: list[ConflictInfo]
    conflict_coverage: ConflictCoverage


# ── Helpers ─────────────────────────────────────────────────────────────────

def _iso(value: dt.datetime) -> str:
    return value.isoformat() + "Z"


def _local_iso(value: dt.datetime, tz_name: str) -> str:
    return utc_to_local(value, tz_name).isoformat()


def _occurrence_response(occ: Occurrence) -> EventResponse:
    ev = occ.event
    return EventResponse(
        id=ev.id, title=occ.title,
        start=_iso(occ.start_utc), end=_iso(occ.end_utc),
        start_local=_local_iso(occ.start_utc, ev.timezone),
        end_local=_local_iso(occ.end_utc, ev.timezone),
        timezone=ev.timezone, is_recurring=ev.is_recurring,
        original_date=occ.original_date.isoformat() if occ.original_date else None,
        is_exception=occ.is_exception,
    )


def _event_detail(event: Event) -> EventDetail:
    recurrence = None
    exceptions: list[ExceptionResponse] = []
    rule = event.recurrence_rule
    if event.is_recurring and rule is not None:
        recurrence = RecurrenceResponse(
            freq=rule.freq, interval=rule.interval,
            until=utc_to_local(rule.until_utc, event.timezone).date().isoformat() if rule.until_utc else None,
            byday=rule.byday, bymonthday=rule.bymonthday,
        )
        exceptions = [
            ExceptionResponse(
                original_date=exc.original_date.isoformat(), is_deleted=exc.is_deleted,
                title=exc.title,
                start=_iso(exc.start_utc) if exc.start_utc else None,
                end=_iso(exc.end_utc) if exc.end_utc else None,
            )
            for exc in sorted(event.exceptions, key=lambda e: e.original_date)
        ]
    return EventDetail(
        id=event.id, title=event.title,
        start=_iso(event.start_utc), end=_iso(event.end_utc),
        start_local=_local_iso(event.start_utc, event.timezone),
        end_local=_local_iso(event.end_utc, event.timezone),
        timezone=event.timezone, is_recurring=event.is_recurring,
        recurrence=recurrence, exceptions=exceptions,
    )


def _not_found(what: str = "Event") -> HTTPException:
    return HTTPException(404, f"{what} not found")


def _get_owned_event(db: Session, event_id: int, user: User) -> Event:
    event = db.scalar(
        select(Event)
        .options(selectinload(Event.recurrence_rule), selectinload(Event.exceptions))
        .where(Event.id == event_id, Event.owner_id == user.id)
    )
    if event is None:
        raise _not_found()
    return event


def _get_owned_series(db: Session, event_id: int, user: User) -> Event:
    event = _get_owned_event(db, event_id, user)
    if not event.is_recurring:
        raise HTTPException(400, "Event is not recurring")
    return event


def _parse_occurrence_date(date_str: str) -> dt.date:
    try:
        return dt.date.fromisoformat(date_str)
    except ValueError:
        raise HTTPException(422, "Occurrence date must be an ISO date (YYYY-MM-DD)") from None


def _invalid(db: Session, exc: InvalidInput) -> HTTPException:
    db.rollback()
    return HTTPException(422, str(exc))


def _commit(db: Session) -> None:
    try:
        db.commit()
    except SQLAlchemyError:
        db.rollback()
        raise HTTPException(500, "The change could not be saved; nothing was changed") from None


def _save_event_response(db: Session, event: Event, user: User) -> EventSaveResponse:
    """Flush, compute conflicts against the stored state, then commit."""
    try:
        db.flush()
        conflicts, coverage = report_for_event(db, user.id, event)
    except SQLAlchemyError:
        db.rollback()
        raise HTTPException(500, "The change could not be saved; nothing was changed") from None
    _commit(db)
    db.refresh(event)
    return EventSaveResponse(event=_event_detail(event), conflicts=conflicts, conflict_coverage=coverage)


# ── POST /events ────────────────────────────────────────────────────────────

@router.post("/events", response_model=EventSaveResponse)
def create_event(body: EventCreate, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    try:
        tz_name = require_timezone(body.timezone)
        start_utc = to_utc(body.start, tz_name, "start")
        end_utc = to_utc(body.end, tz_name, "end")
        require_order(start_utc, end_utc)
        event = Event(
            owner_id=user.id,  # ownership always comes from the token, never the body
            title=body.title,
            start_utc=start_utc, end_utc=end_utc,
            timezone=tz_name,
            is_recurring=body.recurrence is not None,
        )
        if body.recurrence:
            apply_rule(event, spec_from_schema(body.recurrence))
            validate_series(event)
    except InvalidInput as exc:
        raise _invalid(db, exc)

    db.add(event)
    return _save_event_response(db, event, user)


# ── GET /events ─────────────────────────────────────────────────────────────

def _parse_range_bound(value: str, tz_name: str, field: str, is_end: bool) -> dt.datetime:
    text = value.strip()
    if len(text) == 10:  # date only: whole local days, end date inclusive
        try:
            day = dt.date.fromisoformat(text)
        except ValueError:
            raise InvalidInput(f"{field} must be an ISO date or datetime") from None
        if is_end:
            day += dt.timedelta(days=1)
        text = dt.datetime.combine(day, dt.time()).isoformat()
    return to_utc(text, tz_name, field)


@router.get("/events", response_model=list[EventResponse])
def get_events(
    start: str = Query(..., description="ISO date or datetime (local to tz)"),
    end: str = Query(..., description="ISO date (inclusive) or datetime (exclusive), local to tz"),
    tz: str = Query("UTC"),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        tz_name = require_timezone(tz)
        start_utc = _parse_range_bound(start, tz_name, "start", is_end=False)
        end_utc = _parse_range_bound(end, tz_name, "end", is_end=True)
        require_order(start_utc, end_utc)
    except InvalidInput as exc:
        raise HTTPException(422, str(exc))
    if end_utc - start_utc > MAX_QUERY_RANGE:
        raise HTTPException(422, f"Query range must not exceed {MAX_QUERY_RANGE.days} days")

    return [_occurrence_response(o) for o in owned_occurrences(db, user.id, start_utc, end_utc)]


# ── GET /events/:id ─────────────────────────────────────────────────────────

@router.get("/events/{event_id}", response_model=EventDetail)
def get_event(event_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return _event_detail(_get_owned_event(db, event_id, user))


# ── PUT /events/:id ─────────────────────────────────────────────────────────

@router.put("/events/{event_id}", response_model=EventSaveResponse)
def update_event(
    event_id: int, body: EventUpdate,
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    """Edit a single event, or the whole series of a recurring event."""
    event = _get_owned_event(db, event_id, user)
    try:
        if event.is_recurring:
            edit_whole_series(event, body)
        else:
            if body.recurrence is not None:
                raise InvalidInput("recurrence can only be changed on a recurring event")
            tz_name = require_timezone(body.timezone) if body.timezone is not None else event.timezone
            start_utc = to_utc(body.start, tz_name, "start") if body.start is not None else event.start_utc
            end_utc = to_utc(body.end, tz_name, "end") if body.end is not None else event.end_utc
            require_order(start_utc, end_utc)
            if body.title is not None:
                event.title = body.title
            event.timezone = tz_name
            event.start_utc, event.end_utc = start_utc, end_utc
    except InvalidInput as exc:
        raise _invalid(db, exc)
    return _save_event_response(db, event, user)


# ── PUT /events/:id/series ──────────────────────────────────────────────────

@router.put("/events/{event_id}/series", response_model=EventSaveResponse)
def update_series(
    event_id: int, body: SeriesUpdate,
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    """Edit the whole series, or with from_date, only occurrences on/after that date."""
    event = _get_owned_series(db, event_id, user)
    try:
        if body.from_date is None:
            target = edit_whole_series(event, body)
        else:
            target = split_series(db, event, body.from_date, body)
    except InvalidInput as exc:
        raise _invalid(db, exc)
    return _save_event_response(db, target, user)


# ── PUT /events/:id/occurrence/:date ────────────────────────────────────────

@router.put("/events/{event_id}/occurrence/{date_str}", response_model=OccurrenceSaveResponse)
def update_occurrence(
    event_id: int, date_str: str, body: OccurrenceUpdate,
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    event = _get_owned_series(db, event_id, user)
    occ_date = _parse_occurrence_date(date_str)
    current = get_occurrence(event, occ_date)
    if current is None:
        raise _not_found("Occurrence")

    try:
        start_utc = to_utc(body.start, event.timezone, "start") if body.start is not None else current.start_utc
        end_utc = to_utc(body.end, event.timezone, "end") if body.end is not None else current.end_utc
        require_order(start_utc, end_utc)
    except InvalidInput as exc:
        raise _invalid(db, exc)

    exc = next((e for e in event.exceptions if e.original_date == occ_date), None)
    if exc is None:
        exc = RecurrenceException(original_date=occ_date, is_deleted=False)
        event.exceptions.append(exc)
    if body.title is not None:
        exc.title = None if body.title == event.title else body.title
    if body.start is not None or body.end is not None:
        exc.start_utc, exc.end_utc = start_utc, end_utc

    try:
        db.flush()
        saved = get_occurrence(event, occ_date)
        conflicts, coverage = report_for_occurrence(db, user.id, saved)
    except SQLAlchemyError:
        db.rollback()
        raise HTTPException(500, "The change could not be saved; nothing was changed") from None
    _commit(db)
    return OccurrenceSaveResponse(
        occurrence=_occurrence_response(saved), conflicts=conflicts, conflict_coverage=coverage,
    )


# ── DELETE /events/:id/occurrence/:date ─────────────────────────────────────

@router.delete("/events/{event_id}/occurrence/{date_str}")
def delete_occurrence(
    event_id: int, date_str: str,
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    event = _get_owned_series(db, event_id, user)
    occ_date = _parse_occurrence_date(date_str)
    if get_occurrence(event, occ_date) is None:
        raise _not_found("Occurrence")

    exc = next((e for e in event.exceptions if e.original_date == occ_date), None)
    if exc is None:
        exc = RecurrenceException(original_date=occ_date)
        event.exceptions.append(exc)
    exc.is_deleted = True
    exc.title = exc.start_utc = exc.end_utc = None

    _commit(db)
    return {"status": "deleted", "original_date": occ_date.isoformat()}


# ── DELETE /events/:id ──────────────────────────────────────────────────────

@router.delete("/events/{event_id}")
def delete_event(event_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Delete a single event, or a recurring event's entire series."""
    event = _get_owned_event(db, event_id, user)
    db.delete(event)
    _commit(db)
    return {"status": "deleted"}

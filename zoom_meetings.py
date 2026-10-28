"""
Zoom meetings attached to calendar events.

An event's meeting is created, updated and deleted with the organizer's own
Zoom connection (see zoom.zoom_request). Every function here commits the event
itself before talking to Zoom and reports Zoom problems as a ZoomError instead
of raising, so a Zoom failure never loses or rolls back event data.

Meeting times come from the stored UTC times and the event's IANA timezone:
the start is the organizer's local wall-clock time and the duration is the real
elapsed time, so events that cross midnight or a DST change line up in Zoom.

Recurring series get a single "recurring meeting with no fixed time" (Zoom
type 3): one join link is shared by every occurrence, so per-occurrence edits
never need to touch Zoom.
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy.orm import Session

import zoom
from models import Event, User, ZoomError
from timezone import utc_to_local


# Placeholder in zoom_meeting_id while a create request is in flight. Claiming
# it with a conditional UPDATE guarantees one meeting per event even when the
# same create or edit is submitted twice at once.
PENDING = "pending"

MEETING_TYPE_SCHEDULED = 2
MEETING_TYPE_RECURRING_NO_FIXED_TIME = 3
TOPIC_MAX_LENGTH = 200

_MESSAGES = {
    "not_connected": "Connect your Zoom account to add a Zoom meeting.",
    "reconnect_required": "Your Zoom connection expired. Reconnect Zoom to sync this meeting.",
    "zoom_unavailable": "Zoom is unavailable right now.",
    "zoom_error": "Zoom rejected the request.",
}


class _ZoomCallFailed(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _error(code: str, action: str) -> ZoomError:
    return ZoomError(code=code, message=f"{action} {_MESSAGES[code]}")


def has_meeting(event: Event) -> bool:
    return bool(event.zoom_meeting_id) and event.zoom_meeting_id != PENDING


def join_url(event: Event) -> Optional[str]:
    return event.zoom_join_url if has_meeting(event) else None


def meeting_body(event: Event) -> dict:
    """Zoom meeting fields for an event: title, local start, duration, timezone."""
    duration_minutes = max(1, round((event.end_utc - event.start_utc).total_seconds() / 60))
    body = {
        "topic": event.title[:TOPIC_MAX_LENGTH],
        "timezone": event.timezone,
        "duration": duration_minutes,
    }
    if event.is_recurring:
        body["type"] = MEETING_TYPE_RECURRING_NO_FIXED_TIME
    else:
        local_start = utc_to_local(event.start_utc, event.timezone)
        body["type"] = MEETING_TYPE_SCHEDULED
        # No trailing "Z": Zoom reads the wall-clock time in `timezone`.
        body["start_time"] = local_start.strftime("%Y-%m-%dT%H:%M:%S")
    return body


def _call(db: Session, user: User, method: str, path: str, ok_statuses: tuple[int, ...], **kwargs):
    try:
        res = zoom.zoom_request(db, user, method, path, **kwargs)
    except zoom.ZoomReconnectRequired:
        raise _ZoomCallFailed("reconnect_required")
    except zoom.ZoomNotConnected:
        raise _ZoomCallFailed("not_connected")
    except zoom.ZoomUnavailable:
        raise _ZoomCallFailed("zoom_unavailable")
    if res.status_code not in ok_statuses:
        raise _ZoomCallFailed("zoom_error")
    return res


def create_meeting(db: Session, user: User, event: Event) -> Optional[ZoomError]:
    """Create the event's Zoom meeting unless it already has (or is creating) one."""
    claimed = (
        db.query(Event)
        .filter(Event.id == event.id, Event.zoom_meeting_id.is_(None))
        .update({Event.zoom_meeting_id: PENDING}, synchronize_session=False)
    )
    db.commit()
    db.refresh(event)
    if not claimed:
        return None

    try:
        res = _call(db, user, "POST", "/users/me/meetings", (200, 201), json=meeting_body(event))
        try:
            payload = res.json()
        except ValueError:
            payload = {}
        meeting_id, url = payload.get("id"), payload.get("join_url")
        if not meeting_id or not url:
            raise _ZoomCallFailed("zoom_error")
    except _ZoomCallFailed as exc:
        db.refresh(event)
        event.zoom_meeting_id = None
        event.zoom_join_url = None
        db.commit()
        return _error(exc.code, "Zoom link was not created.")

    db.refresh(event)
    event.zoom_meeting_id = str(meeting_id)
    event.zoom_join_url = url
    db.commit()
    return None


def update_meeting(db: Session, user: User, event: Event) -> Optional[ZoomError]:
    """Push the event's current title and time to its Zoom meeting."""
    if not has_meeting(event):
        return None
    try:
        _call(db, user, "PATCH", f"/meetings/{event.zoom_meeting_id}", (200, 204), json=meeting_body(event))
    except _ZoomCallFailed as exc:
        return _error(exc.code, "The event was saved but its Zoom meeting was not updated.")
    return None


def delete_meeting(db: Session, user: User, event: Event) -> Optional[ZoomError]:
    """Delete the event's Zoom meeting and clear the link from the event."""
    if not has_meeting(event):
        return None
    try:
        # 404: already deleted in Zoom, which is the outcome we want.
        _call(db, user, "DELETE", f"/meetings/{event.zoom_meeting_id}", (200, 204, 404))
    except _ZoomCallFailed as exc:
        error = _error(exc.code, "The Zoom meeting could not be deleted in Zoom.")
    else:
        error = None
    db.refresh(event)
    event.zoom_meeting_id = None
    event.zoom_join_url = None
    db.commit()
    return error

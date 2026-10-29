"""Durable, fail-closed Zoom provisioning for a new event or finite series."""
import datetime as dt
import hashlib
import json
import uuid

import httpx
from fastapi import HTTPException
from sqlalchemy.orm import Session

from models import CreationRequest, EventMeeting
from rrule_engine import build_rrule, expand
from timezone import localize_and_convert, local_to_utc, utc_to_local
from zoom import api_call, failure, invalidate, lock, usable_connection


def resolve_schedule(body):
    try:
        import pytz
        pytz.timezone(body.timezone)
        start = localize_and_convert(body.start, body.timezone)
        end = localize_and_convert(body.end, body.timezone)
        seconds = (end - start).total_seconds()
        if start.microsecond or end.microsecond or seconds <= 0 or seconds % 60 or seconds > 24 * 3600:
            raise ValueError("Use a positive duration in whole minutes, no longer than 24 hours.")
        rule = None
        starts = [start]
        if body.recurrence:
            r = body.recurrence
            if not r.count and not r.until:
                raise ValueError("New recurring series need a count (up to 60) or an end date within 10 years.")
            local_start = utc_to_local(start, body.timezone)
            until = dt.datetime.fromisoformat(r.until + "T23:59:59") if r.until else None
            horizon = local_start + dt.timedelta(days=3660)
            if until and until > horizon:
                raise ValueError("The recurrence end date must be within 10 years.")
            rule = build_rrule(local_start, r.freq, r.interval, r.count, until, r.byday, r.bymonthday, r.bymonth)
            # Validate fields which the existing engine otherwise silently ignores.
            if r.freq.upper() not in ("DAILY", "WEEKLY", "MONTHLY", "YEARLY"):
                raise ValueError("Unsupported recurrence frequency.")
            if r.bymonth and any(not 1 <= int(v) <= 12 for v in r.bymonth.split(',')):
                raise ValueError("Invalid recurrence month.")
            if r.bymonthday and any(int(v) == 0 or not -31 <= int(v) <= 31 for v in r.bymonthday.split(',')):
                raise ValueError("Invalid recurrence day.")
            if rule.byday and any(v.ordinal is not None and (v.ordinal == 0 or abs(v.ordinal) > 5) for v in rule.byday):
                raise ValueError("Invalid weekday ordinal.")
            if r.freq.upper() in ("DAILY", "WEEKLY") and (r.bymonthday or r.bymonth or (r.freq.upper() == "DAILY" and r.byday)):
                raise ValueError("This recurrence filter is unsupported for daily/weekly events.")
            if r.freq.upper() == "WEEKLY" and rule.byday and any(v.ordinal is not None for v in rule.byday):
                raise ValueError("Weekly recurrence requires plain weekdays.")
            occurrences = expand(rule, local_start, horizon)
            if not occurrences or len(occurrences) > 60 or (r.count and len(occurrences) < r.count):
                raise ValueError("Use 1 to 60 occurrences within 10 years.")
            if occurrences[0] != local_start:
                raise ValueError("The recurrence must include the event's start date. Choose a first date matching its repeat rule.")
            starts = [local_to_utc(occ, body.timezone).utc_dt for occ in occurrences]
            starts[0] = start  # preserve an explicit offset on a folded first instant
            if len(set(starts)) != len(starts):
                raise ValueError("Recurrence resolves to duplicate instants.")
        return start, end, [(s, s + dt.timedelta(seconds=seconds)) for s in starts]
    except (ValueError, KeyError, OverflowError) as error:
        raise failure("invalid_schedule", str(error) or "Invalid event time or recurrence.", 422)


def marker(meeting):
    return "calendar-request:" + meeting.id


def capture(meeting, result, host_id):
    # Never associate another host's meeting, nor expose host start URLs.
    if str(result.get("host_id", "")) != host_id or not result.get("id") or not result.get("join_url"):
        raise failure("zoom_outcome_uncertain", "Zoom returned an incomplete meeting. Retry this same draft to reconcile.")
    try:
        remote_start = dt.datetime.fromisoformat(result["start_time"].replace("Z", "+00:00"))
        if remote_start.tzinfo is None:
            raise ValueError()
        remote_start = remote_start.astimezone(dt.UTC).replace(tzinfo=None)
        if remote_start != meeting.start_utc or int(result["duration"]) != int((meeting.end_utc - meeting.start_utc).total_seconds() // 60):
            raise ValueError()
    except (KeyError, ValueError, TypeError):
        raise failure("zoom_outcome_uncertain", "Zoom's scheduled time could not be confirmed. Retry this same draft to reconcile.")
    from urllib.parse import urlparse
    join = urlparse(result["join_url"])
    if join.scheme != "https" or not (join.hostname == "zoom.us" or (join.hostname or "").endswith(".zoom.us")):
        raise failure("zoom_outcome_uncertain", "Zoom returned an invalid join link. Retry this same draft to reconcile.")
    meeting.meeting_id = str(result["id"])
    meeting.join_url = result["join_url"]
    meeting.host_id = host_id
    meeting.state = "ready"


def reconcile(token, meeting, host_id):
    """A negative search is never proof that a timed-out POST did not succeed."""
    page = ""
    while True:
        response = api_call(token, "GET", "/users/me/meetings", params={"type": "scheduled", "page_size": 300, "next_page_token": page})
        if response.status_code in (401, 403):
            raise failure("zoom_reconnect_required", "Reconnect the original Zoom account, then retry this same draft.")
        if response.status_code != 200:
            raise failure("zoom_unavailable", "Zoom reconciliation is temporarily unavailable. Retry this same draft.", 503)
        data = response.json()
        for item in data.get("meetings", []):
            # List responses may omit agenda; retrieve the authoritative meeting detail.
            if "agenda" not in item:
                detail = api_call(token, "GET", "/meetings/" + str(item["id"]))
                if detail.status_code != 200:
                    continue
                item = detail.json()
            if item.get("agenda") == marker(meeting):
                capture(meeting, item, host_id)
                return True
        page = data.get("next_page_token")
        if not page:
            return False


def prepare(db: Session, user_id, body, request_key, schedule):
    lock(db)
    request = db.query(CreationRequest).filter_by(organizer_id=user_id, request_key=request_key).first()
    fingerprint = hashlib.sha256(json.dumps(body.model_dump(), sort_keys=True).encode()).hexdigest()
    if request and request.fingerprint != fingerprint:
        raise failure("request_payload_changed", "This request already belongs to another draft. Restore its original values before retrying.")
    if request and request.response_json:
        response = json.loads(request.response_json)
        db.commit()
        return request, None, None, response
    connection, token = usable_connection(db, user_id)
    # Refresh persists token rotation and briefly releases the writer lock. Reload
    # the request in case another worker created/finalized it during that interval.
    request = db.query(CreationRequest).filter_by(organizer_id=user_id, request_key=request_key).populate_existing().first()
    if request and request.fingerprint != fingerprint:
        raise failure("request_payload_changed", "This request already belongs to another draft. Restore its original values before retrying.")
    if request and request.response_json:
        response = json.loads(request.response_json)
        db.commit()
        return request, None, None, response
    if request and request.zoom_user_id != connection.zoom_user_id:
        raise failure("zoom_original_account_required", "Reconnect the Zoom account originally used for this draft before retrying.")
    if request is None:
        request = CreationRequest(id=str(uuid.uuid4()), organizer_id=user_id, request_key=request_key,
                                  fingerprint=fingerprint, zoom_user_id=connection.zoom_user_id)
        db.add(request)
        db.flush()
        for start, end in schedule:
            db.add(EventMeeting(id=str(uuid.uuid4()), request_id=request.id, start_utc=start, end_utc=end, state="new"))
    db.commit()
    return request, connection, token, None


def provision(db, request, connection, token, title):
    request_id, host_id = request.id, request.zoom_user_id
    ids = [m.id for m in db.query(EventMeeting).filter_by(request_id=request_id).order_by(EventMeeting.start_utc).all()]
    for meeting_id in ids:
        lock(db)
        meeting = db.get(EventMeeting, meeting_id)
        if meeting.state == "ready":
            db.commit()
            continue
        try:
            if meeting.state == "uncertain":
                if reconcile(token, meeting, host_id):
                    db.commit()
                    continue
                raise failure("zoom_outcome_uncertain", "Zoom may still be processing this meeting. Retry this same draft to check again. No duplicate will be requested; contact support if it remains pending.")
            # Commit intent BEFORE the request. Any crash thereafter requires reconciliation.
            meeting.state = "uncertain"
            db.commit()
            lock(db)
            meeting = db.get(EventMeeting, meeting_id)
            if meeting.state == "ready":
                db.commit()
                continue
            result = api_call(token, "POST", "/users/me/meetings", json={
                "topic": title, "type": 2, "start_time": meeting.start_utc.isoformat() + "Z",
                "timezone": "UTC", "duration": int((meeting.end_utc - meeting.start_utc).total_seconds() // 60),
                "agenda": marker(meeting), "settings": {"use_pmi": False},
            })
            authorization_failure = result.status_code in (401, 403)
            if result.status_code == 400:
                try:
                    authorization_failure = result.json().get("code") in (124, 4700, 4711)
                except (ValueError, AttributeError):
                    pass
            if authorization_failure:
                meeting.state = "new"  # definitive rejection, no remote meeting
                db.commit()
                invalidate(db, connection)
            if 400 <= result.status_code < 500 and result.status_code != 408:
                meeting.state = "new"  # rate limit/validation rejection is safe to retry
                db.commit()
                raise failure("zoom_request_rejected", "Zoom rejected this meeting request. Check Zoom permissions and account limits, then retry this same draft.", 503)
            if result.status_code != 201:
                raise failure("zoom_outcome_uncertain", "Zoom could not confirm creation. Retry this same draft to reconcile; no duplicate will be requested.", 503)
            capture(meeting, result.json(), host_id)
            db.commit()
        except (httpx.TransportError, ValueError, KeyError, TypeError):
            db.rollback()
            raise failure("zoom_outcome_uncertain", "Zoom did not confirm the result. Retry this same draft to reconcile; no duplicate will be requested.", 503)

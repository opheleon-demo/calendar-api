"""Organizer-scoped OAuth and Zoom transport. Secrets never leave this module."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import secrets
from urllib.parse import urlencode, urlparse

import httpx
from cryptography.fernet import Fernet, InvalidToken
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from auth import get_current_user
from database import get_db
from models import User, ZoomConnection, ZoomOAuthState

router = APIRouter(prefix="/zoom", tags=["zoom"])
API = "https://api.zoom.us/v2"
COOKIE = "calendar_zoom_binding"


def now():
    return dt.datetime.now(dt.UTC).replace(tzinfo=None)


def failure(code, message, status=409):
    return HTTPException(status, {"code": code, "message": message})


def configured():
    return all(os.getenv(key) for key in ("ZOOM_CLIENT_ID", "ZOOM_CLIENT_SECRET", "ZOOM_REDIRECT_URI", "ZOOM_TOKEN_KEY"))


def cipher():
    if not configured():
        raise failure("zoom_not_configured", "Zoom setup is incomplete. Contact the calendar administrator.", 503)
    try:
        return Fernet(os.environ["ZOOM_TOKEN_KEY"].encode())
    except ValueError:
        raise failure("zoom_not_configured", "Zoom token encryption is not configured correctly.", 503)


def lock(db):
    """SQLite's writer lock serializes refresh, provisioning and finalization across workers."""
    db.rollback()
    db.execute(text("BEGIN IMMEDIATE"))


def transport(method, url, **kwargs):
    # No automatic retries: a POST timeout may already have created a meeting.
    with httpx.Client(timeout=20, follow_redirects=False) as client:
        return client.request(method, url, **kwargs)


def token_exchange(data):
    cipher()
    try:
        response = transport("POST", "https://zoom.us/oauth/token", data=data,
                             auth=(os.environ["ZOOM_CLIENT_ID"], os.environ["ZOOM_CLIENT_SECRET"]))
    except httpx.TransportError:
        raise failure("zoom_reconnect_required", "Zoom authorization could not be confirmed. Connect Zoom again.")
    if response.status_code >= 400:
        raise failure("zoom_reconnect_required", "Zoom authorization failed or expired. Connect Zoom again.")
    try:
        result = response.json()
        if not result["access_token"] or not result["refresh_token"] or int(result["expires_in"]) <= 0:
            raise ValueError()
        return result
    except (KeyError, ValueError, TypeError):
        raise failure("zoom_reconnect_required", "Zoom authorization could not be confirmed. Connect Zoom again.")


def store_tokens(connection, tokens):
    granted = set(tokens.get("scope", "").split())
    required = (
        {"user:read:user", "user:read"},
        {"meeting:write:meeting", "meeting:write"},
        {"meeting:read:list_meetings", "meeting:read"},
        {"meeting:read:meeting", "meeting:read"},
    )
    if any(not granted.intersection(options) for options in required):
        raise failure("zoom_permissions_required", "Zoom did not grant the required user and meeting permissions. Reconnect Zoom after the app permissions are corrected.")
    f = cipher()
    connection.access_token = f.encrypt(tokens["access_token"].encode()).decode()
    connection.refresh_token = f.encrypt(tokens["refresh_token"].encode()).decode()
    connection.expires_at = now() + dt.timedelta(seconds=int(tokens["expires_in"]))
    connection.usable = True


def refresh(db, connection):
    try:
        refresh_token = cipher().decrypt(connection.refresh_token.encode()).decode()
    except InvalidToken:
        invalidate(db, connection)
    # A crash/timeout during token rotation requires reconnection, never reuse of a rotated token.
    connection.usable = False
    db.commit()
    lock(db)
    tokens = token_exchange({"grant_type": "refresh_token", "refresh_token": refresh_token})
    store_tokens(connection, tokens)
    db.commit()
    lock(db)


def invalidate(db, connection):
    connection.usable = False
    db.commit()
    raise failure("zoom_reconnect_required", "Zoom access was revoked or permissions are missing. Reconnect Zoom before retrying.")


def api_call(token, method, path, **kwargs):
    return transport(method, API + path, headers={"Authorization": "Bearer " + token}, **kwargs)


def usable_connection(db, organizer_id):
    cipher()
    connection = db.get(ZoomConnection, organizer_id)
    if not connection or not connection.usable:
        raise failure("zoom_connection_required", "Connect or reconnect your Zoom account before creating an event.")
    if connection.expires_at <= now() + dt.timedelta(seconds=60):
        refresh(db, connection)
    try:
        token = cipher().decrypt(connection.access_token.encode()).decode()
    except InvalidToken:
        invalidate(db, connection)
    try:
        result = api_call(token, "GET", "/users/me")
        if result.status_code == 401:
            refresh(db, connection)
            token = cipher().decrypt(connection.access_token.encode()).decode()
            result = api_call(token, "GET", "/users/me")
        if result.status_code in (400, 401, 403):
            invalidate(db, connection)
        if result.status_code != 200:
            raise failure("zoom_unavailable", "Zoom is temporarily unavailable. Keep your draft and retry.", 503)
        if result.json().get("id") != connection.zoom_user_id:
            invalidate(db, connection)
    except (httpx.TransportError, ValueError):
        raise failure("zoom_unavailable", "Could not verify Zoom access. Keep your draft and retry.", 503)
    return connection, token


@router.get("/connection")
def connection_status(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if not configured():
        return {"connected": False, "code": "zoom_not_configured", "message": "Zoom setup is incomplete. Contact the calendar administrator."}
    lock(db)
    try:
        connection, _ = usable_connection(db, user.id)
        result = {"connected": True, "zoom_user_id": connection.zoom_user_id}
        db.commit()
        return result
    except HTTPException as error:
        db.rollback()
        return {"connected": False, **error.detail}


@router.post("/connect")
def connect(response: Response, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    cipher()
    state, browser = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    lock(db)
    # Only the most recent connection flow for this organizer remains valid.
    db.query(ZoomOAuthState).filter(ZoomOAuthState.organizer_id == user.id).delete()
    db.query(ZoomOAuthState).filter(ZoomOAuthState.expires_at < now()).delete()
    db.add(ZoomOAuthState(digest=hashlib.sha256(state.encode()).hexdigest(), organizer_id=user.id,
                          browser_digest=hashlib.sha256(browser.encode()).hexdigest(),
                          expires_at=now() + dt.timedelta(minutes=10)))
    db.commit()
    response.set_cookie(COOKIE, browser, httponly=True, secure=urlparse(os.environ["ZOOM_REDIRECT_URI"]).scheme == "https",
                        samesite="lax", max_age=600, path="/zoom")
    response.headers["Cache-Control"] = "no-store"
    return {"url": "https://zoom.us/oauth/authorize?" + urlencode({
        "response_type": "code", "client_id": os.environ["ZOOM_CLIENT_ID"],
        "redirect_uri": os.environ["ZOOM_REDIRECT_URI"], "state": state})}


def callback_page(status):
    origin = os.getenv("CALENDAR_UI_ORIGIN", "http://localhost:5173")
    # Fixed status and configured origin only. No code, token, provider text, or state rendered.
    response = HTMLResponse("<!doctype html><meta charset=utf-8><title>Zoom connection</title>"
        "<p>Zoom connection " + ("complete" if status == "connected" else "failed. Return to the calendar and reconnect Zoom") + ". You can close this window.</p>"
        "<script>if(window.opener){window.opener.postMessage({type:'calendar-zoom',status:" + json.dumps(status) + "}," + json.dumps(origin).replace("<", "\\u003c") + ");window.close()}</script>")
    response.delete_cookie(COOKIE, path="/zoom")
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@router.get("/callback")
def callback(request: Request, state: str = "", code: str = "", error: str = "", db: Session = Depends(get_db)):
    lock(db)
    record = db.get(ZoomOAuthState, hashlib.sha256(state.encode()).hexdigest())
    browser = request.cookies.get(COOKIE, "")
    if not record or record.expires_at < now() or not secrets.compare_digest(record.browser_digest, hashlib.sha256(browser.encode()).hexdigest()):
        db.rollback()
        return callback_page("invalid_state")
    organizer_id = record.organizer_id
    db.delete(record)
    db.commit()  # single use, even on denial or exchange failure
    if error or not code:
        return callback_page("denied")
    try:
        tokens = token_exchange({"grant_type": "authorization_code", "code": code,
                                 "redirect_uri": os.environ["ZOOM_REDIRECT_URI"]})
        profile = api_call(tokens["access_token"], "GET", "/users/me")
        if profile.status_code != 200 or not profile.json().get("id"):
            return callback_page("failed")
        lock(db)
        connection = db.get(ZoomConnection, organizer_id)
        if connection is None:
            connection = ZoomConnection(organizer_id=organizer_id)
            db.add(connection)
        connection.zoom_user_id = profile.json()["id"]
        store_tokens(connection, tokens)
        db.commit()
        return callback_page("connected")
    except (HTTPException, httpx.TransportError, ValueError, KeyError):
        db.rollback()
        return callback_page("failed")


@router.delete("/connection")
def disconnect(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    lock(db)
    connection = db.get(ZoomConnection, user.id)
    if connection:
        # Disable locally first. Remote revoke is best effort; tokens are then erased locally.
        try:
            access = cipher().decrypt(connection.access_token.encode()).decode()
        except InvalidToken:
            access = None
        db.delete(connection)
        db.commit()
        try:
            if not access:
                return {"connected": False}
            transport("POST", "https://zoom.us/oauth/revoke", data={"token": access},
                      auth=(os.environ["ZOOM_CLIENT_ID"], os.environ["ZOOM_CLIENT_SECRET"]))
        except httpx.TransportError:
            pass
    else:
        db.commit()
    return {"connected": False}

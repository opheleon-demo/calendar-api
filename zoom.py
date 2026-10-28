"""
Per-user Zoom account connection (OAuth 2.0 authorization code flow).

Each calendar user connects their own Zoom account. Access and refresh tokens
are encrypted at rest with Fernet, every lookup is scoped to the signed-in
user, and tokens are never returned from the API. `zoom_request` is the entry
point for making Zoom API calls on a user's behalf: it refreshes expired
tokens and marks the connection `reconnect_required` when Zoom rejects them.

Configuration (environment):
  ZOOM_CLIENT_ID, ZOOM_CLIENT_SECRET  OAuth app credentials
  ZOOM_REDIRECT_URI                   must match the Zoom app; defaults to
                                      http://localhost:8000/zoom/callback
  ZOOM_TOKEN_ENCRYPTION_KEY           Fernet key used to encrypt stored tokens
  FRONTEND_URL                        where the callback sends the browser;
                                      defaults to http://localhost:5173
"""
from __future__ import annotations

import datetime as dt
import os
import secrets
from typing import Optional
from urllib.parse import urlencode

import httpx
from cryptography.fernet import Fernet, InvalidToken
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from auth import get_current_user
from database import get_db
from models import (
    User, ZoomConnection, ZoomOAuthState,
    ZoomAuthorizeResponse, ZoomStatusResponse,
)


ZOOM_AUTHORIZE_URL = "https://zoom.us/oauth/authorize"
ZOOM_TOKEN_URL = "https://zoom.us/oauth/token"
ZOOM_REVOKE_URL = "https://zoom.us/oauth/revoke"
ZOOM_API_BASE = "https://api.zoom.us/v2"

DEFAULT_REDIRECT_URI = "http://localhost:8000/zoom/callback"
DEFAULT_FRONTEND_URL = "http://localhost:5173"

STATE_TTL = dt.timedelta(minutes=10)
# Refresh a little before Zoom's stated expiry so in-flight calls don't race it.
EXPIRY_MARGIN = dt.timedelta(seconds=60)
HTTP_TIMEOUT_SECONDS = 10.0

STATUS_CONNECTED = "connected"
STATUS_NOT_CONNECTED = "not_connected"
STATUS_RECONNECT_REQUIRED = "reconnect_required"

# Tests swap this for an httpx.MockTransport.
http_transport: Optional[httpx.BaseTransport] = None

router = APIRouter(prefix="/zoom", tags=["zoom"])


class ZoomNotConnected(Exception):
    """The user has no usable Zoom connection."""


class ZoomReconnectRequired(ZoomNotConnected):
    """Zoom rejected the stored tokens (expired refresh token or app revoked)."""


class ZoomUnavailable(Exception):
    """Zoom could not be reached or returned a server error."""


# ── Configuration ───────────────────────────────────────────────────────────

def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC).replace(tzinfo=None)


def _client_credentials() -> tuple[str, str]:
    return os.getenv("ZOOM_CLIENT_ID", ""), os.getenv("ZOOM_CLIENT_SECRET", "")


def _redirect_uri() -> str:
    return os.getenv("ZOOM_REDIRECT_URI") or DEFAULT_REDIRECT_URI


def _frontend_url() -> str:
    return (os.getenv("FRONTEND_URL") or DEFAULT_FRONTEND_URL).rstrip("/")


def _fernet() -> Optional[Fernet]:
    key = os.getenv("ZOOM_TOKEN_ENCRYPTION_KEY")
    if not key:
        return None
    try:
        return Fernet(key)
    except (ValueError, TypeError):
        return None


def is_configured() -> bool:
    client_id, client_secret = _client_credentials()
    return bool(client_id and client_secret and _fernet())


def _require_configured() -> None:
    if not is_configured():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Zoom integration is not configured on the server",
        )


def _encrypt(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode()


def _decrypt(value: str) -> str:
    return _fernet().decrypt(value.encode()).decode()


# ── Zoom HTTP calls ─────────────────────────────────────────────────────────

def _http() -> httpx.Client:
    return httpx.Client(timeout=HTTP_TIMEOUT_SECONDS, transport=http_transport)


def _token_request(data: dict) -> dict:
    """POST to Zoom's token endpoint. Raises ZoomReconnectRequired on 4xx."""
    try:
        with _http() as client:
            res = client.post(ZOOM_TOKEN_URL, data=data, auth=_client_credentials())
    except httpx.HTTPError as exc:
        raise ZoomUnavailable(str(exc)) from exc
    if res.status_code >= 500:
        raise ZoomUnavailable(f"Zoom token endpoint returned {res.status_code}")
    if res.status_code >= 400:
        raise ZoomReconnectRequired(f"Zoom token endpoint returned {res.status_code}")
    payload = res.json()
    if not payload.get("access_token") or not payload.get("refresh_token"):
        raise ZoomReconnectRequired("Zoom token response is missing tokens")
    return payload


def _revoke(access_token: str) -> None:
    """Best-effort revoke at Zoom; local deletion proceeds regardless."""
    try:
        with _http() as client:
            client.post(ZOOM_REVOKE_URL, data={"token": access_token}, auth=_client_credentials())
    except httpx.HTTPError:
        pass


def _get_profile(access_token: str) -> dict:
    try:
        with _http() as client:
            res = client.get(
                f"{ZOOM_API_BASE}/users/me",
                headers={"Authorization": f"Bearer {access_token}"},
            )
    except httpx.HTTPError as exc:
        raise ZoomUnavailable(str(exc)) from exc
    if res.status_code >= 400:
        raise ZoomUnavailable(f"Zoom /users/me returned {res.status_code}")
    return res.json()


# ── Token storage ───────────────────────────────────────────────────────────

def _connection_for(db: Session, user: User) -> Optional[ZoomConnection]:
    return db.query(ZoomConnection).filter(ZoomConnection.user_id == user.id).first()


def _store_tokens(conn: ZoomConnection, tokens: dict) -> None:
    conn.access_token_encrypted = _encrypt(tokens["access_token"])
    conn.refresh_token_encrypted = _encrypt(tokens["refresh_token"])
    conn.token_expires_at = _now() + dt.timedelta(seconds=int(tokens.get("expires_in", 3600)))
    conn.scope = tokens.get("scope")
    conn.status = STATUS_CONNECTED


def _mark_reconnect_required(db: Session, conn: ZoomConnection) -> None:
    conn.status = STATUS_RECONNECT_REQUIRED
    conn.access_token_encrypted = None
    conn.refresh_token_encrypted = None
    conn.token_expires_at = None
    db.commit()


def _refresh(db: Session, conn: ZoomConnection) -> str:
    try:
        refresh_token = _decrypt(conn.refresh_token_encrypted)
        tokens = _token_request({"grant_type": "refresh_token", "refresh_token": refresh_token})
    except (InvalidToken, ZoomReconnectRequired) as exc:
        _mark_reconnect_required(db, conn)
        raise ZoomReconnectRequired("Zoom authorization expired or was revoked") from exc
    # Zoom rotates refresh tokens, so persist both new tokens immediately.
    _store_tokens(conn, tokens)
    db.commit()
    return tokens["access_token"]


def get_access_token(db: Session, user: User, force_refresh: bool = False) -> str:
    """Return a valid access token for `user`, refreshing it when needed."""
    if not is_configured():
        raise ZoomNotConnected("Zoom integration is not configured")
    conn = _connection_for(db, user)
    if not conn:
        raise ZoomNotConnected("Zoom is not connected")
    if conn.status != STATUS_CONNECTED or not conn.refresh_token_encrypted:
        raise ZoomReconnectRequired("Zoom needs to be reconnected")

    expired = not conn.token_expires_at or conn.token_expires_at - EXPIRY_MARGIN <= _now()
    if force_refresh or expired or not conn.access_token_encrypted:
        return _refresh(db, conn)
    try:
        return _decrypt(conn.access_token_encrypted)
    except InvalidToken:
        return _refresh(db, conn)


def zoom_request(db: Session, user: User, method: str, path: str, **kwargs) -> httpx.Response:
    """Call the Zoom REST API as `user`, retrying once with a refreshed token on 401."""
    url = f"{ZOOM_API_BASE}{path}"
    extra_headers = kwargs.pop("headers", {})
    for attempt in range(2):
        token = get_access_token(db, user, force_refresh=attempt > 0)
        headers = {**extra_headers, "Authorization": f"Bearer {token}"}
        try:
            with _http() as client:
                res = client.request(method, url, headers=headers, **kwargs)
        except httpx.HTTPError as exc:
            raise ZoomUnavailable(str(exc)) from exc
        if res.status_code != 401:
            if res.status_code >= 500:
                raise ZoomUnavailable(f"Zoom API returned {res.status_code}")
            return res
    # A freshly refreshed token was still rejected: the app was revoked in Zoom.
    conn = _connection_for(db, user)
    if conn:
        _mark_reconnect_required(db, conn)
    raise ZoomReconnectRequired("Zoom rejected the access token")


# ── OAuth state ─────────────────────────────────────────────────────────────

def _create_state(db: Session, user: User) -> str:
    now = _now()
    db.query(ZoomOAuthState).filter(ZoomOAuthState.expires_at < now).delete()
    state = secrets.token_urlsafe(32)
    db.add(ZoomOAuthState(state=state, user_id=user.id, expires_at=now + STATE_TTL))
    db.commit()
    return state


def _consume_state(db: Session, state: Optional[str]) -> Optional[int]:
    """Delete the state row and return its user id if it was valid and unexpired."""
    if not state:
        return None
    row = db.query(ZoomOAuthState).filter(ZoomOAuthState.state == state).first()
    if not row:
        return None
    user_id = row.user_id if row.expires_at >= _now() else None
    db.delete(row)
    db.commit()
    return user_id


def _redirect_to_ui(result: str, reason: Optional[str] = None) -> RedirectResponse:
    params = {"zoom": result}
    if reason:
        params["zoom_reason"] = reason
    return RedirectResponse(f"{_frontend_url()}/?{urlencode(params)}", status_code=302)


# ── Endpoints ───────────────────────────────────────────────────────────────

@router.get("/authorize", response_model=ZoomAuthorizeResponse)
def start_authorization(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return the Zoom consent URL. The UI navigates the browser to it."""
    _require_configured()
    client_id, _ = _client_credentials()
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": _redirect_uri(),
        "state": _create_state(db, user),
    }
    return ZoomAuthorizeResponse(authorization_url=f"{ZOOM_AUTHORIZE_URL}?{urlencode(params)}")


@router.get("/callback", include_in_schema=False)
def oauth_callback(
    code: Optional[str] = None,
    state: Optional[str] = None,
    error: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """Zoom redirects the browser here; we always redirect back to the UI."""
    user_id = _consume_state(db, state)
    if error or not code:
        return _redirect_to_ui("error", "denied")
    if user_id is None or not is_configured():
        return _redirect_to_ui("error", "invalid_state")
    user = db.get(User, user_id)
    if not user:
        return _redirect_to_ui("error", "invalid_state")

    try:
        tokens = _token_request({
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": _redirect_uri(),
        })
    except (ZoomUnavailable, ZoomReconnectRequired):
        return _redirect_to_ui("error", "exchange_failed")

    try:
        profile = _get_profile(tokens["access_token"])
    except ZoomUnavailable:
        _revoke(tokens["access_token"])
        return _redirect_to_ui("error", "exchange_failed")

    conn = _connection_for(db, user) or ZoomConnection(user_id=user.id)
    conn.zoom_user_id = profile.get("id")
    conn.zoom_email = profile.get("email")
    _store_tokens(conn, tokens)
    db.add(conn)
    db.commit()
    return _redirect_to_ui("connected")


@router.get("/status", response_model=ZoomStatusResponse)
def get_status(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    configured = is_configured()
    conn = _connection_for(db, user)
    if not conn:
        return ZoomStatusResponse(configured=configured, status=STATUS_NOT_CONNECTED)

    if configured and conn.status == STATUS_CONNECTED:
        # Verify the authorization is still live so revocations in Zoom surface here.
        # zoom_request marks the connection reconnect_required when Zoom rejects
        # it; if Zoom is merely unreachable we report the last known state.
        try:
            zoom_request(db, user, "GET", "/users/me")
        except (ZoomReconnectRequired, ZoomUnavailable):
            pass
        db.refresh(conn)

    return ZoomStatusResponse(
        configured=configured,
        status=conn.status,
        zoom_email=conn.zoom_email,
        connected_at=conn.created_at.isoformat() + "Z" if conn.created_at else None,
    )


@router.delete("/connection", response_model=ZoomStatusResponse)
def disconnect(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Revoke and delete this user's Zoom tokens. Events are left untouched."""
    conn = _connection_for(db, user)
    if conn:
        if is_configured() and conn.access_token_encrypted:
            try:
                _revoke(_decrypt(conn.access_token_encrypted))
            except InvalidToken:
                pass
        db.delete(conn)
        db.commit()
    return ZoomStatusResponse(configured=is_configured(), status=STATUS_NOT_CONNECTED)

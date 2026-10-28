"""Zoom connection tests. Zoom itself is replaced with an httpx.MockTransport."""
import datetime as dt
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import zoom
from database import get_db
from main import app
from models import Base, Event, ZoomConnection, ZoomOAuthState


class FakeZoom:
    """Minimal stand-in for Zoom's OAuth and REST endpoints."""

    def __init__(self):
        self.issued = 0
        self.valid_access = set()
        self.valid_refresh = set()
        self.revoked = []
        self.token_status = None   # force a status code from /oauth/token
        self.api_status = None     # force a status code from the REST API
        self.requests = []

    def _issue(self):
        self.issued += 1
        access, refresh = f"access-{self.issued}", f"refresh-{self.issued}"
        self.valid_access.add(access)
        self.valid_refresh.add(refresh)
        return httpx.Response(200, json={
            "access_token": access, "refresh_token": refresh,
            "expires_in": 3600, "scope": "user:read",
        })

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)
        if url == zoom.ZOOM_TOKEN_URL:
            if self.token_status:
                return httpx.Response(self.token_status, json={"reason": "forced"})
            form = parse_qs(request.content.decode())
            if form["grant_type"] == ["authorization_code"]:
                if form["code"] != ["good-code"]:
                    return httpx.Response(400, json={"reason": "Invalid authorization code"})
                return self._issue()
            refresh = form["refresh_token"][0]
            if refresh not in self.valid_refresh:
                return httpx.Response(400, json={"reason": "Invalid Token!"})
            self.valid_refresh.discard(refresh)
            return self._issue()
        if url == zoom.ZOOM_REVOKE_URL:
            token = parse_qs(request.content.decode())["token"][0]
            self.revoked.append(token)
            self.valid_access.discard(token)
            return httpx.Response(200, json={"status": "success"})
        if url == f"{zoom.ZOOM_API_BASE}/users/me":
            if self.api_status:
                return httpx.Response(self.api_status)
            token = request.headers["Authorization"].removeprefix("Bearer ")
            if token not in self.valid_access:
                return httpx.Response(401, json={"code": 124, "message": "Invalid access token."})
            return httpx.Response(200, json={"id": "zoom-user-1", "email": "alice@example.com"})
        return httpx.Response(404)

    def revoke_app(self):
        """Simulate the user removing the app inside Zoom."""
        self.valid_access.clear()
        self.valid_refresh.clear()


@pytest.fixture()
def env(monkeypatch):
    monkeypatch.setenv("JWT_SECRET_KEY", "x" * 40)
    monkeypatch.setenv("ZOOM_CLIENT_ID", "client-id")
    monkeypatch.setenv("ZOOM_CLIENT_SECRET", "client-secret")
    monkeypatch.setenv("ZOOM_TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("FRONTEND_URL", "http://ui.test")

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    TestSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    def override_get_db():
        db = TestSession()
        try:
            yield db
        finally:
            db.close()

    fake = FakeZoom()
    monkeypatch.setattr(zoom, "http_transport", httpx.MockTransport(fake.handler))
    app.dependency_overrides[get_db] = override_get_db
    # No `with` block: skip the lifespan so the real database is never touched.
    client = TestClient(app)
    yield client, fake, TestSession
    app.dependency_overrides.clear()


def register(client, username):
    res = client.post("/auth/register", json={"username": username, "password": "password123"})
    assert res.status_code == 200
    return {"Authorization": f"Bearer {res.json()['access_token']}"}


def start_auth(client, headers):
    res = client.get("/zoom/authorize", headers=headers)
    assert res.status_code == 200
    url = urlparse(res.json()["authorization_url"])
    params = parse_qs(url.query)
    assert url.netloc == "zoom.us"
    assert params["client_id"] == ["client-id"]
    return params["state"][0]


def callback(client, **params):
    res = client.get("/zoom/callback", params=params, follow_redirects=False)
    assert res.status_code == 302
    location = urlparse(res.headers["location"])
    assert f"{location.scheme}://{location.netloc}" == "http://ui.test"
    return {k: v[0] for k, v in parse_qs(location.query).items()}


def connect(client, headers):
    state = start_auth(client, headers)
    assert callback(client, code="good-code", state=state) == {"zoom": "connected"}


def test_endpoints_require_login(env):
    client, _, _ = env
    for method, path in [("get", "/zoom/authorize"), ("get", "/zoom/status"), ("delete", "/zoom/connection")]:
        assert getattr(client, method)(path).status_code == 401


def test_connect_stores_encrypted_tokens_and_reports_status(env):
    client, _, Session = env
    headers = register(client, "alice")
    assert client.get("/zoom/status", headers=headers).json()["status"] == "not_connected"

    connect(client, headers)

    body = client.get("/zoom/status", headers=headers).json()
    assert body["status"] == "connected"
    assert body["zoom_email"] == "alice@example.com"
    assert "token" not in str(body).lower()

    with Session() as db:
        conn = db.query(ZoomConnection).one()
        assert conn.zoom_user_id == "zoom-user-1"
        assert "access-" not in conn.access_token_encrypted
        assert "refresh-" not in conn.refresh_token_encrypted
        assert db.query(ZoomOAuthState).count() == 0


def test_state_is_single_use(env):
    client, fake, Session = env
    headers = register(client, "alice")
    state = start_auth(client, headers)
    assert callback(client, code="good-code", state=state)["zoom"] == "connected"
    assert callback(client, code="good-code", state=state) == {"zoom": "error", "zoom_reason": "invalid_state"}


def test_unknown_or_expired_state_is_rejected(env):
    client, _, Session = env
    headers = register(client, "alice")
    assert callback(client, code="good-code", state="forged")["zoom_reason"] == "invalid_state"

    state = start_auth(client, headers)
    with Session() as db:
        row = db.query(ZoomOAuthState).one()
        row.expires_at = dt.datetime(2000, 1, 1)
        db.commit()
    assert callback(client, code="good-code", state=state)["zoom_reason"] == "invalid_state"
    with Session() as db:
        assert db.query(ZoomConnection).count() == 0


def test_denied_authorization_stores_nothing(env):
    """E1: cancelling or denying in Zoom connects nothing and reports it."""
    client, _, Session = env
    headers = register(client, "alice")
    state = start_auth(client, headers)
    assert callback(client, error="access_denied", state=state) == {"zoom": "error", "zoom_reason": "denied"}
    assert callback(client, state=start_auth(client, headers)) == {"zoom": "error", "zoom_reason": "denied"}
    with Session() as db:
        assert db.query(ZoomConnection).count() == 0
        assert db.query(ZoomOAuthState).count() == 0
    assert client.get("/zoom/status", headers=headers).json()["status"] == "not_connected"


def test_failed_code_exchange_stores_nothing(env):
    client, fake, Session = env
    headers = register(client, "alice")
    assert callback(client, code="bad-code", state=start_auth(client, headers))["zoom_reason"] == "exchange_failed"
    fake.token_status = 503
    assert callback(client, code="good-code", state=start_auth(client, headers))["zoom_reason"] == "exchange_failed"
    with Session() as db:
        assert db.query(ZoomConnection).count() == 0


def test_expired_access_token_is_refreshed(env):
    client, fake, Session = env
    headers = register(client, "alice")
    connect(client, headers)
    with Session() as db:
        conn = db.query(ZoomConnection).one()
        conn.token_expires_at = dt.datetime(2000, 1, 1)
        old_refresh = conn.refresh_token_encrypted
        db.commit()

    assert client.get("/zoom/status", headers=headers).json()["status"] == "connected"
    with Session() as db:
        conn = db.query(ZoomConnection).one()
        assert conn.refresh_token_encrypted != old_refresh
        assert conn.token_expires_at > dt.datetime.now(dt.UTC).replace(tzinfo=None)


def test_revoked_in_zoom_shows_reconnect_required(env):
    """E2: a revoked authorization surfaces as reconnect_required, not a silent failure."""
    client, fake, Session = env
    headers = register(client, "alice")
    connect(client, headers)
    fake.revoke_app()

    body = client.get("/zoom/status", headers=headers).json()
    assert body["status"] == "reconnect_required"
    with Session() as db:
        conn = db.query(ZoomConnection).one()
        assert conn.access_token_encrypted is None and conn.refresh_token_encrypted is None

    with Session() as db:
        user = db.get(zoom.User, conn.user_id)
        with pytest.raises(zoom.ZoomReconnectRequired):
            zoom.zoom_request(db, user, "GET", "/users/me")

    # Reconnecting restores the connection.
    connect(client, headers)
    assert client.get("/zoom/status", headers=headers).json()["status"] == "connected"


def test_zoom_outage_keeps_last_known_state(env):
    client, fake, _ = env
    headers = register(client, "alice")
    connect(client, headers)
    fake.api_status = 503
    assert client.get("/zoom/status", headers=headers).json()["status"] == "connected"


def test_disconnect_revokes_and_keeps_events(env):
    """E3: disconnecting deletes tokens only; events are untouched."""
    client, fake, Session = env
    headers = register(client, "alice")
    created = client.post("/events", headers=headers, json={
        "title": "Standup", "start": "2026-11-02T09:00:00", "end": "2026-11-02T09:15:00",
        "timezone": "America/New_York",
    })
    assert created.status_code == 200
    connect(client, headers)

    res = client.delete("/zoom/connection", headers=headers)
    assert res.status_code == 200
    assert res.json()["status"] == "not_connected"
    assert fake.revoked == ["access-1"]
    with Session() as db:
        assert db.query(ZoomConnection).count() == 0
        assert db.query(Event).filter(Event.title == "Standup").count() == 1
    assert client.get("/zoom/status", headers=headers).json()["status"] == "not_connected"
    # Disconnecting again is harmless.
    assert client.delete("/zoom/connection", headers=headers).status_code == 200


def test_connections_are_isolated_per_user(env):
    """E4: one user's Zoom connection is neither visible to nor usable by another."""
    client, _, Session = env
    alice = register(client, "alice")
    bob = register(client, "bob")
    connect(client, alice)

    assert client.get("/zoom/status", headers=bob).json()["status"] == "not_connected"
    client.delete("/zoom/connection", headers=bob)
    assert client.get("/zoom/status", headers=alice).json()["status"] == "connected"

    with Session() as db:
        bob_user = db.query(zoom.User).filter(zoom.User.username == "bob").one()
        with pytest.raises(zoom.ZoomNotConnected):
            zoom.get_access_token(db, bob_user)


def test_bob_cannot_complete_alices_authorization(env):
    """The state binds the callback to the user who started it."""
    client, _, Session = env
    alice = register(client, "alice")
    register(client, "bob")
    state = start_auth(client, alice)
    callback(client, code="good-code", state=state)
    with Session() as db:
        conn = db.query(ZoomConnection).one()
        assert db.get(zoom.User, conn.user_id).username == "alice"


def test_unconfigured_server(env, monkeypatch):
    client, _, _ = env
    monkeypatch.delenv("ZOOM_CLIENT_SECRET")
    headers = register(client, "alice")
    assert client.get("/zoom/authorize", headers=headers).status_code == 503
    body = client.get("/zoom/status", headers=headers).json()
    assert body == {"configured": False, "status": "not_connected", "zoom_email": None, "connected_at": None}

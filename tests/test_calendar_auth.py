"""
Every calendar route rejects requests without a valid bearer token and
returns no calendar data (REQ-3 / E1).
"""
from __future__ import annotations

import base64
import datetime as dt
import json

import jwt
import pytest
from fastapi.routing import APIRoute

import auth
from main import app
from models import Event, RecurrenceRule


SECRET = auth._get_jwt_secret_key()
OCC_DATE = "2026-04-06"

# (method, path, json body) for every calendar route. Event 1 is a weekly
# recurring event seeded by the `event` fixture.
CALENDAR_REQUESTS = [
    ("GET", "/events?start=2026-04-01&end=2026-04-30&tz=UTC", None),
    ("POST", "/events", {
        "title": "Intruder", "start": "2026-04-10T09:00:00",
        "end": "2026-04-10T10:00:00", "timezone": "UTC",
    }),
    ("PUT", "/events/1", {"title": "Hijacked"}),
    ("PUT", f"/events/1/occurrence/{OCC_DATE}", {"title": "Hijacked"}),
    ("PUT", "/events/1/series", {"from_date": OCC_DATE, "title": "Hijacked"}),
    ("DELETE", f"/events/1/occurrence/{OCC_DATE}", None),
    ("DELETE", "/events/1", None),
]


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _expired_token(user_id: int) -> str:
    past = dt.datetime.now(dt.UTC) - dt.timedelta(minutes=1)
    return jwt.encode({"sub": str(user_id), "exp": past}, SECRET, algorithm=auth.JWT_ALGORITHM)


def _tampered_payload_token(valid: str) -> str:
    """Swap the payload for a different subject but keep the original signature."""
    header, body, signature = valid.split(".")
    payload = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    payload["sub"] = "999"
    return f"{header}.{_b64url(json.dumps(payload).encode())}.{signature}"


def _wrong_key_token(user_id: int) -> str:
    future = dt.datetime.now(dt.UTC) + dt.timedelta(minutes=5)
    return jwt.encode(
        {"sub": str(user_id), "exp": future},
        "attacker-controlled-secret-key-32-characters",
        algorithm=auth.JWT_ALGORITHM,
    )


def _alg_none_token(user_id: int) -> str:
    future = int((dt.datetime.now(dt.UTC) + dt.timedelta(minutes=5)).timestamp())
    header = _b64url(json.dumps({"alg": "none", "typ": "JWT"}).encode())
    payload = _b64url(json.dumps({"sub": str(user_id), "exp": future}).encode())
    return f"{header}.{payload}."


def _unknown_user_token() -> str:
    future = dt.datetime.now(dt.UTC) + dt.timedelta(minutes=5)
    return jwt.encode({"sub": "999", "exp": future}, SECRET, algorithm=auth.JWT_ALGORITHM)


def _no_exp_token(user_id: int) -> str:
    return jwt.encode({"sub": str(user_id)}, SECRET, algorithm=auth.JWT_ALGORITHM)


def _bad_auth_headers(user_id: int, valid: str) -> dict[str, dict[str, str]]:
    return {
        "missing": {},
        "empty-bearer": {"Authorization": "Bearer "},
        "wrong-scheme": {"Authorization": f"Basic {valid}"},
        "raw-token-no-scheme": {"Authorization": valid},
        "malformed": {"Authorization": "Bearer not-a-jwt"},
        "malformed-segments": {"Authorization": "Bearer a.b.c"},
        "tampered-payload": {"Authorization": f"Bearer {_tampered_payload_token(valid)}"},
        "tampered-signature": {"Authorization": f"Bearer {valid[:-4]}AAAA"},
        "wrong-key": {"Authorization": f"Bearer {_wrong_key_token(user_id)}"},
        "alg-none": {"Authorization": f"Bearer {_alg_none_token(user_id)}"},
        "expired": {"Authorization": f"Bearer {_expired_token(user_id)}"},
        "unknown-user": {"Authorization": f"Bearer {_unknown_user_token()}"},
        "no-exp-claim": {"Authorization": f"Bearer {_no_exp_token(user_id)}"},
    }


BAD_TOKEN_KINDS = [
    "missing", "empty-bearer", "wrong-scheme", "raw-token-no-scheme",
    "malformed", "malformed-segments", "tampered-payload", "tampered-signature",
    "wrong-key", "alg-none", "expired", "unknown-user", "no-exp-claim",
]


@pytest.fixture
def event(session_factory):
    db = session_factory()
    try:
        ev = Event(
            id=1, title="Standup",
            start_utc=dt.datetime(2026, 4, 6, 13, 0),
            end_utc=dt.datetime(2026, 4, 6, 13, 15),
            timezone="UTC", is_recurring=True,
        )
        db.add(ev)
        db.flush()
        db.add(RecurrenceRule(event_id=ev.id, freq="WEEKLY", interval=1))
        db.commit()
    finally:
        db.close()


def _snapshot(session_factory):
    db = session_factory()
    try:
        return [
            (e.id, e.title, e.start_utc, e.end_utc, e.is_recurring,
             [(x.original_date, x.is_deleted, x.title) for x in e.exceptions],
             e.recurrence_rule.until_utc if e.recurrence_rule else None)
            for e in db.query(Event).order_by(Event.id).all()
        ]
    finally:
        db.close()


def test_every_calendar_route_is_covered():
    routes = {
        (method, route.path)
        for route in app.routes
        if isinstance(route, APIRoute) and route.path.startswith("/events")
        for method in route.methods
    }
    covered = set()
    for method, path, _ in CALENDAR_REQUESTS:
        for route in app.routes:
            if isinstance(route, APIRoute) and method in route.methods:
                if route.path_regex.match(path.split("?")[0]):
                    covered.add((method, route.path))
    assert routes == covered


@pytest.mark.parametrize("kind", BAD_TOKEN_KINDS)
@pytest.mark.parametrize("method,path,body", CALENDAR_REQUESTS)
def test_calendar_route_rejects_invalid_token(
    client, session_factory, user, token, event, method, path, body, kind,
):
    before = _snapshot(session_factory)
    headers = _bad_auth_headers(user.id, token)[kind]

    res = client.request(method, path, json=body, headers=headers)

    assert res.status_code == 401
    assert res.headers.get("www-authenticate") == "Bearer"
    assert set(res.json()) == {"detail"}
    assert "Standup" not in res.text
    assert _snapshot(session_factory) == before


@pytest.mark.parametrize("method,path,body", CALENDAR_REQUESTS)
def test_calendar_route_accepts_valid_token(client, token, event, method, path, body):
    res = client.request(method, path, json=body, headers={"Authorization": f"Bearer {token}"})
    assert res.status_code == 200, res.text


def test_valid_token_returns_events(client, token, event):
    res = client.get(
        "/events?start=2026-04-01&end=2026-04-30&tz=UTC",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 200
    assert [e["title"] for e in res.json()] == ["Standup"] * 4


def test_login_token_is_accepted_by_calendar_routes(client, user):
    login = client.post("/auth/login", json={"username": "alice", "password": "correct horse"})
    assert login.status_code == 200
    res = client.get(
        "/events?start=2026-04-01&end=2026-04-30",
        headers={"Authorization": f"Bearer {login.json()['access_token']}"},
    )
    assert res.status_code == 200


def test_auth_routes_do_not_require_token(client):
    res = client.post("/auth/register", json={"username": "bob", "password": "hunter2!"})
    assert res.status_code == 200
    assert res.json()["access_token"]

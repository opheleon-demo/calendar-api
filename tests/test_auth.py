"""Tests for registration, login, and bearer-token access to /events."""
import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-key-that-is-at-least-32-chars")

from database import get_db  # noqa: E402
from main import app  # noqa: E402
from models import Base, User  # noqa: E402


@pytest.fixture()
def client():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    TestingSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    def override_get_db():
        db = TestingSession()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    # No context manager: skip the lifespan so the real database is never touched.
    test_client = TestClient(app)
    test_client.session_factory = TestingSession
    yield test_client
    app.dependency_overrides.clear()


def register(client, username="alice", password="password123"):
    return client.post("/auth/register", json={"username": username, "password": password})


def user_count(client):
    db = client.session_factory()
    try:
        return db.query(User).count()
    finally:
        db.close()


def test_register_returns_token_accepted_by_calendar_api(client):
    res = register(client)
    assert res.status_code == 200
    body = res.json()
    assert body["token_type"] == "bearer"
    assert body["expires_in"] == 3600

    events = client.get(
        "/events",
        params={"start": "2026-01-01", "end": "2026-01-08"},
        headers={"Authorization": f"Bearer {body['access_token']}"},
    )
    assert events.status_code == 200


def test_register_duplicate_username_is_rejected_without_new_account(client):
    assert register(client).status_code == 200
    res = register(client, username="  alice  ")
    assert res.status_code == 409
    assert res.json()["detail"] == "Username already exists"
    assert user_count(client) == 1


@pytest.mark.parametrize(
    "payload, field",
    [
        ({"username": "", "password": "password123"}, "username"),
        ({"username": "   ", "password": "password123"}, "username"),
        ({"username": "a" * 151, "password": "password123"}, "username"),
        ({"username": "bob", "password": "short"}, "password"),
        ({"password": "password123"}, "username"),
    ],
)
def test_register_invalid_fields_return_per_field_422(client, payload, field):
    res = client.post("/auth/register", json=payload)
    assert res.status_code == 422
    assert any(err["loc"][-1] == field for err in res.json()["detail"])
    assert user_count(client) == 0


def test_login_returns_token(client):
    register(client)
    res = client.post("/auth/login", json={"username": "alice", "password": "password123"})
    assert res.status_code == 200
    assert res.json()["access_token"]
    assert res.json()["expires_in"] == 3600


def test_login_failure_is_generic(client):
    register(client)
    wrong_password = client.post("/auth/login", json={"username": "alice", "password": "wrongpass1"})
    unknown_user = client.post("/auth/login", json={"username": "nobody", "password": "password123"})
    assert wrong_password.status_code == unknown_user.status_code == 401
    assert wrong_password.json() == unknown_user.json()


def test_events_require_token(client):
    res = client.get("/events", params={"start": "2026-01-01", "end": "2026-01-08"})
    assert res.status_code == 401
    bad = client.get(
        "/events",
        params={"start": "2026-01-01", "end": "2026-01-08"},
        headers={"Authorization": "Bearer not-a-token"},
    )
    assert bad.status_code == 401

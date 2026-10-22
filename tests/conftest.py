import os
import tempfile

# Must be set before the app modules are imported.
_DB_DIR = tempfile.mkdtemp(prefix="calendar-api-tests-")
os.environ["CALENDAR_DATABASE_URL"] = f"sqlite:///{_DB_DIR}/test.db"
os.environ["JWT_SECRET_KEY"] = "test-only-secret-key-with-more-than-32-chars"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

PASSWORD = "correct horse battery staple"


@pytest.fixture
def client():
    from database import engine
    from main import app
    from models import Base

    Base.metadata.drop_all(bind=engine)
    with TestClient(app) as c:
        yield c


def register(client, username: str, password: str = PASSWORD) -> dict:
    resp = client.post("/auth/register", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest.fixture
def alice(client):
    return register(client, "alice")


@pytest.fixture
def bob(client):
    return register(client, "bob")


def create(client, headers, expected_status=200, **body):
    body.setdefault("title", "Event")
    resp = client.post("/events", json=body, headers=headers)
    assert resp.status_code == expected_status, resp.text
    return resp.json()


def list_events(client, headers, start="2026-01-01", end="2026-12-31", tz="UTC"):
    resp = client.get("/events", params={"start": start, "end": end, "tz": tz}, headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()

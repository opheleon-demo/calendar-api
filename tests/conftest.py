import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-key-that-is-at-least-32-chars")

from database import get_db  # noqa: E402
from main import app  # noqa: E402
from models import Base  # noqa: E402


@pytest.fixture
def engine(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'calendar.db'}", connect_args={"check_same_thread": False}
    )
    yield engine
    engine.dispose()


@pytest.fixture
def session_factory(engine):
    Base.metadata.create_all(bind=engine)
    return sessionmaker(autocommit=False, autoflush=False, bind=engine)


@pytest.fixture
def client(session_factory):
    def override_get_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    yield TestClient(app)
    app.dependency_overrides.clear()


def register(client, username, password="password123"):
    res = client.post("/auth/register", json={"username": username, "password": password})
    assert res.status_code == 200, res.text
    return {"Authorization": f"Bearer {res.json()['access_token']}"}


@pytest.fixture
def alice(client):
    return register(client, "alice")


@pytest.fixture
def bob(client):
    return register(client, "bob")

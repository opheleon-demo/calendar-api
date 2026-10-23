"""Shared test fixtures: isolated in-memory database and auth helpers."""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("JWT_SECRET_KEY", "test-secret-key-that-is-at-least-32-chars")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from auth import create_access_token, get_password_hash  # noqa: E402
from database import get_db  # noqa: E402
from main import app  # noqa: E402
from models import Base, User  # noqa: E402


@pytest.fixture
def session_factory():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    yield sessionmaker(autocommit=False, autoflush=False, bind=engine)
    engine.dispose()


@pytest.fixture
def client(session_factory):
    def override_get_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    # Not entered as a context manager, so the lifespan (which seeds the real
    # SQLite file) never runs.
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def user(session_factory):
    db = session_factory()
    try:
        u = User(username="alice", password_hash=get_password_hash("correct horse"))
        db.add(u)
        db.commit()
        db.refresh(u)
        db.expunge(u)
        return u
    finally:
        db.close()


@pytest.fixture
def token(user):
    return create_access_token(user)[0]

"""HTTP regressions against isolated in-memory data, never calendar.db."""
import os
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database import get_db
from main import app
from models import Base


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"JWT_SECRET_KEY": "local-test-secret-at-least-32-characters"})
        self.env.start()
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.session = sessionmaker(bind=self.engine)()
        app.dependency_overrides[get_db] = lambda: self.session
        # Do not enter lifespan: startup seeding must not touch the normal database.
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        app.dependency_overrides.clear()
        self.session.close()
        self.engine.dispose()
        self.env.stop()

    def test_public_health_exact_response_and_cors(self):
        def no_database():
            raise AssertionError("Health must not access a database")
        app.dependency_overrides[get_db] = no_database
        for headers in ({}, {"Authorization": "Bearer invalid", "Origin": "http://localhost:5173"}):
            response = self.client.get("/health", headers=headers)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["content-type"], "application/json")
            self.assertEqual(response.content, b'{"status":"ok"}')
        self.assertEqual(response.headers["access-control-allow-origin"], "*")

    def test_authentication_and_event_operations(self):
        event = {"title": "Test", "start": "2026-10-05T10:00:00", "end": "2026-10-05T11:00:00", "timezone": "UTC"}
        for method, path, body in [
            ("GET", "/events?start=2026-10-01&end=2026-11-01", None),
            ("POST", "/events", event), ("PUT", "/events/1", {"title": "Edit"}),
            ("PUT", "/events/1/occurrence/2026-10-05", {}),
            ("PUT", "/events/1/series", {"from_date": "2026-10-05"}),
            ("DELETE", "/events/1/occurrence/2026-10-05", None),
            ("DELETE", "/events/1", None),
        ]:
            self.assertEqual(self.client.request(method, path, json=body).status_code, 401)
        credentials = {"username": "tester", "password": "test-password"}
        registered = self.client.post("/auth/register", json=credentials)
        self.assertEqual(registered.status_code, 200)
        self.assertEqual(self.client.post("/auth/register", json=credentials).status_code, 409)
        self.assertEqual(self.client.post("/auth/login", json={**credentials, "password": "wrong"}).status_code, 401)
        login = self.client.post("/auth/login", json=credentials)
        self.assertEqual(login.status_code, 200)
        self.client.headers["Authorization"] = f"Bearer {login.json()['access_token']}"
        created = self.client.post("/events", json=event)
        self.assertEqual(created.status_code, 200)
        event_id = created.json()["event"]["id"]
        self.assertEqual(self.client.put(f"/events/{event_id}", json={"title": "Edited"}).json()["title"], "Edited")
        query = "/events?start=2026-10-01&end=2026-11-01&tz=UTC"
        self.assertEqual(self.client.get(query).json()[0]["title"], "Edited")
        self.assertEqual(self.client.delete(f"/events/{event_id}").status_code, 200)
        self.assertEqual(self.client.get(query).json(), [])
        recurring = self.client.post("/events", json={**event, "recurrence": {"freq": "DAILY", "count": 5}})
        self.assertEqual(recurring.status_code, 200)
        series_id = recurring.json()["event"]["id"]
        self.assertEqual(len(self.client.get(query).json()), 5)
        self.assertEqual(self.client.put(f"/events/{series_id}/occurrence/2026-10-06", json={"title": "Exception"}).status_code, 200)
        self.assertIn("Exception", [e["title"] for e in self.client.get(query).json()])
        self.assertEqual(self.client.delete(f"/events/{series_id}/occurrence/2026-10-07").status_code, 200)
        self.assertEqual(len(self.client.get(query).json()), 4)
        split = self.client.put(f"/events/{series_id}/series", json={"from_date": "2026-10-08", "title": "Future"})
        self.assertEqual(split.status_code, 200)
        self.assertEqual(split.json()["title"], "Future")


if __name__ == "__main__":
    unittest.main()

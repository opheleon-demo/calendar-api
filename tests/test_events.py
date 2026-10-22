"""REQ-2: private CRUD for non-recurring events."""
import pytest
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from conftest import create, list_events

LUNCH = {"title": "Lunch", "start": "2026-04-01T12:00", "end": "2026-04-01T13:00", "timezone": "America/New_York"}


def test_new_account_list_is_empty(client, alice):
    assert list_events(client, alice, "2000-01-01", "2004-12-31") == []
    assert list_events(client, alice) == []


def test_create_retrieve_list_and_persist(client, alice):
    saved = create(client, alice, **LUNCH)
    event = saved["event"]
    assert saved["conflicts"] == []
    assert event["start"] == "2026-04-01T16:00:00Z"  # stored in UTC
    assert event["start_local"] == "2026-04-01T12:00:00"
    assert event["timezone"] == "America/New_York"

    # Fresh requests (new DB sessions) see the persisted event.
    got = client.get(f"/events/{event['id']}", headers=alice)
    assert got.status_code == 200
    assert got.json()["title"] == "Lunch"
    listed = list_events(client, alice, "2026-04-01", "2026-04-01", tz="America/New_York")
    assert [e["id"] for e in listed] == [event["id"]]

    from database import SessionLocal
    from models import Event
    with SessionLocal() as db:
        assert db.get(Event, event["id"]).title == "Lunch"


def test_edit_preserves_untouched_fields(client, alice):
    event = create(client, alice, **LUNCH)["event"]
    resp = client.put(f"/events/{event['id']}", json={"title": "Team lunch"}, headers=alice)
    assert resp.status_code == 200, resp.text
    updated = client.get(f"/events/{event['id']}", headers=alice).json()
    assert updated["title"] == "Team lunch"
    for field in ("start", "end", "timezone", "start_local", "end_local"):
        assert updated[field] == event[field]

    resp = client.put(f"/events/{event['id']}", json={"end": "2026-04-01T14:00"}, headers=alice)
    assert resp.status_code == 200
    updated = client.get(f"/events/{event['id']}", headers=alice).json()
    assert updated["title"] == "Team lunch"
    assert updated["start"] == event["start"]
    assert updated["end"] == "2026-04-01T18:00:00Z"


@pytest.mark.parametrize("body", [
    {**LUNCH, "end": "2026-04-01T11:00"},               # end before start
    {**LUNCH, "end": LUNCH["start"]},                   # zero length
    {**LUNCH, "start": "not a date"},
    {**LUNCH, "timezone": "Mars/Olympus_Mons"},
    {**LUNCH, "title": "   "},
    {"title": "missing times"},
])
def test_invalid_create_is_rejected_without_writes(client, alice, body):
    resp = client.post("/events", json=body, headers=alice)
    assert resp.status_code == 422
    assert list_events(client, alice) == []


@pytest.mark.parametrize("change", [
    {"end": "2026-04-01T11:00"},
    {"start": "2026-04-01T13:30"},          # merged start after stored end
    {"start": "garbage"},
    {"timezone": "Not/AZone"},
    {"title": ""},
    {"recurrence": {"freq": "DAILY"}},       # cannot turn a single event into a series
])
def test_invalid_update_is_rejected_without_changes(client, alice, change):
    event = create(client, alice, **LUNCH)["event"]
    resp = client.put(f"/events/{event['id']}", json=change, headers=alice)
    assert resp.status_code == 422, resp.text
    assert client.get(f"/events/{event['id']}", headers=alice).json() == event


def test_delete_and_missing_events(client, alice):
    event = create(client, alice, **LUNCH)["event"]
    assert client.delete(f"/events/{event['id']}", headers=alice).status_code == 200
    assert client.get(f"/events/{event['id']}", headers=alice).status_code == 404
    assert list_events(client, alice) == []
    assert client.delete(f"/events/{event['id']}", headers=alice).status_code == 404
    resp = client.put("/events/999999", json={"title": "x"}, headers=alice)
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Event not found"


@pytest.fixture
def failing_commit(monkeypatch):
    def fail(self):
        raise OperationalError("COMMIT", {}, Exception("disk I/O error"))
    return lambda: monkeypatch.setattr(Session, "commit", fail)


def test_failed_create_reports_error_and_writes_nothing(client, alice, failing_commit, monkeypatch):
    failing_commit()
    resp = client.post("/events", json=LUNCH, headers=alice)
    assert resp.status_code == 500
    assert "event" not in resp.json()
    monkeypatch.undo()
    assert list_events(client, alice) == []


def test_failed_update_leaves_event_unchanged(client, alice, failing_commit, monkeypatch):
    event = create(client, alice, **LUNCH)["event"]
    failing_commit()
    resp = client.put(f"/events/{event['id']}",
                      json={"title": "Changed", "start": "2026-04-01T08:00"}, headers=alice)
    assert resp.status_code == 500
    monkeypatch.undo()
    assert client.get(f"/events/{event['id']}", headers=alice).json() == event


def test_failed_delete_keeps_event(client, alice, failing_commit, monkeypatch):
    event = create(client, alice, **LUNCH)["event"]
    failing_commit()
    assert client.delete(f"/events/{event['id']}", headers=alice).status_code == 500
    monkeypatch.undo()
    assert client.get(f"/events/{event['id']}", headers=alice).json() == event


def test_failed_occurrence_and_series_writes_leave_series_unchanged(client, alice, failing_commit, monkeypatch):
    series = create(client, alice, title="Standup", start="2026-05-04T09:00", end="2026-05-04T09:15",
                    recurrence={"freq": "DAILY"})["event"]
    before = list_events(client, alice, "2026-05-01", "2026-05-31")
    failing_commit()
    sid = series["id"]
    assert client.put(f"/events/{sid}/occurrence/2026-05-05", json={"start": "2026-05-05T10:00", "end": "2026-05-05T10:15"},
                      headers=alice).status_code == 500
    assert client.delete(f"/events/{sid}/occurrence/2026-05-06", headers=alice).status_code == 500
    assert client.put(f"/events/{sid}/series", json={"title": "New", "from_date": "2026-05-10"},
                      headers=alice).status_code == 500
    assert client.put(f"/events/{sid}", json={"title": "New"}, headers=alice).status_code == 500
    monkeypatch.undo()
    assert client.get(f"/events/{sid}", headers=alice).json() == series
    assert list_events(client, alice, "2026-05-01", "2026-05-31") == before

"""Zoom meetings on events. Zoom is replaced with an httpx.MockTransport."""
import json

import httpx
import pytest
from sqlalchemy import create_engine, inspect, text

import zoom
from models import Event
from test_zoom import connect, env, register  # noqa: F401  (env is a fixture)


class FakeMeetings:
    """Zoom's meeting endpoints, layered over the OAuth fake from test_zoom."""

    def __init__(self, oauth):
        self.oauth = oauth
        self.meetings = {}
        self.calls = []            # (method, path, body)
        self.status = None         # force a status code from meeting endpoints
        self.next_id = 1000

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if not (path == "/v2/users/me/meetings" or path.startswith("/v2/meetings/")):
            return self.oauth.handler(request)
        token = request.headers["Authorization"].removeprefix("Bearer ")
        if token not in self.oauth.valid_access:
            return httpx.Response(401, json={"code": 124})
        body = json.loads(request.content) if request.content else None
        self.calls.append((request.method, path, body))
        if self.status:
            return httpx.Response(self.status)

        if request.method == "POST":
            self.next_id += 1
            meeting = {**body, "id": self.next_id, "join_url": f"https://zoom.us/j/{self.next_id}"}
            self.meetings[self.next_id] = meeting
            return httpx.Response(201, json=meeting)
        meeting_id = int(path.rsplit("/", 1)[1])
        if meeting_id not in self.meetings:
            return httpx.Response(404, json={"code": 3001})
        if request.method == "PATCH":
            self.meetings[meeting_id].update(body)
            return httpx.Response(204)
        if request.method == "DELETE":
            del self.meetings[meeting_id]
            return httpx.Response(204)
        return httpx.Response(405)

    def creates(self):
        return [c for c in self.calls if c[0] == "POST"]


@pytest.fixture()
def zm(env, monkeypatch):  # noqa: F811
    client, oauth, Session = env
    fake = FakeMeetings(oauth)
    monkeypatch.setattr(zoom, "http_transport", httpx.MockTransport(fake.handler))
    alice = register(client, "alice")
    connect(client, alice)
    return client, fake, Session, alice


def create(client, headers, **overrides):
    body = {
        "title": "Design review", "start": "2026-11-02T09:00:00", "end": "2026-11-02T10:00:00",
        "timezone": "America/New_York", "create_zoom_meeting": True,
        **overrides,
    }
    res = client.post("/events", headers=headers, json=body)
    assert res.status_code == 200, res.text
    return res.json()


def week(client, headers, start="2026-11-01", end="2026-11-08"):
    res = client.get(f"/events?start={start}&end={end}&tz=America/New_York", headers=headers)
    assert res.status_code == 200
    return res.json()


def test_create_with_zoom_saves_join_link(zm):
    client, fake, Session, alice = zm
    result = create(client, alice)

    assert result["zoom_error"] is None
    meeting = next(iter(fake.meetings.values()))
    assert result["event"]["zoom_join_url"] == meeting["join_url"]
    assert meeting["topic"] == "Design review"
    assert meeting["type"] == 2
    assert meeting["start_time"] == "2026-11-02T09:00:00"
    assert meeting["timezone"] == "America/New_York"
    assert meeting["duration"] == 60

    [listed] = week(client, alice)
    assert listed["zoom_join_url"] == meeting["join_url"]


def test_zoom_is_opt_in(zm):
    client, fake, _, alice = zm
    result = create(client, alice, create_zoom_meeting=False)
    assert result["event"]["zoom_join_url"] is None
    assert fake.calls == []


@pytest.mark.parametrize("status", [500, 503, 400])
def test_zoom_failure_still_saves_event(zm, status):
    """REQ-2 E1."""
    client, fake, Session, alice = zm
    fake.status = status
    result = create(client, alice)

    assert result["event"]["id"]
    assert result["event"]["zoom_join_url"] is None
    assert result["zoom_error"]["code"] == ("zoom_unavailable" if status >= 500 else "zoom_error")
    assert "Zoom link was not created" in result["zoom_error"]["message"]
    with Session() as db:
        event = db.get(Event, result["event"]["id"])
        assert event.zoom_meeting_id is None and event.zoom_join_url is None


def test_zoom_unreachable_still_saves_event(zm, monkeypatch):
    client, fake, Session, alice = zm

    def handler(request):
        if request.url.path.startswith("/v2/users/me/meetings"):
            raise httpx.ConnectError("down", request=request)
        return fake.handler(request)

    monkeypatch.setattr(zoom, "http_transport", httpx.MockTransport(handler))
    result = create(client, alice)
    assert result["zoom_error"]["code"] == "zoom_unavailable"
    assert len(week(client, alice)) == 1


def test_not_connected_reports_status_and_saves_event(zm):
    """REQ-2 E2: the API reports not_connected; the event is still saved."""
    client, fake, Session, _ = zm
    bob = register(client, "bob")
    result = create(client, bob)
    assert result["zoom_error"]["code"] == "not_connected"
    assert result["event"]["zoom_join_url"] is None
    assert len(week(client, bob)) == 1
    assert fake.calls == []


def test_revoked_connection_reports_reconnect(zm):
    client, fake, _, alice = zm
    fake.oauth.revoke_app()
    result = create(client, alice)
    assert result["zoom_error"]["code"] == "reconnect_required"
    assert client.get("/zoom/status", headers=alice).json()["status"] == "reconnect_required"


def test_edit_title_and_time_updates_meeting(zm):
    """REQ-2 E3."""
    client, fake, _, alice = zm
    event = create(client, alice)["event"]
    res = client.put(f"/events/{event['id']}", headers=alice, json={
        "title": "Design review v2", "start": "2026-11-03T14:30:00", "end": "2026-11-03T15:00:00",
    })
    assert res.status_code == 200
    assert res.json()["zoom_error"] is None
    assert res.json()["zoom_join_url"] == event["zoom_join_url"]

    meeting = next(iter(fake.meetings.values()))
    assert meeting["topic"] == "Design review v2"
    assert meeting["start_time"] == "2026-11-03T14:30:00"
    assert meeting["duration"] == 30


def test_unchanged_edit_does_not_call_zoom(zm):
    client, fake, _, alice = zm
    event = create(client, alice)["event"]
    fake.calls.clear()
    client.put(f"/events/{event['id']}", headers=alice, json={"title": "Design review"})
    assert fake.calls == []


def test_update_failure_keeps_edit(zm):
    client, fake, _, alice = zm
    event = create(client, alice)["event"]
    fake.status = 503
    res = client.put(f"/events/{event['id']}", headers=alice, json={"title": "Renamed"})
    assert res.json()["title"] == "Renamed"
    assert res.json()["zoom_error"]["code"] == "zoom_unavailable"
    assert res.json()["zoom_join_url"] == event["zoom_join_url"]


def test_toggle_on_and_off_when_editing(zm):
    client, fake, _, alice = zm
    event = create(client, alice, create_zoom_meeting=False)["event"]

    res = client.put(f"/events/{event['id']}", headers=alice, json={"create_zoom_meeting": True})
    assert res.json()["zoom_join_url"].startswith("https://zoom.us/j/")
    assert len(fake.meetings) == 1

    res = client.put(f"/events/{event['id']}", headers=alice, json={"create_zoom_meeting": False})
    assert res.json()["zoom_join_url"] is None
    assert fake.meetings == {}


def test_delete_event_deletes_meeting(zm):
    """REQ-2 E3."""
    client, fake, Session, alice = zm
    event = create(client, alice)["event"]
    res = client.delete(f"/events/{event['id']}", headers=alice)
    assert res.json() == {"status": "deleted", "zoom_error": None}
    assert fake.meetings == {}
    with Session() as db:
        assert db.get(Event, event["id"]) is None


def test_delete_event_when_meeting_already_gone(zm):
    client, fake, _, alice = zm
    event = create(client, alice)["event"]
    fake.meetings.clear()
    res = client.delete(f"/events/{event['id']}", headers=alice)
    assert res.json()["zoom_error"] is None


def test_delete_event_when_zoom_down_reports_it(zm):
    client, fake, Session, alice = zm
    event = create(client, alice)["event"]
    fake.status = 503
    res = client.delete(f"/events/{event['id']}", headers=alice)
    assert res.json()["status"] == "deleted"
    assert res.json()["zoom_error"]["code"] == "zoom_unavailable"
    with Session() as db:
        assert db.get(Event, event["id"]) is None


@pytest.mark.parametrize("start,end,start_time,duration", [
    # Crosses midnight.
    ("2026-11-02T23:30:00", "2026-11-03T00:45:00", "2026-11-02T23:30:00", 75),
    # Crosses the fall-back change (Nov 1 2026, 2am -> 1am): 3 wall hours, 4 real hours.
    ("2026-10-31T23:00:00", "2026-11-01T02:00:00", "2026-10-31T23:00:00", 240),
    # Crosses the spring-forward change (Mar 8 2026, 2am -> 3am): 3 wall hours, 2 real hours.
    ("2026-03-08T00:00:00", "2026-03-08T03:00:00", "2026-03-08T00:00:00", 120),
])
def test_meeting_time_matches_event_across_midnight_and_dst(zm, start, end, start_time, duration):
    """REQ-2 E4."""
    client, fake, _, alice = zm
    create(client, alice, start=start, end=end)
    meeting = next(iter(fake.meetings.values()))
    assert meeting["start_time"] == start_time
    assert meeting["timezone"] == "America/New_York"
    assert meeting["duration"] == duration


def test_same_idempotency_key_creates_one_event_and_meeting(zm):
    """REQ-2 E5."""
    client, fake, Session, alice = zm
    first = create(client, alice, idempotency_key="key-1")
    second = create(client, alice, idempotency_key="key-1")
    assert second["event"]["id"] == first["event"]["id"]
    assert second["event"]["zoom_join_url"] == first["event"]["zoom_join_url"]
    assert len(fake.creates()) == 1
    with Session() as db:
        assert db.query(Event).count() == 1


def test_retry_after_zoom_failure_creates_meeting_once(zm):
    client, fake, Session, alice = zm
    fake.status = 503
    first = create(client, alice, idempotency_key="key-2")
    assert first["zoom_error"]["code"] == "zoom_unavailable"
    fake.status = None
    retry = create(client, alice, idempotency_key="key-2")
    assert retry["event"]["id"] == first["event"]["id"]
    assert retry["event"]["zoom_join_url"]
    create(client, alice, idempotency_key="key-2")
    assert len(fake.meetings) == 1


def test_meeting_in_flight_is_not_created_twice(zm):
    """A second request that arrives while Zoom is still creating the meeting is a no-op."""
    client, fake, Session, alice = zm
    event = create(client, alice, create_zoom_meeting=False, idempotency_key="key-3")["event"]
    with Session() as db:
        db.get(Event, event["id"]).zoom_meeting_id = "pending"
        db.commit()
    retry = create(client, alice, idempotency_key="key-3")
    assert retry["zoom_error"] is None
    assert retry["event"]["zoom_join_url"] is None
    assert fake.creates() == []


def test_idempotency_keys_are_per_user(zm):
    client, _, _, alice = zm
    bob = register(client, "bob")
    a = create(client, alice, idempotency_key="shared", create_zoom_meeting=False)
    b = create(client, bob, idempotency_key="shared", create_zoom_meeting=False)
    assert a["event"]["id"] != b["event"]["id"]


def test_events_are_scoped_to_their_owner(zm):
    client, fake, Session, alice = zm
    bob = register(client, "bob")
    event = create(client, alice)["event"]

    assert week(client, bob) == []
    assert client.put(f"/events/{event['id']}", headers=bob, json={"title": "x"}).status_code == 404
    assert client.delete(f"/events/{event['id']}", headers=bob).status_code == 404
    assert client.put(f"/events/{event['id']}/series", headers=bob,
                      json={"from_date": "2026-11-02"}).status_code == 404
    assert len(fake.meetings) == 1
    assert week(client, alice)[0]["title"] == "Design review"


def test_conflicts_only_consider_own_events(zm):
    client, _, _, alice = zm
    bob = register(client, "bob")
    create(client, alice, create_zoom_meeting=False)
    assert create(client, bob, create_zoom_meeting=False)["conflicts"] == []
    assert len(create(client, alice, create_zoom_meeting=False)["conflicts"]) == 1


def test_recurring_series_shares_one_meeting(zm):
    client, fake, _, alice = zm
    result = create(client, alice, title="Standup", start="2026-11-02T09:00:00",
                    end="2026-11-02T09:15:00", recurrence={"freq": "DAILY", "count": 5})
    meeting = next(iter(fake.meetings.values()))
    assert meeting["type"] == 3
    assert meeting["topic"] == "Standup"
    occurrences = week(client, alice)
    assert len(occurrences) == 5
    assert {o["zoom_join_url"] for o in occurrences} == {result["event"]["zoom_join_url"]}

    # Editing one occurrence leaves the shared meeting alone.
    fake.calls.clear()
    res = client.put(f"/events/{result['event']['id']}/occurrence/2026-11-03", headers=alice,
                     json={"title": "Standup (moved)", "start": "2026-11-03T10:00:00"})
    assert res.status_code == 200
    assert fake.calls == []

    # Renaming the whole series renames the meeting.
    client.put(f"/events/{result['event']['id']}", headers=alice, json={"title": "Daily sync"})
    assert meeting["topic"] == "Daily sync"


def test_series_split_gives_new_series_its_own_meeting(zm):
    client, fake, _, alice = zm
    series = create(client, alice, title="Standup", start="2026-11-02T09:00:00",
                    end="2026-11-02T09:15:00", recurrence={"freq": "DAILY", "count": 5})["event"]
    res = client.put(f"/events/{series['id']}/series", headers=alice,
                     json={"from_date": "2026-11-04", "title": "Standup v2"})
    body = res.json()
    assert body["id"] != series["id"]
    assert body["zoom_error"] is None
    assert body["zoom_join_url"] and body["zoom_join_url"] != series["zoom_join_url"]
    assert sorted(m["topic"] for m in fake.meetings.values()) == ["Standup", "Standup v2"]

    # Deleting the new series removes only its meeting.
    client.delete(f"/events/{body['id']}", headers=alice)
    assert [m["topic"] for m in fake.meetings.values()] == ["Standup"]


def test_series_split_can_skip_meeting(zm):
    client, fake, _, alice = zm
    series = create(client, alice, title="Standup", start="2026-11-02T09:00:00",
                    end="2026-11-02T09:15:00", recurrence={"freq": "DAILY", "count": 5})["event"]
    body = client.put(f"/events/{series['id']}/series", headers=alice,
                      json={"from_date": "2026-11-04", "create_zoom_meeting": False}).json()
    assert body["zoom_join_url"] is None
    assert len(fake.meetings) == 1


def test_series_split_at_first_occurrence_updates_meeting(zm):
    client, fake, _, alice = zm
    series = create(client, alice, title="Standup", start="2026-11-02T09:00:00",
                    end="2026-11-02T09:15:00", recurrence={"freq": "DAILY", "count": 5})["event"]
    body = client.put(f"/events/{series['id']}/series", headers=alice,
                      json={"from_date": "2026-11-02", "title": "Renamed"}).json()
    assert body["id"] == series["id"]
    assert body["zoom_join_url"] == series["zoom_join_url"]
    assert [m["topic"] for m in fake.meetings.values()] == ["Renamed"]


def test_disconnect_keeps_saved_links(zm):
    """REQ-1 E3 still holds with meetings attached."""
    client, _, _, alice = zm
    event = create(client, alice)["event"]
    client.delete("/zoom/connection", headers=alice)
    assert week(client, alice)[0]["zoom_join_url"] == event["zoom_join_url"]


def test_upgrade_adds_columns_to_existing_database(tmp_path):
    from migrations import upgrade

    engine = create_engine(f"sqlite:///{tmp_path / 'old.db'}")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE users (id INTEGER PRIMARY KEY)"))
        conn.execute(text(
            "CREATE TABLE events (id INTEGER PRIMARY KEY, title VARCHAR NOT NULL, "
            "start_utc DATETIME NOT NULL, end_utc DATETIME NOT NULL, timezone VARCHAR NOT NULL, "
            "is_recurring BOOLEAN NOT NULL)"
        ))
        conn.execute(text(
            "INSERT INTO events (title, start_utc, end_utc, timezone, is_recurring) "
            "VALUES ('Old', '2026-01-01 10:00:00', '2026-01-01 11:00:00', 'UTC', 0)"
        ))
    upgrade(engine)
    upgrade(engine)  # idempotent

    columns = {c["name"] for c in inspect(engine).get_columns("events")}
    assert {"owner_user_id", "idempotency_key", "zoom_meeting_id", "zoom_join_url"} <= columns
    with engine.connect() as conn:
        assert conn.execute(text("SELECT title FROM events")).scalar() == "Old"

"""Each person sees, creates, edits, and deletes only their own events."""
from conftest import register

from models import Event, User

RANGE = {"start": "2026-06-01", "end": "2026-06-30", "tz": "UTC"}
SINGLE = {"title": "Alice lunch", "start": "2026-06-10T12:00:00", "end": "2026-06-10T13:00:00"}
WEEKLY = {
    "title": "Alice standup",
    "start": "2026-06-01T09:00:00",
    "end": "2026-06-01T09:30:00",
    "recurrence": {"freq": "WEEKLY", "byday": "MO"},
}


def create(client, headers, body):
    res = client.post("/events", json=body, headers=headers)
    assert res.status_code == 200, res.text
    return res.json()["event"]["id"]


def list_events(client, headers):
    res = client.get("/events", params=RANGE, headers=headers)
    assert res.status_code == 200, res.text
    return res.json()


def test_new_account_sees_empty_calendar(client, alice):
    create(client, alice, SINGLE)
    create(client, alice, WEEKLY)
    carol = register(client, "carol")
    assert list_events(client, carol) == []


def test_list_returns_only_own_events(client, alice, bob):
    create(client, alice, SINGLE)
    create(client, alice, WEEKLY)
    bob_id = create(client, bob, {**SINGLE, "title": "Bob lunch"})

    assert {e["title"] for e in list_events(client, alice)} == {"Alice lunch", "Alice standup"}
    bob_events = list_events(client, bob)
    assert [(e["id"], e["title"]) for e in bob_events] == [(bob_id, "Bob lunch")]


def test_other_persons_event_is_not_found_on_every_by_id_route(client, alice, bob, session_factory):
    single_id = create(client, alice, SINGLE)
    series_id = create(client, alice, WEEKLY)
    attempts = [
        ("put", f"/events/{single_id}", {"title": "hijacked"}),
        ("delete", f"/events/{single_id}", None),
        ("put", f"/events/{series_id}", {"title": "hijacked"}),
        ("put", f"/events/{series_id}/occurrence/2026-06-08", {"title": "hijacked"}),
        ("delete", f"/events/{series_id}/occurrence/2026-06-08", None),
        ("put", f"/events/{series_id}/series", {"title": "hijacked", "from_date": "2026-06-15"}),
        ("delete", f"/events/{series_id}", None),
    ]
    for method, url, body in attempts:
        kwargs = {"json": body} if body is not None else {}
        res = client.request(method, url, headers=bob, **kwargs)
        assert res.status_code == 404, (method, url, res.text)
        assert res.json() == {"detail": "Event not found"}

    # Same response as an ID that does not exist at all
    assert client.put("/events/99999", json={"title": "x"}, headers=bob).json() == {
        "detail": "Event not found"
    }

    # Alice's events are untouched
    events = list_events(client, alice)
    assert {e["title"] for e in events} == {"Alice lunch", "Alice standup"}
    assert not any(e["is_exception"] for e in events)
    with session_factory() as db:
        assert db.query(Event).count() == 2
        assert all(not ev.exceptions for ev in db.query(Event).all())


def test_owner_can_edit_and_delete_own_events(client, alice):
    single_id = create(client, alice, SINGLE)
    series_id = create(client, alice, WEEKLY)

    assert client.put(f"/events/{single_id}", json={"title": "Renamed"}, headers=alice).status_code == 200
    assert client.put(
        f"/events/{series_id}/occurrence/2026-06-08", json={"title": "Moved"}, headers=alice
    ).status_code == 200
    assert client.delete(f"/events/{series_id}/occurrence/2026-06-15", headers=alice).status_code == 200
    titles = [e["title"] for e in list_events(client, alice)]
    assert "Renamed" in titles and "Moved" in titles
    assert len([t for t in titles if t == "Alice standup"]) == 3  # 1, 22, 29 June

    assert client.delete(f"/events/{single_id}", headers=alice).status_code == 200
    assert client.delete(f"/events/{series_id}", headers=alice).status_code == 200
    assert list_events(client, alice) == []


def test_supplied_owner_is_ignored(client, alice, bob, session_factory):
    with session_factory() as db:
        bob_id = db.query(User).filter_by(username="bob").one().id

    event_id = create(client, alice, {**SINGLE, "owner_id": bob_id, "owner": bob_id})
    res = client.put(f"/events/{event_id}", json={"title": "Still Alice's", "owner_id": bob_id},
                     headers=alice)
    assert res.status_code == 200

    assert list_events(client, bob) == []
    assert [e["title"] for e in list_events(client, alice)] == ["Still Alice's"]
    with session_factory() as db:
        assert db.get(Event, event_id).owner.username == "alice"


def test_series_split_keeps_owner(client, alice, bob, session_factory):
    series_id = create(client, alice, WEEKLY)
    res = client.put(f"/events/{series_id}/series",
                     json={"title": "Later standup", "from_date": "2026-06-15"}, headers=alice)
    assert res.status_code == 200, res.text
    new_id = res.json()["id"]
    assert new_id != series_id

    with session_factory() as db:
        assert db.get(Event, new_id).owner_id == db.get(Event, series_id).owner_id
    assert list_events(client, bob) == []
    assert "Later standup" in {e["title"] for e in list_events(client, alice)}


def test_conflicts_only_consider_own_events(client, alice, bob):
    create(client, bob, {**SINGLE, "title": "Bob lunch"})
    res = client.post("/events", json=SINGLE, headers=alice)
    assert res.json()["conflicts"] == []

    res = client.post("/events", json={**SINGLE, "title": "Double booked"}, headers=alice)
    assert [c["title"] for c in res.json()["conflicts"]] == ["Alice lunch"]


def test_events_require_a_token(client, alice):
    create(client, alice, SINGLE)
    assert client.get("/events", params=RANGE).status_code == 401
    assert client.get("/events", params=RANGE,
                      headers={"Authorization": "Bearer not-a-token"}).status_code == 401

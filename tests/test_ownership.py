"""REQ-3: every operation is limited to the authenticated owner."""
import sqlite3

from sqlalchemy import create_engine

from conftest import create, list_events, register

SINGLE = {"title": "Alice private", "start": "2026-06-01T10:00", "end": "2026-06-01T11:00"}
SERIES = {"title": "Alice series", "start": "2026-06-01T09:00", "end": "2026-06-01T09:30",
          "recurrence": {"freq": "DAILY"}}


def test_other_users_events_are_invisible_and_immutable(client, alice, bob):
    single = create(client, alice, **SINGLE)["event"]
    series = create(client, alice, **SERIES)["event"]
    missing = client.get("/events/999999", headers=bob).json()

    for event in (single, series):
        eid = event["id"]
        for resp in (
            client.get(f"/events/{eid}", headers=bob),
            client.put(f"/events/{eid}", json={"title": "pwned"}, headers=bob),
            client.delete(f"/events/{eid}", headers=bob),
        ):
            assert resp.status_code == 404
            assert resp.json() == missing  # indistinguishable from a missing id
            assert "Alice" not in resp.text

    sid = series["id"]
    for resp in (
        client.put(f"/events/{sid}/occurrence/2026-06-02", json={"title": "pwned"}, headers=bob),
        client.delete(f"/events/{sid}/occurrence/2026-06-02", headers=bob),
        client.put(f"/events/{sid}/series", json={"title": "pwned"}, headers=bob),
        client.put(f"/events/{sid}/series", json={"title": "pwned", "from_date": "2026-06-05"}, headers=bob),
    ):
        assert resp.status_code == 404
        assert "Alice" not in resp.text

    assert list_events(client, bob) == []
    assert client.get(f"/events/{single['id']}", headers=alice).json() == single
    assert client.get(f"/events/{sid}", headers=alice).json() == series
    assert len(list_events(client, alice, "2026-06-01", "2026-06-07")) == 8


def test_client_ownership_fields_are_ignored(client, alice, bob):
    from database import SessionLocal
    from models import Event, User

    with SessionLocal() as db:
        alice_id = db.query(User).filter_by(username="alice").one().id
        bob_id = db.query(User).filter_by(username="bob").one().id

    event = create(client, bob, owner_id=alice_id, user_id=alice_id, **SINGLE)["event"]
    assert list_events(client, alice) == []
    assert client.get(f"/events/{event['id']}", headers=alice).status_code == 404

    resp = client.put(f"/events/{event['id']}", json={"owner_id": alice_id, "title": "still bob's"}, headers=bob)
    assert resp.status_code == 200
    assert client.get(f"/events/{event['id']}", headers=alice).status_code == 404
    with SessionLocal() as db:
        assert db.get(Event, event["id"]).owner_id == bob_id


def test_split_series_is_owned_by_the_caller(client, alice, bob):
    series = create(client, alice, **SERIES)["event"]
    resp = client.put(f"/events/{series['id']}/series",
                      json={"title": "Later", "from_date": "2026-06-10", "owner_id": 999}, headers=alice)
    assert resp.status_code == 200, resp.text
    new_id = resp.json()["event"]["id"]
    assert new_id != series["id"]
    assert client.get(f"/events/{new_id}", headers=alice).status_code == 200
    assert client.get(f"/events/{new_id}", headers=bob).status_code == 404
    assert list_events(client, bob) == []


def test_conflicts_never_include_other_users(client, alice, bob):
    create(client, alice, **SINGLE)
    create(client, alice, **SERIES)
    saved = create(client, bob, title="Bob", start="2026-06-01T08:00", end="2026-06-01T12:00")
    assert saved["conflicts"] == []
    assert "Alice" not in str(saved)

    # Bob's own overlap is still reported; Alice's are not.
    saved = create(client, bob, title="Bob 2", start="2026-06-01T09:00", end="2026-06-01T10:30")
    assert [c["title"] for c in saved["conflicts"]] == ["Bob"]

    bob_series = create(client, bob, title="Bob daily", start="2026-06-02T09:00", end="2026-06-02T10:00",
                        recurrence={"freq": "DAILY", "until": "2026-06-30"})
    assert bob_series["conflicts"] == []


def test_migration_isolates_unowned_legacy_events(tmp_path):
    from migrations import assign_unowned_events, run_migrations
    from models import User
    from sqlalchemy.orm import sessionmaker
    from occurrences import owned_occurrences
    import datetime as dt

    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE events (id INTEGER PRIMARY KEY, title VARCHAR NOT NULL, start_utc DATETIME NOT NULL,
            end_utc DATETIME NOT NULL, timezone VARCHAR NOT NULL, is_recurring BOOLEAN NOT NULL,
            created_at DATETIME, updated_at DATETIME);
        INSERT INTO events (title, start_utc, end_utc, timezone, is_recurring)
            VALUES ('Legacy sample', '2026-04-01 16:00:00.000000', '2026-04-01 17:00:00.000000', 'UTC', 0);
    """)
    conn.commit()
    conn.close()

    engine = create_engine(f"sqlite:///{path}")
    run_migrations(engine)
    run_migrations(engine)  # idempotent
    Session = sessionmaker(bind=engine)
    window = (dt.datetime(2026, 1, 1), dt.datetime(2027, 1, 1))
    with Session() as db:
        user = User(username="carol", password_hash="x")
        db.add(user)
        db.commit()
        assert owned_occurrences(db, user.id, *window) == []
        assert assign_unowned_events(db, "carol") == 1
        assert [o.title for o in owned_occurrences(db, user.id, *window)] == ["Legacy sample"]
    engine.dispose()


def test_startup_does_not_seed_sample_events(client):
    from database import SessionLocal
    from models import Event
    with SessionLocal() as db:
        assert db.query(Event).count() == 0
    headers = register(client, "dave")
    assert list_events(client, headers) == []

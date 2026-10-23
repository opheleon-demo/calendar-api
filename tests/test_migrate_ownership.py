"""Ownership migration for events created before sign-in existed."""
import datetime as dt

import pytest
from sqlalchemy import text

import migrate_ownership as mig
from auth import get_password_hash

# Schema of a database created before ownership existed.
LEGACY_SCHEMA = [
    """CREATE TABLE users (
        id INTEGER PRIMARY KEY AUTOINCREMENT, username VARCHAR NOT NULL UNIQUE,
        password_hash VARCHAR NOT NULL, created_at DATETIME, updated_at DATETIME)""",
    """CREATE TABLE events (
        id INTEGER PRIMARY KEY AUTOINCREMENT, title VARCHAR NOT NULL,
        start_utc DATETIME NOT NULL, end_utc DATETIME NOT NULL, timezone VARCHAR NOT NULL,
        is_recurring BOOLEAN NOT NULL,
        created_at DATETIME DEFAULT (CURRENT_TIMESTAMP), updated_at DATETIME DEFAULT (CURRENT_TIMESTAMP))""",
    """CREATE TABLE recurrence_rules (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        event_id INTEGER NOT NULL UNIQUE REFERENCES events(id) ON DELETE CASCADE,
        freq VARCHAR NOT NULL, interval INTEGER NOT NULL, count INTEGER, until_utc DATETIME,
        byday VARCHAR, bymonthday VARCHAR, bymonth VARCHAR)""",
    """CREATE TABLE recurrence_exceptions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
        original_date DATE NOT NULL, is_deleted BOOLEAN NOT NULL, title VARCHAR,
        start_utc DATETIME, end_utc DATETIME, UNIQUE (event_id, original_date))""",
]


@pytest.fixture
def legacy_engine(engine):
    with engine.begin() as conn:
        for ddl in LEGACY_SCHEMA:
            conn.exec_driver_sql(ddl)
        for title, recurring in [("Team Lunch", False), ("Daily Standup", True), ("Dentist", False)]:
            conn.execute(
                text("INSERT INTO events (title, start_utc, end_utc, timezone, is_recurring) "
                     "VALUES (:t, :s, :e, 'UTC', :r)"),
                {"t": title, "s": dt.datetime(2026, 6, 1, 9), "e": dt.datetime(2026, 6, 1, 10),
                 "r": recurring},
            )
        conn.exec_driver_sql(
            "INSERT INTO recurrence_rules (event_id, freq, interval, byday) VALUES (2, 'WEEKLY', 1, 'MO')"
        )
        conn.exec_driver_sql(
            "INSERT INTO recurrence_exceptions (event_id, original_date, is_deleted) "
            "VALUES (2, '2026-06-08', 1)"
        )
    return engine


def add_user(engine, username):
    with engine.begin() as conn:
        return conn.execute(
            text("INSERT INTO users (username, password_hash) VALUES (:u, :p) RETURNING id"),
            {"u": username, "p": get_password_hash("password123")},
        ).scalar()


def snapshot(engine):
    with engine.connect() as conn:
        tables = {
            t: conn.exec_driver_sql(f"SELECT * FROM {t} ORDER BY id").fetchall()
            for t in ("events", "recurrence_rules", "recurrence_exceptions")
        }
        tables["schema"] = conn.exec_driver_sql(
            "SELECT sql FROM sqlite_master WHERE name = 'events'"
        ).scalar()
    return tables


def owners(engine):
    with engine.connect() as conn:
        return dict(conn.exec_driver_sql("SELECT id, owner_id FROM events ORDER BY id").fetchall())


def owner_column(engine):
    with engine.connect() as conn:
        return mig._owner_column(conn)


def test_assigns_all_ownerless_events_and_makes_owner_required(legacy_engine):
    alice_id = add_user(legacy_engine, "alice")

    assert mig.migrate_ownership(legacy_engine, "alice") == 3

    assert owners(legacy_engine) == {1: alice_id, 2: alice_id, 3: alice_id}
    assert owner_column(legacy_engine)["notnull"] == 1
    with legacy_engine.connect() as conn:
        # Recurrence data survived the table rebuild
        assert conn.exec_driver_sql("SELECT event_id FROM recurrence_rules").scalar() == 2
        assert conn.exec_driver_sql("SELECT event_id FROM recurrence_exceptions").scalar() == 2
        assert conn.exec_driver_sql("PRAGMA foreign_key_check").fetchall() == []
        indexes = {r[1] for r in conn.exec_driver_sql("PRAGMA index_list(events)")}
        assert "ix_events_owner_id" in indexes
        with pytest.raises(Exception):
            conn.exec_driver_sql(
                "INSERT INTO events (title, start_utc, end_utc, timezone, is_recurring) "
                "VALUES ('orphan', '2026-01-01', '2026-01-01', 'UTC', 0)"
            )


def test_missing_account_fails_and_leaves_database_unchanged(legacy_engine):
    add_user(legacy_engine, "alice")
    before = snapshot(legacy_engine)

    with pytest.raises(mig.OwnershipMigrationError, match="'nobody' does not exist"):
        mig.migrate_ownership(legacy_engine, "nobody")

    assert snapshot(legacy_engine) == before
    assert owner_column(legacy_engine) is None


def test_missing_account_after_column_exists_leaves_events_unassigned(legacy_engine):
    mig.ensure_owner_column(legacy_engine)
    before = snapshot(legacy_engine)

    with pytest.raises(mig.OwnershipMigrationError):
        mig.migrate_ownership(legacy_engine, "nobody")

    assert snapshot(legacy_engine) == before
    assert set(owners(legacy_engine).values()) == {None}


def test_failure_mid_migration_rolls_back(legacy_engine, monkeypatch):
    add_user(legacy_engine, "alice")
    before = snapshot(legacy_engine)

    def boom(conn):
        raise mig.OwnershipMigrationError("simulated failure")

    monkeypatch.setattr(mig, "_rebuild_with_not_null_owner", boom)
    with pytest.raises(mig.OwnershipMigrationError, match="simulated"):
        mig.migrate_ownership(legacy_engine, "alice")

    assert snapshot(legacy_engine) == before


def test_rerun_does_not_reassign_other_peoples_events(legacy_engine):
    alice_id = add_user(legacy_engine, "alice")
    bob_id = add_user(legacy_engine, "bob")
    mig.migrate_ownership(legacy_engine, "alice")
    with legacy_engine.begin() as conn:
        conn.execute(
            text("INSERT INTO events (owner_id, title, start_utc, end_utc, timezone, is_recurring) "
                 "VALUES (:o, 'Bob event', '2026-07-01', '2026-07-01', 'UTC', 0)"),
            {"o": bob_id},
        )

    assert mig.migrate_ownership(legacy_engine, "alice") == 0
    assert mig.migrate_ownership(legacy_engine, "bob") == 0
    assert owners(legacy_engine) == {1: alice_id, 2: alice_id, 3: alice_id, 4: bob_id}


def test_blank_owner_setting_is_rejected(legacy_engine):
    with pytest.raises(mig.OwnershipMigrationError, match="LEGACY_EVENTS_OWNER"):
        mig.migrate_ownership(legacy_engine, "  ")


def test_fresh_schema_migration_is_a_no_op(session_factory, engine):
    add_user(engine, "alice")
    assert mig.migrate_ownership(engine, "alice") == 0
    assert owner_column(engine)["notnull"] == 1


def test_migrated_events_visible_only_to_named_account(legacy_engine, client):
    # `client` shares the same database file as `legacy_engine`
    from conftest import register

    mig.ensure_owner_column(legacy_engine)  # what API startup does before serving
    alice = register(client, "alice")
    bob = register(client, "bob")
    params = {"start": "2026-06-01", "end": "2026-06-07", "tz": "UTC"}
    assert client.get("/events", params=params, headers=alice).json() == []  # hidden until migrated

    mig.migrate_ownership(legacy_engine, "alice")

    titles = {e["title"] for e in client.get("/events", params=params, headers=alice).json()}
    assert titles == {"Team Lunch", "Daily Standup", "Dentist"}
    assert client.get("/events", params=params, headers=bob).json() == []

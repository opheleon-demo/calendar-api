"""
Additive schema upgrades for existing databases.

`Base.metadata.create_all` creates missing tables but never alters existing
ones, so columns added to an existing table are added here. Every step is
idempotent and runs on startup.
"""
from __future__ import annotations

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

from models import Event


# (column name, SQL type) for columns added to `events` after its first release.
EVENT_COLUMNS = [
    ("owner_user_id", "INTEGER REFERENCES users(id) ON DELETE CASCADE"),
    ("idempotency_key", "VARCHAR"),
    ("zoom_meeting_id", "VARCHAR"),
    ("zoom_join_url", "VARCHAR"),
]


def upgrade(engine: Engine) -> None:
    existing = {col["name"] for col in inspect(engine).get_columns(Event.__tablename__)}
    with engine.begin() as conn:
        for name, sql_type in EVENT_COLUMNS:
            if name not in existing:
                conn.execute(text(f"ALTER TABLE events ADD COLUMN {name} {sql_type}"))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_events_owner_user_id ON events (owner_user_id)"
        ))
        conn.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS ix_events_owner_idempotency_key "
            "ON events (owner_user_id, idempotency_key)"
        ))

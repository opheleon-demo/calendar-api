"""
Event ownership migration.

Events created before sign-in existed have no owner. This migration assigns
every ownerless event to one named account and then makes events.owner_id
NOT NULL, all in a single transaction:

1. Look up the receiving account. If it does not exist, stop with a clear
   error and leave the database untouched.
2. Add events.owner_id if the table predates it.
3. Assign only events whose owner_id is NULL, so rerunning after launch never
   reassigns events other people have created.
4. Rebuild the events table so owner_id is NOT NULL (SQLite cannot alter a
   column's nullability in place).

The receiving account's username comes from the LEGACY_EVENTS_OWNER setting
or the --owner flag:

    LEGACY_EVENTS_OWNER=alice python3 migrate_ownership.py
    python3 migrate_ownership.py --owner alice
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from contextlib import contextmanager
from typing import Iterator, Optional

from sqlalchemy import Connection, Engine, MetaData, text
from sqlalchemy.schema import CreateTable

from models import Event, User


LEGACY_EVENTS_OWNER_ENV = "LEGACY_EVENTS_OWNER"
_REBUILD_TABLE = "events__ownership_migration"

logger = logging.getLogger(__name__)


class OwnershipMigrationError(RuntimeError):
    pass


@contextmanager
def _transaction(engine: Engine) -> Iterator[Connection]:
    """
    One explicit SQLite transaction covering DDL and DML alike.

    pysqlite only opens transactions implicitly before INSERT/UPDATE/DELETE,
    so ALTER/CREATE/DROP would otherwise run outside of it.
    """
    with engine.connect() as conn:
        # Must be set outside a transaction; keeps the table rebuild from
        # cascading into recurrence_rules / recurrence_exceptions.
        conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
        conn.exec_driver_sql("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            conn.rollback()
            raise
        conn.commit()


def _owner_column(conn: Connection) -> Optional[dict]:
    for row in conn.exec_driver_sql("PRAGMA table_info(events)").mappings():
        if row["name"] == "owner_id":
            return dict(row)
    return None


def _add_owner_column(conn: Connection) -> None:
    if _owner_column(conn) is None:
        conn.exec_driver_sql("ALTER TABLE events ADD COLUMN owner_id INTEGER REFERENCES users(id)")
    for index in Event.__table__.indexes:
        index.create(conn, checkfirst=True)


def ensure_owner_column(engine: Engine) -> None:
    """
    Add a nullable events.owner_id to a database that predates ownership, so
    the API can run before the migration. Ownerless events stay invisible to
    everyone until migrate_ownership assigns them.
    """
    with _transaction(engine) as conn:
        _add_owner_column(conn)


def _rebuild_with_not_null_owner(conn: Connection) -> None:
    """SQLite's documented table-rebuild procedure, using the current Event schema."""
    metadata = MetaData()
    User.__table__.to_metadata(metadata)  # lets the users.id foreign key resolve
    new_table = Event.__table__.to_metadata(metadata, name=_REBUILD_TABLE)
    columns = ", ".join(c.name for c in Event.__table__.columns)

    conn.execute(CreateTable(new_table))
    conn.exec_driver_sql(f"INSERT INTO {_REBUILD_TABLE} ({columns}) SELECT {columns} FROM events")
    conn.exec_driver_sql("DROP TABLE events")
    conn.exec_driver_sql(f"ALTER TABLE {_REBUILD_TABLE} RENAME TO events")
    for index in Event.__table__.indexes:
        index.create(conn, checkfirst=True)

    violations = conn.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
    if violations:
        raise OwnershipMigrationError(f"Foreign key check failed after rebuild: {violations}")


def migrate_ownership(engine: Engine, owner_username: Optional[str]) -> int:
    """Assign every ownerless event to `owner_username`. Returns the number assigned."""
    username = (owner_username or "").strip()
    if not username:
        raise OwnershipMigrationError(
            f"No receiving account given; set {LEGACY_EVENTS_OWNER_ENV} or pass --owner"
        )

    with _transaction(engine) as conn:
        owner_id = conn.execute(
            text("SELECT id FROM users WHERE username = :username"), {"username": username}
        ).scalar()
        if owner_id is None:
            raise OwnershipMigrationError(
                f"Account '{username}' does not exist; register it before running the "
                "ownership migration. No events were changed."
            )

        _add_owner_column(conn)
        assigned = conn.execute(
            text("UPDATE events SET owner_id = :owner_id WHERE owner_id IS NULL"),
            {"owner_id": owner_id},
        ).rowcount

        if not _owner_column(conn)["notnull"]:
            _rebuild_with_not_null_owner(conn)

        remaining = conn.exec_driver_sql("SELECT COUNT(*) FROM events WHERE owner_id IS NULL").scalar()
        if remaining:
            raise OwnershipMigrationError(f"{remaining} events still have no owner")

    logger.info("Assigned %d ownerless events to account '%s'", assigned, username)
    return assigned


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    parser.add_argument(
        "--owner",
        default=os.getenv(LEGACY_EVENTS_OWNER_ENV),
        help=f"Username receiving ownerless events (default: ${LEGACY_EVENTS_OWNER_ENV})",
    )
    args = parser.parse_args(argv)

    from database import engine
    from models import Base

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    Base.metadata.create_all(bind=engine)
    try:
        migrate_ownership(engine, args.owner)
    except OwnershipMigrationError as exc:
        logger.error("Ownership migration failed: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""
Schema migrations for databases created before event ownership existed.

`run_migrations` adds the `events.owner_id` column when it is missing. Rows
that existed before ownership (including old auto-seeded sample events) keep a
NULL owner. Every API query filters on the caller's id, so those rows are
isolated: no account can list, read, change or conflict with them.

To hand legacy rows to a specific account explicitly:

    python migrations.py --assign-unowned-to USERNAME
"""
from __future__ import annotations
import argparse
import logging

from sqlalchemy import inspect, text, update
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from models import Base, Event, User

logger = logging.getLogger(__name__)


def run_migrations(engine: Engine) -> None:
    Base.metadata.create_all(bind=engine)
    columns = {c["name"] for c in inspect(engine).get_columns("events")}
    with engine.begin() as conn:
        if "owner_id" not in columns:
            conn.execute(text("ALTER TABLE events ADD COLUMN owner_id INTEGER REFERENCES users(id)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_events_owner_id ON events (owner_id)"))
        unowned = conn.execute(text("SELECT COUNT(*) FROM events WHERE owner_id IS NULL")).scalar_one()
    if unowned:
        logger.warning(
            "%d event(s) have no owner and are hidden from every account; "
            "assign them with `python migrations.py --assign-unowned-to USERNAME`",
            unowned,
        )


def assign_unowned_events(db: Session, username: str) -> int:
    """Explicitly give every unowned event to `username`. Returns the count."""
    user = db.query(User).filter(User.username == username).first()
    if user is None:
        raise ValueError(f"No user named {username!r}")
    result = db.execute(update(Event).where(Event.owner_id.is_(None)).values(owner_id=user.id))
    db.commit()
    return result.rowcount


if __name__ == "__main__":
    from database import SessionLocal, engine

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--assign-unowned-to", metavar="USERNAME")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    run_migrations(engine)
    if args.assign_unowned_to:
        with SessionLocal() as session:
            n = assign_unowned_events(session, args.assign_unowned_to)
        print(f"Assigned {n} unowned event(s) to {args.assign_unowned_to}")

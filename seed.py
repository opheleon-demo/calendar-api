"""
Seed data: a few single events and 2-3 recurring events with exceptions.

Demonstrates DST crossings (standup crosses March spring-forward),
exception modifications, and exception deletions.
"""
import datetime as dt
from sqlalchemy.orm import Session

from models import Event, RecurrenceRule, RecurrenceException
from timezone import local_to_utc


def seed_database(db: Session) -> None:
    # ── 1. Single event: Team Lunch ─────────────────────────────────────
    lunch_start = local_to_utc(dt.datetime(2026, 4, 1, 12, 0), "America/New_York").utc_dt
    lunch = Event(
        title="Team Lunch",
        start_utc=lunch_start,
        end_utc=lunch_start + dt.timedelta(hours=1),
        timezone="America/New_York",
        is_recurring=False,
    )
    db.add(lunch)

    # ── 2. Single event: Dentist ────────────────────────────────────────
    dentist_start = local_to_utc(dt.datetime(2026, 4, 3, 10, 0), "America/New_York").utc_dt
    dentist = Event(
        title="Dentist Appointment",
        start_utc=dentist_start,
        end_utc=dentist_start + dt.timedelta(hours=1, minutes=30),
        timezone="America/New_York",
        is_recurring=False,
    )
    db.add(dentist)

    # ── 3. Recurring: Daily Standup (MO/WE/FR at 9am ET) ───────────────
    # Starts Jan 5, 2026 — crosses DST spring-forward on March 8, 2026.
    # 9am EST = 14:00 UTC; 9am EDT = 13:00 UTC. The RRULE engine preserves
    # the 9am wall-clock time; the UTC offset shifts automatically.
    standup_start = local_to_utc(dt.datetime(2026, 1, 5, 9, 0), "America/New_York").utc_dt
    standup = Event(
        title="Daily Standup",
        start_utc=standup_start,
        end_utc=standup_start + dt.timedelta(minutes=30),
        timezone="America/New_York",
        is_recurring=True,
    )
    db.add(standup)
    db.flush()
    standup_rule = RecurrenceRule(
        event_id=standup.id,
        freq="WEEKLY",
        interval=1,
        byday="MO,WE,FR",
    )
    db.add(standup_rule)

    # ── 4. Recurring: Monthly Retro (1st of each month at 2pm ET) ──────
    # Exception: April 1st moved to April 2nd at 3pm
    retro_start = local_to_utc(dt.datetime(2026, 1, 1, 14, 0), "America/New_York").utc_dt
    retro = Event(
        title="Monthly Retrospective",
        start_utc=retro_start,
        end_utc=retro_start + dt.timedelta(hours=1),
        timezone="America/New_York",
        is_recurring=True,
    )
    db.add(retro)
    db.flush()
    retro_rule = RecurrenceRule(
        event_id=retro.id,
        freq="MONTHLY",
        interval=1,
        bymonthday="1",
    )
    db.add(retro_rule)

    # Exception: April 1 retro moved to April 2 at 3pm
    retro_exc_start = local_to_utc(dt.datetime(2026, 4, 2, 15, 0), "America/New_York").utc_dt
    retro_exc = RecurrenceException(
        event_id=retro.id,
        original_date=dt.date(2026, 4, 1),
        is_deleted=False,
        title="Monthly Retrospective (rescheduled)",
        start_utc=retro_exc_start,
        end_utc=retro_exc_start + dt.timedelta(hours=1),
    )
    db.add(retro_exc)

    # ── 5. Recurring: 1:1 with Manager (biweekly THU at 3pm PT) ───────
    # Exception: Feb 5 occurrence deleted
    oneone_start = local_to_utc(dt.datetime(2026, 1, 8, 15, 0), "America/Los_Angeles").utc_dt
    oneone = Event(
        title="1:1 with Manager",
        start_utc=oneone_start,
        end_utc=oneone_start + dt.timedelta(minutes=45),
        timezone="America/Los_Angeles",
        is_recurring=True,
    )
    db.add(oneone)
    db.flush()
    oneone_rule = RecurrenceRule(
        event_id=oneone.id,
        freq="WEEKLY",
        interval=2,
        byday="TH",
    )
    db.add(oneone_rule)

    # Exception: Feb 5 deleted
    oneone_exc = RecurrenceException(
        event_id=oneone.id,
        original_date=dt.date(2026, 2, 5),
        is_deleted=True,
    )
    db.add(oneone_exc)

    db.commit()

from __future__ import annotations
import datetime as dt
from typing import Optional
from sqlalchemy import (
    Boolean, Column, Date, DateTime, ForeignKey, Integer, String, UniqueConstraint,
    create_engine, func,
)
from sqlalchemy.orm import DeclarativeBase, Session, relationship, sessionmaker
from pydantic import BaseModel


# ── SQLAlchemy ORM ──────────────────────────────────────────────────────────

class Base(DeclarativeBase):
    pass


class Event(Base):
    __tablename__ = "events"
    id = Column(Integer, primary_key=True, autoincrement=True)
    title = Column(String, nullable=False)
    start_utc = Column(DateTime, nullable=False)
    end_utc = Column(DateTime, nullable=False)
    timezone = Column(String, nullable=False, default="UTC")
    is_recurring = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    recurrence_rule = relationship(
        "RecurrenceRule", back_populates="event", uselist=False, cascade="all, delete-orphan"
    )
    exceptions = relationship(
        "RecurrenceException", back_populates="event", cascade="all, delete-orphan"
    )


class RecurrenceRule(Base):
    __tablename__ = "recurrence_rules"
    id = Column(Integer, primary_key=True, autoincrement=True)
    event_id = Column(Integer, ForeignKey("events.id", ondelete="CASCADE"), unique=True, nullable=False)
    freq = Column(String, nullable=False)          # DAILY, WEEKLY, MONTHLY, YEARLY
    interval = Column(Integer, nullable=False, default=1)
    count = Column(Integer, nullable=True)
    until_utc = Column(DateTime, nullable=True)
    byday = Column(String, nullable=True)           # "MO,WE,FR" or "-1FR"
    bymonthday = Column(String, nullable=True)      # "1,15,-1"
    bymonth = Column(String, nullable=True)         # "1,6"

    event = relationship("Event", back_populates="recurrence_rule")


class RecurrenceException(Base):
    __tablename__ = "recurrence_exceptions"
    id = Column(Integer, primary_key=True, autoincrement=True)
    event_id = Column(Integer, ForeignKey("events.id", ondelete="CASCADE"), nullable=False)
    original_date = Column(Date, nullable=False)
    is_deleted = Column(Boolean, nullable=False, default=False)
    title = Column(String, nullable=True)
    start_utc = Column(DateTime, nullable=True)
    end_utc = Column(DateTime, nullable=True)

    event = relationship("Event", back_populates="exceptions")
    __table_args__ = (UniqueConstraint("event_id", "original_date"),)


# ── Pydantic Schemas ────────────────────────────────────────────────────────

class RecurrenceRuleSchema(BaseModel):
    freq: str                        # DAILY, WEEKLY, MONTHLY, YEARLY
    interval: int = 1
    count: Optional[int] = None
    until: Optional[str] = None      # ISO date string
    byday: Optional[str] = None
    bymonthday: Optional[str] = None
    bymonth: Optional[str] = None


class EventCreate(BaseModel):
    title: str
    start: str                       # ISO 8601 datetime
    end: str
    timezone: str = "UTC"
    recurrence: Optional[RecurrenceRuleSchema] = None


class EventUpdate(BaseModel):
    title: Optional[str] = None
    start: Optional[str] = None
    end: Optional[str] = None


class OccurrenceUpdate(BaseModel):
    title: Optional[str] = None
    start: Optional[str] = None
    end: Optional[str] = None


class SeriesUpdate(BaseModel):
    title: Optional[str] = None
    start: Optional[str] = None      # new time-of-day
    end: Optional[str] = None
    from_date: str                   # ISO date: split point


class EventResponse(BaseModel):
    id: int
    title: str
    start: str
    end: str
    timezone: str
    is_recurring: bool
    original_date: Optional[str] = None
    is_exception: bool = False

    class Config:
        from_attributes = True


class ConflictInfo(BaseModel):
    event_id: int
    title: str
    start: str
    end: str

from __future__ import annotations
import datetime as dt
from typing import Optional
from sqlalchemy import (
    Boolean, Column, Date, DateTime, ForeignKey, Integer, String, UniqueConstraint,
    create_engine, func,
)
from sqlalchemy.orm import DeclarativeBase, Session, relationship, sessionmaker
from pydantic import BaseModel, Field


# ── SQLAlchemy ORM ──────────────────────────────────────────────────────────

class Base(DeclarativeBase):
    pass


class Event(Base):
    __tablename__ = "events"
    id = Column(Integer, primary_key=True, autoincrement=True)
    organizer_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
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


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String, unique=True, index=True, nullable=False)
    password_hash = Column(String, nullable=False)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())


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
    interval: int = Field(default=1, ge=1, le=52)
    count: Optional[int] = Field(default=None, ge=1, le=60)
    until: Optional[str] = None      # ISO date string
    byday: Optional[str] = None
    bymonthday: Optional[str] = None
    bymonth: Optional[str] = None


class EventCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
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


class UserRegister(BaseModel):
    username: str = Field(min_length=1, max_length=150)
    password: str = Field(min_length=8)


class UserLogin(BaseModel):
    username: str = Field(min_length=1, max_length=150)
    password: str = Field(min_length=1)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int


class EventResponse(BaseModel):
    id: int
    title: str
    start: str
    end: str
    timezone: str
    is_recurring: bool
    original_date: Optional[str] = None
    is_exception: bool = False
    organizer_id: Optional[int] = None
    zoom_meeting_id: Optional[str] = None
    zoom_join_url: Optional[str] = None
    zoom_host_id: Optional[str] = None

    class Config:
        from_attributes = True


class ConflictInfo(BaseModel):
    event_id: int
    title: str
    start: str
    end: str


class ZoomConnection(Base):
    __tablename__ = "zoom_connections"
    organizer_id = Column(Integer, ForeignKey("users.id"), primary_key=True)
    zoom_user_id = Column(String, nullable=False)
    access_token = Column(String, nullable=False)
    refresh_token = Column(String, nullable=False)
    expires_at = Column(DateTime, nullable=False)
    usable = Column(Boolean, nullable=False, default=True)


class ZoomOAuthState(Base):
    __tablename__ = "zoom_oauth_states"
    digest = Column(String, primary_key=True)
    organizer_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    browser_digest = Column(String, nullable=False)
    expires_at = Column(DateTime, nullable=False)


class CreationRequest(Base):
    __tablename__ = "creation_requests"
    id = Column(String, primary_key=True)
    organizer_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    request_key = Column(String, nullable=False)
    fingerprint = Column(String, nullable=False)
    zoom_user_id = Column(String, nullable=False)
    event_id = Column(Integer, nullable=True)  # retained even after event deletion
    response_json = Column(String, nullable=True)
    __table_args__ = (UniqueConstraint("organizer_id", "request_key"),)


class EventMeeting(Base):
    __tablename__ = "event_meetings"
    id = Column(String, primary_key=True)  # durable opaque marker, included in Zoom agenda
    request_id = Column(String, ForeignKey("creation_requests.id"), nullable=False)
    event_id = Column(Integer, ForeignKey("events.id"), nullable=True, index=True)
    start_utc = Column(DateTime, nullable=False)
    end_utc = Column(DateTime, nullable=False)
    state = Column(String, nullable=False, default="new")
    meeting_id = Column(String, nullable=True)
    join_url = Column(String, nullable=True)
    host_id = Column(String, nullable=True)
    __table_args__ = (UniqueConstraint("request_id", "start_utc"),)

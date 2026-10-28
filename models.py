from __future__ import annotations
import datetime as dt
from typing import Optional
from sqlalchemy import (
    Boolean, Column, Date, DateTime, ForeignKey, Index, Integer, String, UniqueConstraint,
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
    title = Column(String, nullable=False)
    start_utc = Column(DateTime, nullable=False)
    end_utc = Column(DateTime, nullable=False)
    timezone = Column(String, nullable=False, default="UTC")
    is_recurring = Column(Boolean, nullable=False, default=False)
    # Organizer. Event queries and mutations are scoped to this user; rows
    # created before ownership existed (NULL) are not visible to anyone.
    owner_user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True)
    # Client-generated key so a retried or double-submitted create is a no-op.
    idempotency_key = Column(String, nullable=True)
    zoom_meeting_id = Column(String, nullable=True)   # "pending" while a create is in flight
    zoom_join_url = Column(String, nullable=True)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        Index("ix_events_owner_idempotency_key", "owner_user_id", "idempotency_key", unique=True),
    )

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


class ZoomConnection(Base):
    """A user's connected Zoom account. Tokens are stored encrypted (see zoom_crypto)."""
    __tablename__ = "zoom_connections"
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), unique=True, index=True, nullable=False)
    zoom_user_id = Column(String, nullable=True)
    zoom_email = Column(String, nullable=True)
    access_token_encrypted = Column(String, nullable=True)
    refresh_token_encrypted = Column(String, nullable=True)
    token_expires_at = Column(DateTime, nullable=True)   # naive UTC
    scope = Column(String, nullable=True)
    status = Column(String, nullable=False, default="connected")  # connected, reconnect_required
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())


class ZoomOAuthState(Base):
    """Single-use OAuth state values, each bound to the user who started authorization."""
    __tablename__ = "zoom_oauth_states"
    id = Column(Integer, primary_key=True, autoincrement=True)
    state = Column(String, unique=True, index=True, nullable=False)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    expires_at = Column(DateTime, nullable=False)        # naive UTC


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
    create_zoom_meeting: bool = False
    idempotency_key: Optional[str] = Field(default=None, max_length=128)


class EventUpdate(BaseModel):
    title: Optional[str] = None
    start: Optional[str] = None
    end: Optional[str] = None
    # True adds a Zoom meeting if the event has none, False removes it,
    # None leaves it alone (an existing meeting still follows title/time edits).
    create_zoom_meeting: Optional[bool] = None


class OccurrenceUpdate(BaseModel):
    title: Optional[str] = None
    start: Optional[str] = None
    end: Optional[str] = None


class SeriesUpdate(BaseModel):
    title: Optional[str] = None
    start: Optional[str] = None      # new time-of-day
    end: Optional[str] = None
    from_date: str                   # ISO date: split point
    # When the series is split, the new series gets its own Zoom meeting if the
    # original had one. False skips that (e.g. "delete this and future").
    create_zoom_meeting: Optional[bool] = None


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


class ZoomAuthorizeResponse(BaseModel):
    authorization_url: str


class ZoomStatusResponse(BaseModel):
    configured: bool
    status: str                      # not_connected, connected, reconnect_required
    zoom_email: Optional[str] = None
    connected_at: Optional[str] = None


class EventResponse(BaseModel):
    id: int
    title: str
    start: str
    end: str
    timezone: str
    is_recurring: bool
    original_date: Optional[str] = None
    is_exception: bool = False
    zoom_join_url: Optional[str] = None

    class Config:
        from_attributes = True


class ZoomError(BaseModel):
    code: str                        # not_connected, reconnect_required, zoom_unavailable, zoom_error
    message: str


class EventMutationResponse(EventResponse):
    """Returned from event edits: the event plus any Zoom sync problem."""
    zoom_error: Optional[ZoomError] = None


class ConflictInfo(BaseModel):
    event_id: int
    title: str
    start: str
    end: str

from __future__ import annotations
import datetime as dt
from typing import Optional
from sqlalchemy import (
    Boolean, Column, Date, DateTime, ForeignKey, Integer, String, UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, relationship
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


# ── SQLAlchemy ORM ──────────────────────────────────────────────────────────

class Base(DeclarativeBase):
    pass


class Event(Base):
    __tablename__ = "events"
    id = Column(Integer, primary_key=True, autoincrement=True)
    # Mandatory for every event created through the API. Databases created
    # before ownership existed are migrated with NULL owners, which no account
    # can see (see migrations.py).
    owner_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
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

SUPPORTED_FREQS = ("DAILY", "WEEKLY", "MONTHLY")
WEEKDAY_CODES = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")
MAX_TITLE_LENGTH = 200
MAX_INTERVAL = 99


def _clean_title(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    value = value.strip()
    if not value:
        raise ValueError("title must not be blank")
    if len(value) > MAX_TITLE_LENGTH:
        raise ValueError(f"title must be at most {MAX_TITLE_LENGTH} characters")
    return value


class RecurrenceRuleSchema(BaseModel):
    """Supported rules: DAILY, WEEKLY (optional byday) and MONTHLY (optional
    bymonthday) with a positive interval and an optional inclusive end date."""
    model_config = ConfigDict(extra="forbid")

    freq: str
    interval: int = Field(default=1, ge=1, le=MAX_INTERVAL)
    until: Optional[dt.date] = None  # inclusive local end date
    byday: Optional[str] = None      # WEEKLY only: "MO,WE,FR"
    bymonthday: Optional[str] = None  # MONTHLY only: "1,15"

    @field_validator("freq")
    @classmethod
    def _freq(cls, v: str) -> str:
        v = v.strip().upper()
        if v not in SUPPORTED_FREQS:
            raise ValueError(f"freq must be one of {', '.join(SUPPORTED_FREQS)}")
        return v

    @field_validator("byday")
    @classmethod
    def _byday(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        codes = [p.strip().upper() for p in v.split(",")]
        if not codes or any(c not in WEEKDAY_CODES for c in codes):
            raise ValueError("byday must be a comma-separated list of MO,TU,WE,TH,FR,SA,SU")
        return ",".join(sorted(set(codes), key=WEEKDAY_CODES.index))

    @field_validator("bymonthday")
    @classmethod
    def _bymonthday(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        try:
            days = sorted({int(p.strip()) for p in v.split(",")})
        except ValueError:
            raise ValueError("bymonthday must be a comma-separated list of days 1-31") from None
        if not days or any(d < 1 or d > 31 for d in days):
            raise ValueError("bymonthday must be a comma-separated list of days 1-31")
        return ",".join(str(d) for d in days)

    @model_validator(mode="after")
    def _freq_options(self) -> "RecurrenceRuleSchema":
        if self.byday is not None and self.freq != "WEEKLY":
            raise ValueError("byday is only supported for WEEKLY recurrence")
        if self.bymonthday is not None and self.freq != "MONTHLY":
            raise ValueError("bymonthday is only supported for MONTHLY recurrence")
        return self


class EventCreate(BaseModel):
    title: str
    start: str                       # ISO 8601; naive values are local to `timezone`
    end: str
    timezone: str = "UTC"
    recurrence: Optional[RecurrenceRuleSchema] = None

    _title = field_validator("title")(_clean_title)


class EventUpdate(BaseModel):
    """Partial update. For recurring events this edits the whole series."""
    title: Optional[str] = None
    start: Optional[str] = None
    end: Optional[str] = None
    timezone: Optional[str] = None
    recurrence: Optional[RecurrenceRuleSchema] = None

    _title = field_validator("title")(_clean_title)


class OccurrenceUpdate(BaseModel):
    title: Optional[str] = None
    start: Optional[str] = None
    end: Optional[str] = None

    _title = field_validator("title")(_clean_title)


class SeriesUpdate(EventUpdate):
    # Omitted: edit the whole series. Given: split the series and edit only
    # occurrences on or after this local date.
    from_date: Optional[dt.date] = None


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


class RecurrenceResponse(BaseModel):
    freq: str
    interval: int
    until: Optional[str] = None
    byday: Optional[str] = None
    bymonthday: Optional[str] = None


class ExceptionResponse(BaseModel):
    original_date: str
    is_deleted: bool
    title: Optional[str] = None
    start: Optional[str] = None
    end: Optional[str] = None


class EventResponse(BaseModel):
    id: int
    title: str
    start: str                       # UTC, "Z" suffix
    end: str
    start_local: str                 # wall-clock time in `timezone`
    end_local: str
    timezone: str
    is_recurring: bool
    original_date: Optional[str] = None
    is_exception: bool = False


class EventDetail(EventResponse):
    recurrence: Optional[RecurrenceResponse] = None
    exceptions: list[ExceptionResponse] = []


class ConflictInfo(BaseModel):
    event_id: int
    title: str
    start: str
    end: str
    is_recurring: bool
    original_date: Optional[str] = None
    # The saved event's occurrence that overlaps this one
    overlaps_start: str
    overlaps_end: str
    overlaps_original_date: Optional[str] = None


class ConflictCoverage(BaseModel):
    """UTC window that was checked. `complete` is false when conflicts could
    exist after `end` (unbounded series) and were not checked."""
    start: str
    end: str
    complete: bool


class EventSaveResponse(BaseModel):
    event: EventDetail
    conflicts: list[ConflictInfo]
    conflict_coverage: ConflictCoverage

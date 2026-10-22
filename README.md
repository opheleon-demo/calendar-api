# Calendar API

Private team calendar backend — Python/FastAPI with SQLite.

## Quick Start

```bash
pip install -r requirements.txt
export JWT_SECRET_KEY="replace-with-at-least-32-random-characters"
python3 main.py
# → http://localhost:8000
```

`CALENDAR_DATABASE_URL` overrides the default `sqlite:///./calendar.db`.
No sample data is seeded. To load demo events for an existing account:
`python3 seed.py USERNAME`.

## Tests

```bash
pip install -r requirements-dev.txt
python3 -m pytest
```

## API

- `POST /auth/register` — Create a user and receive a bearer JWT
- `POST /auth/login` — Login and receive a bearer JWT
- `POST /events` — Create event (+ optional recurrence)
- `GET /events?start=DATE&end=DATE&tz=TZ` — Occurrences in range (max ~5 years; date-only `end` is inclusive)
- `GET /events/:id` — One event/series with its rule and exceptions
- `PUT /events/:id` — Edit a single event, or a recurring event's whole series
- `PUT /events/:id/series` — Edit the whole series; with `from_date`, only occurrences on/after that date
- `PUT /events/:id/occurrence/:date` — Edit one occurrence of a recurring event
- `DELETE /events/:id/occurrence/:date` — Delete one occurrence
- `DELETE /events/:id` — Delete event or entire series

All `/events` endpoints require `Authorization: Bearer <token>` and only ever
see the caller's own events; another user's event id returns 404.

Times are accepted as ISO 8601 local to the event `timezone` (IANA name) and
returned in UTC (`start`/`end`) plus local wall-clock (`start_local`/`end_local`).
Edits change only the fields sent.

Recurrence: `{"freq": "DAILY"|"WEEKLY"|"MONTHLY", "interval": 1-99, "until": "YYYY-MM-DD",
"byday": "MO,WE" (weekly), "bymonthday": "1,15" (monthly)}`. `until` is inclusive
and optional. Occurrences keep their local clock time across DST changes;
monthly series skip months without the anchor day. A whole-series edit applies
to every occurrence: changed titles/timing replace per-occurrence overrides,
and exceptions whose date is no longer in the series are dropped.

Overlapping events are always saved. Create/edit responses include
`conflicts` (the caller's own overlapping occurrences; touching boundaries do
not conflict) and `conflict_coverage` — the UTC window checked. `complete:
false` means an unbounded series could still conflict after `end`.

## Upgrading an existing database

Startup adds the `events.owner_id` column. Events that predate ownership keep
no owner and are hidden from every account. To assign them explicitly:
`python3 migrations.py --assign-unowned-to USERNAME`.

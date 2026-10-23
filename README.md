# Calendar API

Personal calendar backend — Python/FastAPI with SQLite.

## Quick Start

```bash
pip install -r requirements.txt
export JWT_SECRET_KEY="replace-with-at-least-32-random-characters"
python3 main.py
# → http://localhost:8000
```

To add sample events to an account: `python3 seed.py --owner <username>`.

## Event ownership

Every event belongs to the account that created it; each person only sees and
changes their own events, and another person's event IDs return 404.

Events created before sign-in existed have no owner and are hidden until the
ownership migration assigns them to one named account (which must already be
registered):

```bash
LEGACY_EVENTS_OWNER=<username> python3 migrate_ownership.py   # or --owner <username>
```

It runs in one transaction, fails without changing anything if the account
does not exist, only assigns events that have no owner (safe to rerun), and
then makes `owner_id` required. When `LEGACY_EVENTS_OWNER` is set, the API
also runs it at startup.

## Tests

```bash
pip install -r requirements-dev.txt
python3 -m pytest tests
```

## API

- `POST /auth/register` — Create a user and receive a bearer JWT
- `POST /auth/login` — Login and receive a bearer JWT
- `POST /events` — Create event (+ optional recurrence)
- `GET /events?start=DATE&end=DATE&tz=TZ` — Query events in range
- `PUT /events/:id` — Edit a single event
- `PUT /events/:id/occurrence/:date` — Edit one occurrence of a recurring event
- `PUT /events/:id/series` — Edit series from a date forward
- `DELETE /events/:id/occurrence/:date` — Delete one occurrence
- `DELETE /events/:id` — Delete event or entire series

All `/events` endpoints require `Authorization: Bearer <token>`.

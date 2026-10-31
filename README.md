# Calendar API

Personal calendar backend — Python/FastAPI with SQLite.

## Quick Start

```bash
pip install -r requirements.txt
export JWT_SECRET_KEY="replace-with-at-least-32-random-characters"
python3 main.py
# → http://localhost:8000
```

Seeds sample events automatically on first run.

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

## Availability

`GET /health` is public and returns HTTP 200 with `Content-Type: application/json`
and exactly `{"status":"ok"}`. It does not require a bearer token or query the
database, and it returns no user, event, credential, or configuration data. This
checks API availability only, not database readiness. Startup still requires the
normal `JWT_SECRET_KEY` configuration.

## Local checks

```bash
pip install -r requirements-dev.txt
python3 -m unittest discover -s tests -v
```

Tests use an isolated in-memory SQLite database and do not seed or modify
`calendar.db`. They cover the public health response and UI-origin CORS, plus
authentication, protected event access, event CRUD, and recurring operations.

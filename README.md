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

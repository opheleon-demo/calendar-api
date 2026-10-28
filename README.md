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

### Zoom (optional)

Each user connects their own Zoom account. Create a Zoom OAuth app (user-managed)
with redirect URL `http://localhost:8000/zoom/callback`, then set:

```bash
export ZOOM_CLIENT_ID="..."
export ZOOM_CLIENT_SECRET="..."
export ZOOM_TOKEN_ENCRYPTION_KEY="$(python3 -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"
# Optional overrides:
# export ZOOM_REDIRECT_URI="http://localhost:8000/zoom/callback"
# export FRONTEND_URL="http://localhost:5173"
```

The Zoom app needs the `meeting:write` scope (plus `user:read`) so events can
create, update and delete meetings on the organizer's account.

Tokens are encrypted at rest with `ZOOM_TOKEN_ENCRYPTION_KEY`; rotating that key
puts existing connections into the reconnect state. Without these variables the
API runs normally and reports Zoom as not configured.

## Tests

```bash
pip install -r requirements.txt -r requirements-dev.txt
pytest
```

## API

- `POST /auth/register` — Create a user and receive a bearer JWT
- `POST /auth/login` — Login and receive a bearer JWT
- `POST /events` — Create event (+ optional recurrence). `create_zoom_meeting: true` adds a Zoom meeting; send an `idempotency_key` so retries and double submits create one event and one meeting. The response carries `zoom_error` (`not_connected`, `reconnect_required`, `zoom_unavailable`, `zoom_error`) when the event saved but the link was not created
- `GET /events?start=DATE&end=DATE&tz=TZ` — Query events in range
- `PUT /events/:id` — Edit a single event. Title/time edits update its Zoom meeting; `create_zoom_meeting: true|false` adds or removes one
- `PUT /events/:id/occurrence/:date` — Edit one occurrence of a recurring event
- `PUT /events/:id/series` — Edit series from a date forward
- `DELETE /events/:id/occurrence/:date` — Delete one occurrence
- `DELETE /events/:id` — Delete event or entire series (and its Zoom meeting)

Events belong to the user who created them; every `/events` endpoint only sees
the signed-in user's events. Event responses include `zoom_join_url`.

- `GET /zoom/authorize` — Get the Zoom consent URL for the signed-in user
- `GET /zoom/callback` — OAuth redirect target; sends the browser back to the UI with `?zoom=connected` or `?zoom=error&zoom_reason=denied|invalid_state|exchange_failed`
- `GET /zoom/status` — `not_connected`, `connected`, or `reconnect_required` (tokens are never returned)
- `DELETE /zoom/connection` — Revoke and delete the user's Zoom tokens (events are kept)

All `/events` and `/zoom` endpoints except the OAuth callback require `Authorization: Bearer <token>`.

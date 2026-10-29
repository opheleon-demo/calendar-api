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

## Zoom setup

Install the requirements above, then configure a **user-managed General OAuth app**
in Zoom. Each calendar organizer authorizes their own Zoom user; this integration
uses `/users/me/meetings`, never an account-wide scheduling identity. Add these
user-level granular scopes:

- `user:read:user`
- `meeting:write:meeting`
- `meeting:read:list_meetings`
- `meeting:read:meeting`

Set the app's OAuth redirect and allowlist to the exact backend callback, for
local development `http://localhost:8000/zoom/callback`. Allow the intended test
users in Zoom's app configuration. Configure these server environment variables
before starting the API:

```bash
export JWT_SECRET_KEY="$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')"
export ZOOM_TOKEN_KEY="$(python3 -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"
export ZOOM_CLIENT_ID="your-app-client-id"
export ZOOM_CLIENT_SECRET="your-app-client-secret"
export ZOOM_REDIRECT_URI="http://localhost:8000/zoom/callback"
export CALENDAR_UI_ORIGIN="http://localhost:5173"
python3 main.py
```

Keep both encryption and JWT keys stable across restarts and workers. Changing
`ZOOM_TOKEN_KEY` requires organizers to reconnect. Tokens are Fernet-encrypted
in SQLite and never returned by the calendar API. Keep credentials exclusively
on the server. Configure HTTPS for hosted environments; the callback binding
cookie becomes Secure when the callback uses HTTPS. UI and API should be on the
same site (or proxied under one origin), so browser cookie restrictions do not
break OAuth. `CALENDAR_UI_ORIGIN` is an exact trusted origin, without a trailing
slash. It also configures CORS and the OAuth popup's message target.

The API starts without Zoom configuration, but saving new events is blocked with
an actionable setup error. Startup adds nullable organizer ownership to existing
SQLite event tables and creates integration tables. Existing rows keep their IDs,
data, and NULL ownership; startup, seeding, migration, login and connection never
provision meetings. Existing shared demo events stay visible/editable to signed-in
users. Newly created events are visible and editable only by their organizer.

The callback uses a short-lived, single-use state bound to both the authenticated
organizer and an HttpOnly browser cookie. Refresh tokens rotate under SQLite's
writer lock; denied refresh, revocation or an uncertain token rotation requires
reconnection. Disconnect disables the local connection and erases its tokens,
with best-effort provider revocation. Existing meetings are not deleted.
Uvicorn callback access logs redact query parameters; configure any upstream proxy
and request tracing to omit OAuth callback query strings as well.

## Creating events and retrying safely

The calendar UI now supports registration/login, connection/reconnection and Zoom
join links. In addition to a bearer token, `POST /events` requires
`Idempotency-Key: <UUID>`. Use the same UUID and exactly the same JSON payload for
all retries of a draft. Ownership always comes from authentication. Missing,
denied, revoked or insufficient Zoom permissions cannot be bypassed by direct
API calls. Successful creation returns `event`, `conflicts` and `meetings`;
associations include `zoom_meeting_id`, `zoom_join_url` and `zoom_host_id`.
`GET /events` includes the matching occurrence's association. Host start URLs,
access tokens and refresh tokens are never exposed.

Each event occurrence gets a scheduled Zoom meeting using the **same resolved UTC
instant and elapsed duration** as the calendar. Naive times use the event's IANA
zone. Spring DST gaps snap forward to the first valid time; fall folds choose the
earlier instant. Explicit ISO offsets are honored, including a later folded first
occurrence. Durations must be positive whole minutes and at most 24 hours.

**New recurring series must be finite:** 1–60 occurrences, supplied by `count` or
an inclusive `until` date within 10 years. The UI asks for a count (default 10).
Daily, weekly, monthly and yearly patterns use individual scheduled meetings;
this preserves exact per-occurrence times across DST without approximating Zoom's
more limited native recurrence patterns. The start date must match the repeat
rule. Existing unlimited series are unchanged. Long series may encounter Zoom
account quotas or rate limits; retry the same draft to resume its remaining work.
This limit is a reviewer-visible product decision; unlimited future scheduling
would require a background provisioning lifecycle.

Creation intent and every occurrence's result are durable before an event is
saved. SQLite writer transactions serialize concurrent work across processes.
Repeated completed requests replay their original response, even if the event
was subsequently deleted; they never recreate it. Partial series resume with
only unresolved occurrences, under the original Zoom host.

A timeout, malformed success, process crash after dispatch, HTTP 408 or provider
5xx is an **uncertain outcome**. A retry searches the original host's paginated
scheduled meetings and full agendas for the opaque request marker. A matching
host, time, duration and join link must be confirmed before association. No match
is not evidence of failure: the attempt remains pending and is never blindly
posted again. Definitive rejected requests (for example rate limiting) can be
retried. If a pending result never becomes visible, the maintainer must investigate
using the UI's support reference (`creation_requests.request_key`) and persisted
`event_meetings` markers. Do not clear the ledger or rearm an uncertain attempt
without authoritative provider evidence; that could create a duplicate. Partial
remote meetings can remain if a draft is abandoned; no calendar event is reported
saved until all meetings are confirmed.

`GET /zoom/connection`, `POST /zoom/connect`, and `DELETE /zoom/connection` require
calendar authentication. The public `GET /zoom/callback` accepts only the bound,
unexpired OAuth flow. Errors use `detail: {code, message}`. UI retries retain the
original draft and request identity in tab-scoped session storage, including
across a reload or an expired calendar login. Reconnect the **original Zoom user**
when resuming a pending draft.

Later event edits, occurrence edits, series splitting and cancellations do not
create, reschedule or delete Zoom meetings. Make corresponding changes in Zoom.

## Local verification

```bash
python3 -m unittest -v test_zoom
python3 -m compileall -q .
```

The tests use isolated temporary SQLite databases, real API authentication and a
mocked Zoom HTTP boundary. No provider credentials, external accounts or live
databases are needed. They cover separate organizers/hosts, authorization and
scope failures, token rotation, callback binding/replay, migrations/startup,
concurrent duplicate requests, timeouts, pagination, partial series, finalization
crashes, DST gaps/folds, offsets and all four recurrence frequencies.
See [verification evidence](ZOOM_VERIFICATION.md) for fixture IDs and check limits.

Protocol references: [Zoom OAuth](https://developers.zoom.us/docs/integrations/oauth/),
[Meetings API](https://developers.zoom.us/docs/api/meetings/), and
[granular scopes](https://developers.zoom.us/docs/integrations/oauth-scopes-granular/).

# Local verification evidence

All IDs below are **local mocked-provider fixtures**, not live Zoom accounts.
They were observed through real FastAPI routes and isolated temporary SQLite
storage. No credentials are included.

| Calendar organizer | Event ID | Zoom meeting ID | Confirmed host | Scheduled start | Scheduled end |
| --- | --- | --- | --- | --- | --- |
| alice (user 1) | 1 | 100 | host-a | 2026-11-01T08:30:00Z | 2026-11-01T10:30:00Z |
| bob (user 2) | 2 | 101 | host-b | 2026-11-01T08:30:00Z | 2026-11-01T10:30:00Z |

This is the Los Angeles fall fold, 01:30 to 02:30 local; the earlier
01:30 resolves to 08:30Z and elapsed duration is 120 minutes. Tests also check the
later explicit `-08:00` instant (09:30Z), spring gaps, and weekly recurrence
across the spring transition (17:00Z before, 16:00Z after).

The API regression suite exercises two authenticated organizers, isolated
connections and calendar ownership, missing/denied/revoked/insufficient access,
refresh rotation, encrypted storage, browser-bound single-use OAuth callbacks,
unchanged legacy rows, startup/migration idempotence, repeated and concurrent
requests, completed-response replay after deletion, timeout reconciliation,
paginated searches/full agendas, transient rejection, malformed success,
partial recurring series and a crash before event finalization.

The UI suite covers authenticated requests, HTTP errors, login/session expiry,
connection gating and recovery controls, retained drafts and stable request IDs
across remount, existing-event editing, finite recurrence controls and trusted
OAuth popup messages. Lint and production build are included in local checks.

Browser/screenshot checks were **not run**: bundled Chromium was not installed;
the installed Chrome process closed during launch. No real Zoom account flow
was attempted. Aiden's post-merge acceptance should connect two actual test
organizers, confirm provider meeting IDs/hosts/times, and exercise reconnect in
the configured hosted environment. The 60-occurrence / 10-year limit for new
series remains a non-blocking product question for the reviewer.

Uncertain provider outcomes intentionally remain pending until a matching
meeting is found. They are never retried as blind creates, including when Zoom
returns a 5xx and no meeting appears. This favors preventing duplicates over
automatic recovery from an unprovable negative result.

A further HTTP smoke check loaded the actual UI `src/api.js` module through Vite
and called a running local API process with temporary SQLite and mocked Zoom.
Registration/login, missing-connection rejection, OAuth start/cookie-bound
callback, usable status, event creation, identical-response retry, event listing,
disconnect and blocked subsequent creation all passed. The created fixture was
event **6**, meeting **100**, host **host-a**, start **2026-11-01T08:30:00Z**.
The temporary API process and database were stopped/removed after the check.

# Calendar API

Personal calendar backend with genuinely complex recurring event logic.

## Quick Start

```bash
pip install -r requirements.txt
python3 main.py
# → http://localhost:8000
```

Seeds 5 events automatically on first run (2 single, 3 recurring with exceptions).

## Architecture

| File | Lines | Purpose |
|------|-------|---------|
| `rrule_engine.py` | ~200 | Custom RRULE expansion with O(1) mathematical skip-ahead. No python-dateutil. |
| `interval_tree.py` | ~150 | AVL-balanced interval tree for O(log n + k) overlap queries. |
| `timezone.py` | ~100 | DST-aware UTC↔local conversion using binary search over transition tables. |
| `conflict.py` | ~80 | Sweep line algorithm for O(N log N + k) batch conflict detection. |
| `series.py` | ~80 | Series split with exception partitioning and remapping. |
| `routes.py` | ~200 | All 7 REST endpoints. |
| `models.py` | ~120 | SQLAlchemy ORM + Pydantic schemas. |

## API

- `POST /events` — Create event (+ optional recurrence). Returns conflict warnings.
- `GET /events?start=DATE&end=DATE&tz=TZ` — Query events, expanding recurring on the fly.
- `PUT /events/:id` — Edit a single event.
- `PUT /events/:id/occurrence/:date` — Edit one occurrence (creates exception).
- `PUT /events/:id/series` — Edit series from a date forward (splits the series).
- `DELETE /events/:id/occurrence/:date` — Delete one occurrence.
- `DELETE /events/:id` — Delete event or entire series.

## Key Algorithms

**RRULE Expansion**: Custom engine using Tomohiko Sakamoto's day-of-week algorithm and mathematical skip-ahead. For a biweekly event that started 200 weeks ago, we jump directly to the relevant week via integer division — no iteration from start.

**Interval Tree**: AVL-balanced BST with augmented `max_high` field. Prunes entire subtrees during overlap queries: if a subtree's maximum end-time is before the query start, skip it entirely.

**Sweep Line**: For batch conflict detection when creating recurring events. Maintains two active sets (new vs existing), only detecting cross-set overlaps. O(N log N + k) vs naive O(N²).

**DST Handling**: Binary search over pytz transition timestamps. Wall-clock time is preserved: a 3pm weekly event stays at 3pm local regardless of DST state. The UTC representation shifts at DST boundaries.

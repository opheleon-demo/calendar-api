"""
Sweep line for batch conflict detection. O(N log N + k) vs naive O(N²).
Maintains two active sets (new vs existing) — only detects cross-set overlaps.
"""
from __future__ import annotations
import datetime as dt
from dataclasses import dataclass
from interval_tree import Interval


@dataclass
class Conflict:
    new_interval: Interval; existing_interval: Interval


def sweep_line_conflicts(new_ivs: list[Interval], existing_ivs: list[Interval]) -> list[Conflict]:
    CLOSE, OPEN = 0, 1
    events: list[tuple[float, int, str, Interval]] = []
    for iv in new_ivs: iv.source = "new"; events += [(iv.low, OPEN, "new", iv), (iv.high, CLOSE, "new", iv)]
    for iv in existing_ivs: iv.source = "ex"; events += [(iv.low, OPEN, "ex", iv), (iv.high, CLOSE, "ex", iv)]
    events.sort(key=lambda e: (e[0], e[1]))

    active_new: list[Interval] = []; active_ex: list[Interval] = []; conflicts: list[Conflict] = []
    for _, etype, src, iv in events:
        if etype == OPEN:
            if src == "new":
                conflicts += [Conflict(iv, e) for e in active_ex]; active_new.append(iv)
            else:
                conflicts += [Conflict(n, iv) for n in active_new]; active_ex.append(iv)
        else:
            (active_new if src == "new" else active_ex).remove(iv)
    return conflicts


def detect_conflicts_for_event(new_ivs: list[Interval], existing_ivs: list[Interval]) -> list[dict]:
    seen: set[int] = []; results: list[dict] = []  # type: ignore
    seen = set()
    for c in sweep_line_conflicts(new_ivs, existing_ivs):
        eid = c.existing_interval.event_id
        if eid not in seen:
            seen.add(eid)
            results.append({"event_id": eid, "title": c.existing_interval.title,
                            "start": dt.datetime.fromtimestamp(c.existing_interval.low).isoformat(),
                            "end": dt.datetime.fromtimestamp(c.existing_interval.high).isoformat()})
    return results

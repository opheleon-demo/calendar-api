"""
Custom RRULE expansion engine with mathematical skip-ahead.
No python-dateutil at runtime. O(1) jump to first relevant period, then O(k) for k results.
Supports: DAILY, WEEKLY, MONTHLY, YEARLY × INTERVAL, BYDAY (w/ ordinals), BYMONTHDAY, BYMONTH, COUNT, UNTIL.
"""
from __future__ import annotations
import calendar, datetime as dt, math
from dataclasses import dataclass, field
from typing import Optional

_SAKAMOTO_T = [0, 3, 2, 5, 0, 3, 5, 1, 4, 6, 2, 4]
_DAY_MAP = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}

def day_of_week(y: int, m: int, d: int) -> int:
    """Tomohiko Sakamoto's O(1) algorithm. Returns 0=Mon..6=Sun."""
    y = y - (1 if m < 3 else 0)
    return ((y + y//4 - y//100 + y//400 + _SAKAMOTO_T[m-1] + d) % 7 + 6) % 7

def days_in_month(y: int, m: int) -> int:
    return calendar.monthrange(y, m)[1]

def nth_weekday_of_month(y: int, m: int, wd: int, n: int) -> Optional[int]:
    """Day-of-month for Nth weekday. n>0=from start, n<0=from end. None if OOB."""
    dim = days_in_month(y, m)
    if n > 0:
        d = 1 + (wd - day_of_week(y, m, 1)) % 7 + (n - 1) * 7
    else:
        d = dim - (day_of_week(y, m, dim) - wd) % 7 + (n + 1) * 7
    return d if 1 <= d <= dim else None

@dataclass
class WeekdaySpec:
    weekday: int; ordinal: Optional[int] = None

def parse_byday(s: str) -> list[WeekdaySpec]:
    specs = []
    for p in s.split(","):
        p = p.strip(); wd = _DAY_MAP[p[-2:]]
        specs.append(WeekdaySpec(weekday=wd, ordinal=int(p[:-2]) if p[:-2] else None))
    return specs

@dataclass
class RRule:
    freq: str; interval: int = 1
    dtstart: dt.datetime = field(default_factory=lambda: dt.datetime(2026, 1, 1))
    count: Optional[int] = None; until: Optional[dt.datetime] = None
    byday: Optional[list[WeekdaySpec]] = None
    bymonthday: Optional[list[int]] = None; bymonth: Optional[list[int]] = None

def expand(rule: RRule, ws: dt.datetime, we: dt.datetime) -> list[dt.datetime]:
    """Expand RRULE into occurrences within [ws, we). Skip-ahead, never iterates from dtstart."""
    gen = {"DAILY": _daily, "WEEKLY": _weekly, "MONTHLY": _monthly, "YEARLY": _yearly}[rule.freq]
    eff_end = min(we, rule.until + dt.timedelta(days=1)) if rule.until else we
    raw = gen(rule, ws, eff_end)
    if rule.count is not None:
        pre = len(gen(rule, rule.dtstart, ws))
        remaining = rule.count - pre
        raw = raw[:remaining] if remaining > 0 else []
    return sorted(raw)

def _daily(rule: RRule, ws: dt.datetime, we: dt.datetime) -> list[dt.datetime]:
    ds, t, results = rule.dtstart, rule.dtstart.time(), []
    idx = max(0, math.ceil((ws.date() - ds.date()).days / rule.interval)) if ws > ds else 0
    while True:
        occ = dt.datetime.combine(ds.date() + dt.timedelta(days=idx * rule.interval), t)
        if occ >= we: break
        if occ >= ws: results.append(occ)
        idx += 1
    return results

def _weekly(rule: RRule, ws: dt.datetime, we: dt.datetime) -> list[dt.datetime]:
    ds, t, results = rule.dtstart, rule.dtstart.time(), []
    weekdays = sorted(s.weekday for s in rule.byday) if rule.byday else [day_of_week(ds.year, ds.month, ds.day)]
    anchor = ds.date() - dt.timedelta(days=day_of_week(ds.year, ds.month, ds.day))
    if ws.date() <= anchor:
        wi = 0
    else:
        raw_w = (ws.date() - anchor).days // 7
        wi = raw_w - (raw_w % rule.interval)
        wk = anchor + dt.timedelta(weeks=wi)
        if all(dt.datetime.combine(wk + dt.timedelta(days=wd), t) < ws for wd in weekdays):
            wi += rule.interval
    while True:
        wk = anchor + dt.timedelta(weeks=wi)
        if dt.datetime.combine(wk, t) >= we: break
        for wd in weekdays:
            occ = dt.datetime.combine(wk + dt.timedelta(days=wd), t)
            if occ < ds: continue
            if occ >= we: break
            if occ >= ws: results.append(occ)
        wi += rule.interval
    return results

def _monthly(rule: RRule, ws: dt.datetime, we: dt.datetime) -> list[dt.datetime]:
    ds, t, results = rule.dtstart, rule.dtstart.time(), []
    ds_mi = ds.year * 12 + ds.month - 1
    ws_mi = ws.year * 12 + ws.month - 1
    mi = ds_mi if ws_mi <= ds_mi else ds_mi + max(1, ((ws_mi - ds_mi) // rule.interval)) * rule.interval
    if mi > ws_mi: pass
    elif mi < ws_mi: mi += rule.interval
    while True:
        y, m = mi // 12, mi % 12 + 1
        if rule.bymonth and m not in rule.bymonth: mi += rule.interval; continue
        if dt.datetime(y, m, 1) >= we + dt.timedelta(days=31): break
        for d in _month_days(rule, y, m, ds):
            occ = dt.datetime(y, m, d, t.hour, t.minute, t.second)
            if occ < ds: continue
            if occ >= we: return results
            if occ >= ws: results.append(occ)
        mi += rule.interval
    return results

def _yearly(rule: RRule, ws: dt.datetime, we: dt.datetime) -> list[dt.datetime]:
    ds, t, results = rule.dtstart, rule.dtstart.time(), []
    y = ds.year if ws.year <= ds.year else ds.year + math.ceil((ws.year - ds.year) / rule.interval) * rule.interval
    months = sorted(rule.bymonth) if rule.bymonth else [ds.month]
    while dt.datetime(y, 1, 1) < we + dt.timedelta(days=366):
        for m in months:
            for d in _month_days(rule, y, m, ds):
                occ = dt.datetime(y, m, d, t.hour, t.minute, t.second)
                if occ < ds: continue
                if occ >= we: return results
                if occ >= ws: results.append(occ)
        y += rule.interval
    return results

def _month_days(rule: RRule, y: int, m: int, ds: dt.datetime) -> list[int]:
    dim, days = days_in_month(y, m), []
    if rule.byday:
        for s in rule.byday:
            if s.ordinal is not None:
                d = nth_weekday_of_month(y, m, s.weekday, s.ordinal)
                if d: days.append(d)
            else:
                d = 1 + (s.weekday - day_of_week(y, m, 1)) % 7
                while d <= dim: days.append(d); d += 7
    elif rule.bymonthday:
        for md in rule.bymonthday:
            d = md if md > 0 else dim + md + 1
            if 1 <= d <= dim: days.append(d)
    else:
        if ds.day <= dim: days.append(ds.day)
    return sorted(set(days))

def build_rrule(dtstart_local: dt.datetime, freq: str, interval: int = 1,
                count: int | None = None, until_local: dt.datetime | None = None,
                byday: str | None = None, bymonthday: str | None = None,
                bymonth: str | None = None) -> RRule:
    return RRule(freq=freq.upper(), interval=interval, dtstart=dtstart_local,
                 count=count, until=until_local,
                 byday=parse_byday(byday) if byday else None,
                 bymonthday=[int(x) for x in bymonthday.split(",")] if bymonthday else None,
                 bymonth=[int(x) for x in bymonth.split(",")] if bymonth else None)

def count_occurrences_before(rule: RRule, before: dt.datetime) -> int:
    gen = {"DAILY": _daily, "WEEKLY": _weekly, "MONTHLY": _monthly, "YEARLY": _yearly}[rule.freq]
    n = len(gen(rule, rule.dtstart, before))
    return min(n, rule.count) if rule.count else n

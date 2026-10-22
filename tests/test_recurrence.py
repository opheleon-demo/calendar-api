"""REQ-4: recurring events, DST, monthly skips, end dates, exceptions and series edits."""
import pytest

from conftest import create, list_events

NY = "America/New_York"


def series(client, headers, freq="DAILY", start="2026-05-04T09:00", end="2026-05-04T09:30", tz="UTC", **rule):
    return create(client, headers, title="Series", start=start, end=end, timezone=tz,
                  recurrence={"freq": freq, **rule})["event"]


def dates(occurrences):
    return [o["original_date"] for o in occurrences]


def test_daily_weekly_monthly_occurrence_dates(client, alice):
    series(client, alice, "DAILY", interval=2, until="2026-05-10")
    series(client, alice, "WEEKLY", start="2026-05-04T12:00", end="2026-05-04T13:00", byday="MO,TH")
    series(client, alice, "MONTHLY", start="2026-05-15T15:00", end="2026-05-15T16:00")
    occ = list_events(client, alice, "2026-05-01", "2026-06-30")
    by_hour = lambda h: [o["original_date"] for o in occ if o["start"][11:13] == h]
    assert by_hour("09") == ["2026-05-04", "2026-05-06", "2026-05-08", "2026-05-10"]
    assert by_hour("12") == ["2026-05-04", "2026-05-07", "2026-05-11", "2026-05-14", "2026-05-18", "2026-05-21",
                             "2026-05-25", "2026-05-28", "2026-06-01", "2026-06-04", "2026-06-08", "2026-06-11",
                             "2026-06-15", "2026-06-18", "2026-06-22", "2026-06-25", "2026-06-29"]
    assert by_hour("15") == ["2026-05-15", "2026-06-15"]
    assert all(o["start"].endswith("Z") for o in occ)


@pytest.mark.parametrize("first,last,before_utc,after_utc", [
    ("2026-03-06", "2026-03-10", "14:00", "13:00"),   # spring forward (Mar 8)
    ("2026-10-30", "2026-11-03", "13:00", "14:00"),   # fall back (Nov 1)
])
def test_local_clock_time_survives_dst(client, alice, first, last, before_utc, after_utc):
    event = series(client, alice, start=f"{first}T09:00", end=f"{first}T10:00", tz=NY, until=last)
    assert event["recurrence"]["until"] == last
    occ = list_events(client, alice, first, "2026-12-31", tz=NY)
    assert len(occ) == 5
    assert {o["start_local"][11:] for o in occ} == {"09:00:00"}
    assert {o["end_local"][11:] for o in occ} == {"10:00:00"}
    assert occ[0]["start"][11:16] == before_utc
    assert occ[-1]["start"][11:16] == after_utc


def test_far_future_dst_offsets_are_correct(client, alice):
    series(client, alice, "MONTHLY", start="2026-01-01T09:00", end="2026-01-01T10:00", tz=NY)
    occ = list_events(client, alice, "2040-07-01", "2040-07-01", tz=NY)
    assert occ[0]["start"] == "2040-07-01T13:00:00Z"  # EDT, beyond pytz's 2037 table


def test_monthly_31st_skips_short_months(client, alice):
    series(client, alice, "MONTHLY", start="2026-01-31T09:00", end="2026-01-31T10:00", tz=NY)
    occ = list_events(client, alice, "2026-01-01", "2028-04-30", tz=NY)
    assert dates(occ) == [
        "2026-01-31", "2026-03-31", "2026-05-31", "2026-07-31", "2026-08-31", "2026-10-31", "2026-12-31",
        "2027-01-31", "2027-03-31", "2027-05-31", "2027-07-31", "2027-08-31", "2027-10-31", "2027-12-31",
        "2028-01-31", "2028-03-31",
    ]
    assert {o["start_local"][11:] for o in occ} == {"09:00:00"}


def test_monthly_29th_includes_leap_day_only(client, alice):
    series(client, alice, "MONTHLY", start="2027-01-29T09:00", end="2027-01-29T10:00")
    feb = lambda y: dates(list_events(client, alice, f"{y}-02-01", f"{y}-02-29" if y == 2028 else f"{y}-02-28"))
    assert feb(2027) == []
    assert feb(2028) == ["2028-02-29"]


@pytest.mark.parametrize("tz", ["UTC", "America/Los_Angeles", "Pacific/Auckland"])
def test_until_is_inclusive_and_final(client, alice, tz):
    series(client, alice, start="2026-05-01T22:00", end="2026-05-01T23:00", tz=tz, until="2026-05-05")
    occ = list_events(client, alice, "2026-04-01", "2026-06-30", tz=tz)
    assert dates(occ) == ["2026-05-01", "2026-05-02", "2026-05-03", "2026-05-04", "2026-05-05"]


def test_unbounded_series_reads_are_finite(client, alice):
    series(client, alice)
    assert len(list_events(client, alice, "2031-03-02", "2031-03-08")) == 7
    resp = client.get("/events", params={"start": "2026-01-01", "end": "2036-01-01"}, headers=alice)
    assert resp.status_code == 422
    resp = client.get("/events", params={"start": "2026-02-01", "end": "2026-01-01"}, headers=alice)
    assert resp.status_code == 422
    resp = client.get("/events", params={"start": "2026-01-01", "end": "2026-02-01", "tz": "Bad/Zone"}, headers=alice)
    assert resp.status_code == 422


@pytest.mark.parametrize("recurrence", [
    {"freq": "YEARLY"},
    {"freq": "HOURLY"},
    {"freq": "DAILY", "interval": 0},
    {"freq": "DAILY", "interval": -1},
    {"freq": "DAILY", "until": "2026-05-01"},           # before start
    {"freq": "DAILY", "until": "May 9"},
    {"freq": "DAILY", "count": 3},                      # unsupported field
    {"freq": "DAILY", "byday": "MO"},
    {"freq": "WEEKLY", "byday": "XX"},
    {"freq": "WEEKLY", "byday": "1MO"},
    {"freq": "MONTHLY", "bymonthday": "32"},
    {"freq": "WEEKLY", "byday": "MO", "until": "2026-05-04"},  # start Tue, no occurrence
])
def test_invalid_rules_are_rejected_without_writes(client, alice, recurrence):
    resp = client.post("/events", json={"title": "x", "start": "2026-05-05T09:00", "end": "2026-05-05T10:00",
                                        "recurrence": recurrence}, headers=alice)
    assert resp.status_code == 422, resp.text
    assert list_events(client, alice, "2026-01-01", "2026-12-31") == []


def test_invalid_timezone_for_series_is_rejected(client, alice):
    resp = client.post("/events", json={"title": "x", "start": "2026-05-05T09:00", "end": "2026-05-05T10:00",
                                        "timezone": "Europe/Atlantis", "recurrence": {"freq": "DAILY"}}, headers=alice)
    assert resp.status_code == 422
    assert list_events(client, alice) == []


def test_edit_single_occurrence_preserves_others(client, alice):
    ev = series(client, alice, tz=NY, until="2026-05-08")
    resp = client.put(f"/events/{ev['id']}/occurrence/2026-05-06",
                      json={"title": "Moved", "start": "2026-05-06T15:00", "end": "2026-05-06T15:45"}, headers=alice)
    assert resp.status_code == 200, resp.text
    assert resp.json()["occurrence"]["start_local"] == "2026-05-06T15:00:00"
    occ = list_events(client, alice, "2026-05-01", "2026-05-31", tz=NY)
    assert dates(occ) == ["2026-05-04", "2026-05-05", "2026-05-06", "2026-05-07", "2026-05-08"]
    moved = occ[2]
    assert (moved["title"], moved["start_local"], moved["end_local"], moved["is_exception"]) == \
        ("Moved", "2026-05-06T15:00:00", "2026-05-06T15:45:00", True)
    for o in occ[:2] + occ[3:]:
        assert (o["title"], o["start_local"][11:], o["is_exception"]) == ("Series", "09:00:00", False)

    # Title-only edit keeps the moved time.
    client.put(f"/events/{ev['id']}/occurrence/2026-05-06", json={"title": "Renamed"}, headers=alice)
    occ = list_events(client, alice, "2026-05-06", "2026-05-06", tz=NY)
    assert (occ[0]["title"], occ[0]["start_local"]) == ("Renamed", "2026-05-06T15:00:00")


def test_moved_occurrence_is_queried_by_effective_interval(client, alice):
    ev = series(client, alice, "WEEKLY", until="2026-06-30")
    client.put(f"/events/{ev['id']}/occurrence/2026-05-11",
               json={"start": "2026-07-20T09:00", "end": "2026-07-20T09:30"}, headers=alice)
    assert "2026-05-11" not in dates(list_events(client, alice, "2026-05-10", "2026-05-12"))
    july = list_events(client, alice, "2026-07-20", "2026-07-20")
    assert dates(july) == ["2026-05-11"]
    assert july[0]["start"] == "2026-07-20T09:00:00Z"


def test_invalid_occurrence_edits(client, alice):
    ev = series(client, alice, "WEEKLY", until="2026-05-31")  # Mondays
    sid = ev["id"]
    before = list_events(client, alice, "2026-05-01", "2026-05-31")
    assert client.put(f"/events/{sid}/occurrence/2026-05-11",
                      json={"start": "2026-05-11T10:00", "end": "2026-05-11T09:00"}, headers=alice).status_code == 422
    assert client.put(f"/events/{sid}/occurrence/2026-05-11", json={"start": "nope"}, headers=alice).status_code == 422
    assert client.put(f"/events/{sid}/occurrence/2026-05-12", json={"title": "x"}, headers=alice).status_code == 404
    assert client.put(f"/events/{sid}/occurrence/2026-06-01", json={"title": "x"}, headers=alice).status_code == 404
    assert client.put(f"/events/{sid}/occurrence/not-a-date", json={"title": "x"}, headers=alice).status_code == 422
    single = create(client, alice, title="One", start="2026-05-02T09:00", end="2026-05-02T10:00")["event"]
    assert client.put(f"/events/{single['id']}/occurrence/2026-05-02", json={"title": "x"},
                      headers=alice).status_code == 400
    assert [o for o in list_events(client, alice, "2026-05-01", "2026-05-31") if o["id"] == sid] == before


def test_delete_single_occurrence(client, alice):
    ev = series(client, alice, until="2026-05-08")
    sid = ev["id"]
    client.put(f"/events/{sid}/occurrence/2026-05-06",
               json={"start": "2026-05-20T09:00", "end": "2026-05-20T10:00"}, headers=alice)
    assert client.delete(f"/events/{sid}/occurrence/2026-05-06", headers=alice).status_code == 200
    assert client.delete(f"/events/{sid}/occurrence/2026-05-05", headers=alice).status_code == 200
    occ = list_events(client, alice, "2026-05-01", "2026-05-31")
    assert dates(occ) == ["2026-05-04", "2026-05-07", "2026-05-08"]  # moved copy gone too
    assert client.delete(f"/events/{sid}/occurrence/2026-05-05", headers=alice).status_code == 404
    assert client.put(f"/events/{sid}/occurrence/2026-05-05", json={"title": "back?"}, headers=alice).status_code == 404
    assert client.delete(f"/events/{sid}/occurrence/2026-05-09", headers=alice).status_code == 404
    assert dates(list_events(client, alice, "2026-05-01", "2026-05-31")) == ["2026-05-04", "2026-05-07", "2026-05-08"]


def test_whole_series_edit_applies_to_every_occurrence(client, alice):
    ev = series(client, alice, tz=NY, until="2026-05-08")
    sid = ev["id"]
    client.put(f"/events/{sid}/occurrence/2026-05-05",
               json={"title": "Custom", "start": "2026-05-05T15:00", "end": "2026-05-05T16:00"}, headers=alice)
    client.delete(f"/events/{sid}/occurrence/2026-05-07", headers=alice)

    resp = client.put(f"/events/{sid}", json={"title": "Standup", "start": "2026-05-04T10:00",
                                               "end": "2026-05-04T10:15"}, headers=alice)
    assert resp.status_code == 200, resp.text
    occ = list_events(client, alice, "2026-05-01", "2026-05-31", tz=NY)
    assert dates(occ) == ["2026-05-04", "2026-05-05", "2026-05-06", "2026-05-08"]  # deletion kept
    assert {(o["title"], o["start_local"][11:], o["end_local"][11:]) for o in occ} == {("Standup", "10:00:00", "10:15:00")}
    detail = client.get(f"/events/{sid}", headers=alice).json()
    assert detail["exceptions"] == [{"original_date": "2026-05-07", "is_deleted": True,
                                     "title": None, "start": None, "end": None}]


def test_whole_series_end_only_change(client, alice):
    ev = series(client, alice, until="2026-05-06")
    resp = client.put(f"/events/{ev['id']}/series", json={"end": "2026-05-04T11:00"}, headers=alice)
    assert resp.status_code == 200, resp.text
    occ = list_events(client, alice, "2026-05-01", "2026-05-31")
    assert [(o["start"], o["end"]) for o in occ] == [
        ("2026-05-04T09:00:00Z", "2026-05-04T11:00:00Z"),
        ("2026-05-05T09:00:00Z", "2026-05-05T11:00:00Z"),
        ("2026-05-06T09:00:00Z", "2026-05-06T11:00:00Z"),
    ]


def test_whole_series_rule_change_drops_stale_exceptions(client, alice):
    ev = series(client, alice)  # daily, unbounded
    sid = ev["id"]
    client.delete(f"/events/{sid}/occurrence/2026-05-05", headers=alice)   # Tuesday
    client.delete(f"/events/{sid}/occurrence/2026-05-11", headers=alice)   # Monday
    resp = client.put(f"/events/{sid}", json={"recurrence": {"freq": "WEEKLY", "until": "2026-05-25"}}, headers=alice)
    assert resp.status_code == 200, resp.text
    detail = resp.json()["event"]
    assert detail["recurrence"] == {"freq": "WEEKLY", "interval": 1, "until": "2026-05-25", "byday": None, "bymonthday": None}
    assert [e["original_date"] for e in detail["exceptions"]] == ["2026-05-11"]
    assert dates(list_events(client, alice, "2026-05-01", "2026-06-30")) == ["2026-05-04", "2026-05-18", "2026-05-25"]


def test_whole_series_timezone_change_keeps_until_date(client, alice):
    ev = series(client, alice, tz=NY, until="2026-05-06")
    resp = client.put(f"/events/{ev['id']}", json={"timezone": "Europe/London", "start": "2026-05-04T09:00",
                                                  "end": "2026-05-04T09:30"}, headers=alice)
    assert resp.status_code == 200, resp.text
    occ = list_events(client, alice, "2026-05-01", "2026-05-31", tz="Europe/London")
    assert dates(occ) == ["2026-05-04", "2026-05-05", "2026-05-06"]
    assert occ[0]["start"] == "2026-05-04T08:00:00Z"


@pytest.mark.parametrize("change", [
    {"end": "2026-05-04T08:00"},
    {"timezone": "Nowhere/Land"},
    {"recurrence": {"freq": "DAILY", "interval": 0}},
    {"recurrence": {"freq": "DAILY", "until": "2026-01-01"}},
    {"title": "  "},
    {"title": "ok", "from_date": "2026-05-06", "end": "2026-05-06T08:00"},
    {"title": "ok", "from_date": "2027-01-01"},  # after the series ends
])
def test_invalid_series_edits_change_nothing(client, alice, change):
    ev = series(client, alice, until="2026-05-10")
    sid = ev["id"]
    client.put(f"/events/{sid}/occurrence/2026-05-05", json={"title": "Custom"}, headers=alice)
    before_detail = client.get(f"/events/{sid}", headers=alice).json()
    before = list_events(client, alice)
    resp = client.put(f"/events/{sid}/series", json=change, headers=alice)
    assert resp.status_code == 422, resp.text
    if "from_date" not in change:
        assert client.put(f"/events/{sid}", json=change, headers=alice).status_code == 422
    assert client.get(f"/events/{sid}", headers=alice).json() == before_detail
    assert list_events(client, alice) == before


def test_split_series_from_date(client, alice):
    ev = series(client, alice, tz=NY, until="2026-05-10")
    sid = ev["id"]
    client.put(f"/events/{sid}/occurrence/2026-05-08", json={"title": "Friday special"}, headers=alice)
    client.delete(f"/events/{sid}/occurrence/2026-05-09", headers=alice)
    resp = client.put(f"/events/{sid}/series", json={"from_date": "2026-05-07", "start": "2026-05-07T14:00",
                                                      "end": "2026-05-07T14:30"}, headers=alice)
    assert resp.status_code == 200, resp.text
    new = resp.json()["event"]
    assert new["id"] != sid
    assert new["recurrence"]["until"] == "2026-05-10"
    assert client.get(f"/events/{sid}", headers=alice).json()["recurrence"]["until"] == "2026-05-06"
    occ = list_events(client, alice, "2026-05-01", "2026-05-31", tz=NY)
    assert [(o["original_date"], o["start_local"][11:16], o["title"]) for o in occ] == [
        ("2026-05-04", "09:00", "Series"), ("2026-05-05", "09:00", "Series"), ("2026-05-06", "09:00", "Series"),
        ("2026-05-07", "14:00", "Series"), ("2026-05-08", "14:00", "Friday special"), ("2026-05-10", "14:00", "Series"),
    ]


def test_delete_whole_series(client, alice):
    ev = series(client, alice)
    sid = ev["id"]
    client.put(f"/events/{sid}/occurrence/2026-05-05",
               json={"start": "2026-08-01T09:00", "end": "2026-08-01T10:00"}, headers=alice)
    assert client.delete(f"/events/{sid}", headers=alice).status_code == 200
    assert list_events(client, alice) == []
    assert client.get(f"/events/{sid}", headers=alice).status_code == 404
    assert client.delete(f"/events/{sid}/occurrence/2026-05-06", headers=alice).status_code == 404

    from database import SessionLocal
    from models import RecurrenceException, RecurrenceRule
    with SessionLocal() as db:
        assert db.query(RecurrenceRule).count() == 0
        assert db.query(RecurrenceException).count() == 0


def test_sparse_monthly_series_bounds_and_split(client, alice):
    # Every 5 months on the 31st: Jun, Nov 2026, Apr, Sep 2027 and Feb 2028 are
    # skipped, so the second occurrence is Jul 31 2028 (30 months later).
    ev = series(client, alice, "MONTHLY", start="2026-01-31T09:00", end="2026-01-31T10:00", interval=5,
                until="2031-12-31")
    occ = dates(list_events(client, alice, "2026-01-01", "2030-12-31"))
    assert occ[:2] == ["2026-01-31", "2028-07-31"]
    resp = client.put(f"/events/{ev['id']}/series", json={"from_date": "2026-02-01", "title": "Later"}, headers=alice)
    assert resp.status_code == 200, resp.text
    assert resp.json()["event"]["start"][:10] == occ[1]
    later = [o for o in list_events(client, alice, "2026-01-01", "2030-12-31") if o["title"] == "Later"]
    assert dates(later) == occ[1:]


def test_out_of_range_datetimes_are_rejected(client, alice):
    resp = client.post("/events", json={"title": "x", "start": "9999-12-31T23:00", "end": "9999-12-31T23:30",
                                        "timezone": "Pacific/Kiritimati"}, headers=alice)
    assert resp.status_code == 422
    resp = client.get("/events", params={"start": "9990-01-01", "end": "9990-02-01"}, headers=alice)
    assert resp.status_code == 422

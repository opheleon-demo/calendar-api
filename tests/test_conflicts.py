"""REQ-5: overlaps are saved and reported, only within the owner's calendar."""
import pytest

from conftest import create

NY = "America/New_York"
BASE = {"title": "Base", "start": "2026-05-04T10:00", "end": "2026-05-04T11:00"}


def titles(saved):
    return [c["title"] for c in saved["conflicts"]]


@pytest.mark.parametrize("start,end,conflict", [
    ("2026-05-04T10:30", "2026-05-04T11:30", True),    # partial overlap (end side)
    ("2026-05-04T09:30", "2026-05-04T10:30", True),    # partial overlap (start side)
    ("2026-05-04T10:15", "2026-05-04T10:45", True),    # contained
    ("2026-05-04T09:00", "2026-05-04T12:00", True),    # containing
    ("2026-05-04T10:00", "2026-05-04T11:00", True),    # equal
    ("2026-05-04T11:00", "2026-05-04T12:00", False),   # adjacent after
    ("2026-05-04T09:00", "2026-05-04T10:00", False),   # adjacent before
    ("2026-05-04T13:00", "2026-05-04T14:00", False),   # disjoint
])
def test_interval_relationships(client, alice, start, end, conflict):
    base = create(client, alice, **BASE)["event"]
    saved = create(client, alice, title="New", start=start, end=end)
    assert titles(saved) == (["Base"] if conflict else [])
    if conflict:
        c = saved["conflicts"][0]
        assert (c["event_id"], c["start"], c["end"]) == (base["id"], base["start"], base["end"])
        assert c["overlaps_start"] == saved["event"]["start"]


def test_conflicting_saves_are_persisted(client, alice):
    create(client, alice, **BASE)
    saved = create(client, alice, title="Overlap", start="2026-05-04T10:30", end="2026-05-04T11:30")
    assert titles(saved) == ["Base"]
    assert client.get(f"/events/{saved['event']['id']}", headers=alice).status_code == 200

    other = create(client, alice, title="Later", start="2026-05-04T15:00", end="2026-05-04T16:00")
    assert other["conflicts"] == []
    resp = client.put(f"/events/{other['event']['id']}",
                      json={"start": "2026-05-04T10:45", "end": "2026-05-04T11:15"}, headers=alice)
    assert resp.status_code == 200
    assert sorted(titles(resp.json())) == ["Base", "Overlap"]
    stored = client.get(f"/events/{other['event']['id']}", headers=alice).json()
    assert stored["start"] == "2026-05-04T10:45:00Z"


def test_edit_does_not_conflict_with_previous_version(client, alice):
    event = create(client, alice, **BASE)["event"]
    resp = client.put(f"/events/{event['id']}", json={"start": "2026-05-04T10:30", "end": "2026-05-04T11:30"},
                      headers=alice)
    assert resp.json()["conflicts"] == []
    ser = create(client, alice, title="Daily", start="2026-06-01T09:00", end="2026-06-01T10:00",
                 recurrence={"freq": "DAILY"})["event"]
    resp = client.put(f"/events/{ser['id']}", json={"end": "2026-06-01T10:30"}, headers=alice)
    assert resp.json()["conflicts"] == []


def test_recurring_occurrences_conflict_with_single_events(client, alice):
    ser = create(client, alice, title="Daily", start="2026-05-04T09:00", end="2026-05-04T10:00", timezone=NY,
                 recurrence={"freq": "DAILY", "until": "2026-12-31"})["event"]
    saved = create(client, alice, title="Meeting", start="2026-09-15T09:30", end="2026-09-15T10:30", timezone=NY)
    assert [(c["event_id"], c["original_date"], c["is_recurring"]) for c in saved["conflicts"]] == \
        [(ser["id"], "2026-09-15", True)]  # > 90 days after the series start

    after_end = create(client, alice, title="After", start="2027-01-01T09:30", end="2027-01-01T10:30", timezone=NY)
    assert after_end["conflicts"] == []
    adjacent = create(client, alice, title="Adjacent", start="2026-09-16T10:00", end="2026-09-16T11:00", timezone=NY)
    assert adjacent["conflicts"] == []


def test_new_series_reports_conflicts_beyond_90_days(client, alice):
    far = create(client, alice, title="Far", start="2027-08-02T09:15", end="2027-08-02T09:45")["event"]
    saved = create(client, alice, title="Daily", start="2026-05-04T09:00", end="2026-05-04T10:00",
                   recurrence={"freq": "DAILY"})
    assert [(c["event_id"], c["title"]) for c in saved["conflicts"]] == [(far["id"], "Far")]
    cov = saved["conflict_coverage"]
    assert cov["complete"] is True
    assert cov["start"] == "2026-05-04T09:00:00Z"
    assert cov["end"] >= "2027-08-02T09:45:00Z"


def test_unbounded_vs_unbounded_coverage_is_explicit(client, alice):
    create(client, alice, title="Weekly", start="2026-05-04T09:00", end="2026-05-04T10:00",
           recurrence={"freq": "WEEKLY"})
    saved = create(client, alice, title="Daily", start="2026-05-05T09:30", end="2026-05-05T10:30",
                   recurrence={"freq": "DAILY"})
    assert saved["conflicts"][0]["original_date"] == "2026-05-11"
    assert len(saved["conflicts"]) > 50  # every weekly occurrence in the window
    cov = saved["conflict_coverage"]
    assert cov["complete"] is False
    assert cov["start"] == "2026-05-05T09:30:00Z"
    assert cov["end"] == "2027-05-06T09:30:00Z"
    assert all(c["start"] < cov["end"] for c in saved["conflicts"])

    bounded = create(client, alice, title="Bounded", start="2026-05-05T09:30", end="2026-05-05T10:30",
                     recurrence={"freq": "DAILY", "until": "2026-05-20"})
    assert bounded["conflict_coverage"]["complete"] is True
    assert [c["original_date"] for c in bounded["conflicts"] if c["title"] == "Weekly"] == ["2026-05-11", "2026-05-18"]


def test_conflicts_respect_occurrence_exceptions(client, alice):
    ser = create(client, alice, title="Daily", start="2026-05-04T09:00", end="2026-05-04T10:00",
                 recurrence={"freq": "DAILY", "until": "2026-05-31"})["event"]
    sid = ser["id"]
    client.delete(f"/events/{sid}/occurrence/2026-05-06", headers=alice)
    client.put(f"/events/{sid}/occurrence/2026-05-07",
               json={"start": "2026-05-07T14:00", "end": "2026-05-07T15:00"}, headers=alice)

    assert create(client, alice, title="A", start="2026-05-06T09:00", end="2026-05-06T10:00")["conflicts"] == []
    assert create(client, alice, title="B", start="2026-05-07T09:00", end="2026-05-07T10:00")["conflicts"] == []
    moved = create(client, alice, title="C", start="2026-05-07T14:30", end="2026-05-07T15:30")
    assert [(c["event_id"], c["original_date"], c["start"]) for c in moved["conflicts"]] == \
        [(sid, "2026-05-07", "2026-05-07T14:00:00Z")]


def test_occurrence_edit_conflicts_exclude_only_itself(client, alice):
    ser = create(client, alice, title="Daily", start="2026-05-04T09:00", end="2026-05-04T10:00",
                 recurrence={"freq": "DAILY", "until": "2026-05-31"})["event"]
    sid = ser["id"]
    resp = client.put(f"/events/{sid}/occurrence/2026-05-05",
                      json={"start": "2026-05-05T09:30", "end": "2026-05-05T10:30"}, headers=alice)
    assert resp.json()["conflicts"] == []  # previous version of this occurrence is not a conflict

    resp = client.put(f"/events/{sid}/occurrence/2026-05-05",
                      json={"start": "2026-05-06T09:30", "end": "2026-05-06T10:30"}, headers=alice)
    assert [(c["event_id"], c["original_date"]) for c in resp.json()["conflicts"]] == [(sid, "2026-05-06")]
    assert resp.json()["occurrence"]["start"] == "2026-05-06T09:30:00Z"


def test_recurring_vs_recurring_with_dst_and_monthly_skips(client, alice):
    # 09:00 New York every day: 14:00Z before Mar 8 2026, 13:00Z after.
    create(client, alice, title="NY daily", start="2026-03-01T09:00", end="2026-03-01T10:00", timezone=NY,
           recurrence={"freq": "DAILY", "until": "2026-04-30"})
    before = create(client, alice, title="UTC before", start="2026-03-05T13:00", end="2026-03-05T13:59")
    after = create(client, alice, title="UTC after", start="2026-03-10T13:00", end="2026-03-10T13:59")
    assert before["conflicts"] == []
    assert titles(after) == ["NY daily"]

    monthly = create(client, alice, title="Monthly 31st", start="2026-01-31T09:00", end="2026-01-31T10:00",
                     timezone=NY, recurrence={"freq": "MONTHLY"})["event"]
    feb = create(client, alice, title="Feb 28", start="2026-02-28T09:00", end="2026-02-28T10:00", timezone=NY)
    assert feb["conflicts"] == []
    weekly = create(client, alice, title="Tuesdays", start="2026-03-03T09:30", end="2026-03-03T09:45",
                    timezone=NY, recurrence={"freq": "WEEKLY", "until": "2026-03-31"})
    hits = sorted(((c["title"], c["original_date"]) for c in weekly["conflicts"]), key=str)
    assert hits == [("Monthly 31st", "2026-03-31")] + [
        ("NY daily", d) for d in ("2026-03-03", "2026-03-10", "2026-03-17", "2026-03-24", "2026-03-31")
    ] + [("UTC after", None)]  # Tue Mar 10 09:30 EDT = 13:30Z
    assert all(c["event_id"] != monthly["id"] or c["original_date"] == "2026-03-31" for c in weekly["conflicts"])


def test_series_split_reports_conflicts(client, alice):
    ser = create(client, alice, title="Daily", start="2026-05-04T09:00", end="2026-05-04T10:00",
                 recurrence={"freq": "DAILY", "until": "2026-05-20"})["event"]
    create(client, alice, title="Lunch", start="2026-05-15T12:00", end="2026-05-15T13:00")
    resp = client.put(f"/events/{ser['id']}/series", json={"from_date": "2026-05-10", "start": "2026-05-10T12:30",
                                                          "end": "2026-05-10T13:30"}, headers=alice)
    assert resp.status_code == 200
    assert titles(resp.json()) == ["Lunch"]


def test_other_users_events_do_not_affect_conflicts(client, alice, bob):
    create(client, alice, **BASE)
    create(client, alice, title="Alice daily", start="2026-05-01T10:00", end="2026-05-01T11:00",
           recurrence={"freq": "DAILY"})
    saved = create(client, bob, title="Bob", start="2026-05-04T10:00", end="2026-05-04T11:00")
    assert saved["conflicts"] == []
    resp = client.put(f"/events/{saved['event']['id']}", json={"start": "2026-05-04T10:30", "end": "2026-05-04T11:30"},
                      headers=bob)
    assert resp.status_code == 200 and resp.json()["conflicts"] == []
    bob_series = create(client, bob, title="Bob daily", start="2026-05-01T10:00", end="2026-05-01T11:00",
                        recurrence={"freq": "DAILY"})
    assert titles(bob_series) == ["Bob"]
    assert bob_series["conflict_coverage"]["complete"] is True  # Alice's unbounded series is not considered

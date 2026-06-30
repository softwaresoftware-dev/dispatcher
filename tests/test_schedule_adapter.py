"""Tests for the schedule Event Source adapter — cron matching + the poll
window (cold start, due/not-due, catch-up coalescing, dedupe id stability).

The adapter reads the clock, so tests inject a fixed `now` (keyword seam) and a
`last_seen` ISO watermark — no real time passes.
"""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app.adapters import schedule
from app.event_sources import EventSource

UTC = timezone.utc


def _source(cron, name="morning-review", event_type=None, **kw):
    sched = {"cron": cron}
    if event_type is not None:
        sched["event_type"] = event_type
    return EventSource(name=name, system="schedule", raw={"schedule": sched}, **kw)


def _poll(source, last_seen, now):
    return asyncio.run(schedule.poll(source, last_seen, None, None, now=now))


def _at(y, mo, d, h, mi):
    return datetime(y, mo, d, h, mi, tzinfo=UTC)


# --------------------------- cron matcher ---------------------------

def test_cron_fields_exact():
    f = schedule.parse_cron("30 7 * * *")
    assert schedule.cron_matches(f, _at(2026, 6, 9, 7, 30))
    assert not schedule.cron_matches(f, _at(2026, 6, 9, 7, 31))
    assert not schedule.cron_matches(f, _at(2026, 6, 9, 8, 30))


def test_cron_step_and_list_and_range():
    f = schedule.parse_cron("*/15 9-17 * * *")        # every 15 min, 9am–5pm
    assert schedule.cron_matches(f, _at(2026, 6, 9, 9, 0))
    assert schedule.cron_matches(f, _at(2026, 6, 9, 17, 45))
    assert not schedule.cron_matches(f, _at(2026, 6, 9, 9, 7))
    assert not schedule.cron_matches(f, _at(2026, 6, 9, 18, 0))
    f2 = schedule.parse_cron("0 8,12,18 * * *")
    assert all(schedule.cron_matches(f2, _at(2026, 6, 9, h, 0)) for h in (8, 12, 18))
    assert not schedule.cron_matches(f2, _at(2026, 6, 9, 9, 0))


def test_cron_dow_names_and_sunday_aliasing():
    f = schedule.parse_cron("0 7 * * MON")            # 2026-06-08 is a Monday
    assert schedule.cron_matches(f, _at(2026, 6, 8, 7, 0))
    assert not schedule.cron_matches(f, _at(2026, 6, 9, 7, 0))  # Tuesday
    # 0 and 7 both mean Sunday; 2026-06-14 is a Sunday
    for dow in ("0", "7", "SUN"):
        assert schedule.cron_matches(schedule.parse_cron(f"0 7 * * {dow}"), _at(2026, 6, 14, 7, 0))


def test_cron_dom_dow_or_quirk():
    # When BOTH dom and dow are restricted, EITHER matches (classic cron).
    f = schedule.parse_cron("0 0 13 * FRI")
    assert schedule.cron_matches(f, _at(2026, 2, 13, 0, 0))   # the 13th (any weekday)
    assert schedule.cron_matches(f, _at(2026, 6, 12, 0, 0))   # a Friday (any date)
    assert not schedule.cron_matches(f, _at(2026, 6, 10, 0, 0))  # neither


def test_cron_aliases():
    assert schedule.parse_cron("@daily") == schedule.parse_cron("0 0 * * *")
    assert schedule.parse_cron("hourly") == schedule.parse_cron("0 * * * *")
    assert schedule.parse_cron("@weekly") == schedule.parse_cron("0 0 * * 0")


def test_bad_cron_raises():
    with pytest.raises(ValueError):
        schedule.parse_cron("0 7 * *")          # 4 fields
    with pytest.raises(ValueError):
        schedule.parse_cron("99 7 * * *")       # minute out of range


# --------------------------- poll window ---------------------------

def test_cold_start_emits_nothing_but_sets_baseline():
    events, meta = _poll(_source("0 7 * * *"), None, _at(2026, 6, 9, 7, 0))
    assert events == []
    assert meta["newest"]["created_at"] == _at(2026, 6, 9, 7, 0).isoformat()


def test_due_fires_one_event():
    src = _source("0 7 * * *")
    last = _at(2026, 6, 9, 6, 59).isoformat()
    events, _ = _poll(src, last, _at(2026, 6, 9, 7, 0))
    assert len(events) == 1
    ev = events[0]
    assert ev["event_type"] == "morning-review"
    assert ev["data"]["id"] == "morning-review:" + _at(2026, 6, 9, 7, 0).isoformat()
    assert ev["data"]["scheduled_for"] == _at(2026, 6, 9, 7, 0).isoformat()


def test_not_due_after_already_fired():
    src = _source("0 7 * * *")
    last = _at(2026, 6, 9, 7, 0).isoformat()       # already fired at 07:00
    events, _ = _poll(src, last, _at(2026, 6, 9, 7, 1))
    assert events == []


def test_catchup_coalesces_missed_runs():
    # Hourly schedule, poller was down 05:00 → 09:30: fire ONCE for the latest
    # matching minute (09:00), not once per missed hour.
    src = _source("0 * * * *")
    last = _at(2026, 6, 9, 5, 0).isoformat()
    events, _ = _poll(src, last, _at(2026, 6, 9, 9, 30))
    assert len(events) == 1
    assert events[0]["data"]["scheduled_for"] == _at(2026, 6, 9, 9, 0).isoformat()


def test_catchup_respects_cap():
    # Daily 07:00, last seen 4 days ago: only today's 07:00 is within the ~25h
    # cap; the older missed runs are not replayed.
    src = _source("0 7 * * *")
    last = _at(2026, 6, 5, 12, 0).isoformat()
    events, _ = _poll(src, last, _at(2026, 6, 9, 9, 30))
    assert len(events) == 1
    assert events[0]["data"]["scheduled_for"] == _at(2026, 6, 9, 7, 0).isoformat()


def test_event_type_defaults_to_name_and_is_overridable():
    e1, _ = _poll(_source("0 7 * * *", name="abc"),
                  _at(2026, 6, 9, 6, 59).isoformat(), _at(2026, 6, 9, 7, 0))
    assert e1[0]["event_type"] == "abc"
    e2, _ = _poll(_source("0 7 * * *", name="abc", event_type="custom"),
                  _at(2026, 6, 9, 6, 59).isoformat(), _at(2026, 6, 9, 7, 0))
    assert e2[0]["event_type"] == "custom"


def test_dedupe_id_disambiguates_same_minute_sources():
    last, now = _at(2026, 6, 9, 6, 59).isoformat(), _at(2026, 6, 9, 7, 0)
    a, _ = _poll(_source("0 7 * * *", name="a"), last, now)
    b, _ = _poll(_source("0 7 * * *", name="b"), last, now)
    assert a[0]["data"]["id"] != b[0]["data"]["id"]
    # but the same source at the same minute is stable (so the dedupe holds)
    a2, _ = _poll(_source("0 7 * * *", name="a"), last, now)
    assert a[0]["data"]["id"] == a2[0]["data"]["id"]


def test_missing_cron_raises():
    src = EventSource(name="x", system="schedule", raw={"schedule": {}})
    with pytest.raises(RuntimeError):
        _poll(src, _at(2026, 6, 9, 6, 59).isoformat(), _at(2026, 6, 9, 7, 0))

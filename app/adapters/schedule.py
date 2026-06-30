"""Schedule event-source adapter — emit a synthetic event when a cron is due.

Unlike HTTP adapters (github), this reads the clock instead of a remote feed.
The poller ticks ~every 60s; on each tick we check whether a cron-matching
minute fell in the window `(last_seen, now]`. If one did, we emit ONE synthetic
event for the latest such minute (coalescing missed runs within CATCHUP_CAP — a
daily that slept through 07:00 fires once on wake, a week of downtime does not
spam N runs). The cursor advances to `now` on every tick, so the only way the
window grows is the poller being down — which is exactly when catch-up should
happen.

Source declaration (per workspace, under .mindframe/dispatcher/event-sources/):

    name: morning-review
    system: schedule
    schedule:
      cron: "0 7 * * *"            # 5-field cron, or an @alias (see _ALIASES)
      event_type: morning-review   # optional; defaults to the source name

The emitted event routes like any other: `source: schedule, event_type: <type>`
in channels.yaml → `spawn:<recipe>`. Times are evaluated in the operator's local
timezone ("every morning" means local 07:00).
"""

from __future__ import annotations

from datetime import datetime, timedelta

# How far back a recovered poller will look for a missed run. A daily missed by
# under a day fires once on wake; longer outages don't replay history.
CATCHUP_CAP_MIN = 25 * 60

_DOW_NAMES = {"SUN": 0, "MON": 1, "TUE": 2, "WED": 3, "THU": 4, "FRI": 5, "SAT": 6}
_MON_NAMES = {"JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
              "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12}

# Standard cron @aliases (also accepted without the @). Friendly names with an
# opinionated time (e.g. "daily at 7am") live in the mindframe skills that WRITE
# these sources — the adapter stays standard.
_ALIASES = {
    "@yearly": "0 0 1 1 *", "@annually": "0 0 1 1 *",
    "@monthly": "0 0 1 * *",
    "@weekly": "0 0 * * 0",
    "@daily": "0 0 * * *", "@midnight": "0 0 * * *",
    "@hourly": "0 * * * *",
}


def _val(tok: str, lo: int, hi: int, names: dict | None) -> int:
    tok = tok.strip().upper()
    v = names[tok] if names and tok in names else int(tok)
    if not (lo <= v <= hi):
        raise ValueError(f"cron value {tok!r} out of range [{lo},{hi}]")
    return v


def _parse_part(part: str, lo: int, hi: int, names: dict | None) -> set[int]:
    step = 1
    base = part
    if "/" in part:
        base, step_s = part.split("/", 1)
        step = int(step_s)
        if step < 1:
            raise ValueError(f"cron step must be >= 1: {part!r}")
    if base == "*":
        start, end = lo, hi
    elif "-" in base:
        a, b = base.split("-", 1)
        start, end = _val(a, lo, hi, names), _val(b, lo, hi, names)
    else:
        start = end = _val(base, lo, hi, names)
    if start > end:
        raise ValueError(f"cron range start > end: {part!r}")
    return set(range(start, end + 1, step))


def _parse_field(field: str, lo: int, hi: int, names: dict | None = None) -> set[int]:
    out: set[int] = set()
    for part in field.split(","):
        out |= _parse_part(part, lo, hi, names)
    return out


def parse_cron(expr: str):
    """Compile a 5-field cron (or @alias) into matchable field sets. Raises
    ValueError on a malformed expression."""
    expr = (expr or "").strip()
    low = expr.lower()
    if low in _ALIASES:
        expr = _ALIASES[low]
    elif ("@" + low) in _ALIASES:  # bare hourly/daily/weekly/monthly/yearly
        expr = _ALIASES["@" + low]
    parts = expr.split()
    if len(parts) != 5:
        raise ValueError(f"cron must have 5 fields (or an @alias): {expr!r}")
    dow_raw, dom_raw = parts[4], parts[2]
    dows = _parse_field(dow_raw, 0, 7, _DOW_NAMES)
    dows = {0 if d == 7 else d for d in dows}  # 7 and 0 both mean Sunday
    return {
        "minute": _parse_field(parts[0], 0, 59),
        "hour": _parse_field(parts[1], 0, 23),
        "dom": _parse_field(dom_raw, 1, 31),
        "month": _parse_field(parts[3], 1, 12, _MON_NAMES),
        "dow": dows,
        "dom_restricted": dom_raw != "*",
        "dow_restricted": dow_raw != "*",
    }


def cron_matches(fields: dict, dt: datetime) -> bool:
    """True if `dt` (minute resolution) satisfies the compiled cron fields."""
    if dt.minute not in fields["minute"]:
        return False
    if dt.hour not in fields["hour"]:
        return False
    if dt.month not in fields["month"]:
        return False
    cron_dow = dt.isoweekday() % 7  # Mon=1..Sat=6, Sun=0 (cron numbering)
    dom_ok = dt.day in fields["dom"]
    dow_ok = cron_dow in fields["dow"]
    # Classic cron quirk: when BOTH day-of-month and day-of-week are restricted,
    # a match on EITHER fires; otherwise both must hold (the `*` one is total).
    if fields["dom_restricted"] and fields["dow_restricted"]:
        return dom_ok or dow_ok
    return dom_ok and dow_ok


def _latest_match(fields: dict, after: datetime, now: datetime) -> datetime | None:
    """The latest cron-matching minute m with `after < m <= now`, bounded by
    CATCHUP_CAP_MIN before now. None if no minute in the window matches."""
    m = now.replace(second=0, microsecond=0)
    floor = max(after + timedelta(minutes=1),
                now - timedelta(minutes=CATCHUP_CAP_MIN)).replace(second=0, microsecond=0)
    while m >= floor:
        if cron_matches(fields, m):
            return m
        m -= timedelta(minutes=1)
    return None


def _now() -> datetime:
    return datetime.now().astimezone().replace(second=0, microsecond=0)


def _parse_iso(s: str) -> datetime | None:
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


async def poll(source, last_seen, last_event_id, state, *, fetch=None, now=None):
    """Return (events, meta) per the adapter contract. `now` is an injection
    seam for tests; production leaves it unset and reads the local clock."""
    sched = (source.raw or {}).get("schedule") or {}
    cron = sched.get("cron")
    if not cron:
        raise RuntimeError(f"source {source.name}: schedule.cron is required")
    fields = parse_cron(cron)  # raises ValueError on a bad expression
    event_type = sched.get("event_type") or source.name

    now = (now or _now()).replace(second=0, microsecond=0)
    now_iso = now.isoformat()
    newest = {"id": now_iso, "created_at": now_iso}

    # Cold start (or an unparseable watermark): establish the baseline, fire
    # nothing. The poller watermarks from `newest`; subsequent ticks get a real
    # `last_seen` and the window opens.
    after = _parse_iso(last_seen) if last_seen else None
    if after is None:
        return [], {"state": None, "newest": newest}

    fired = _latest_match(fields, after, now)
    if fired is None:
        return [], {"state": None, "newest": newest}

    fid = fired.isoformat()
    # data.id disambiguates per-source: every schedule event shares
    # source="schedule", so the ingress dedupe key (source:data.id) must carry
    # the source name to keep two same-minute schedules from colliding.
    ev = {
        "event_type": event_type,
        "id": fid,
        "created_at": now_iso,
        "data": {
            "id": f"{source.name}:{fid}",
            "scheduled_for": fid,
            "cron": cron,
            "source": source.name,
        },
    }
    return [ev], {"state": None, "newest": newest}

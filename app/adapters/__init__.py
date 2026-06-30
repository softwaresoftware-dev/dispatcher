"""Event Source adapters — one per external `system`.

An adapter is an async callable:

    await poll(source: EventSource, last_seen: str|None,
               last_event_id: str|None, state: str|None) -> (events, meta)

`state` is an opaque per-source string the adapter asked the poller to persist
on its last fully-routed tick (GitHub: the ETag for If-None-Match).

`events` are normalized items newer than the cursor, ascending, each shaped:

    {
      "event_type": "<type>",      # matches the channels.yaml route event_type
      "id":         "<stable id>", # used for the ingress dedupe key + cursor tie-break
      "created_at": "<ISO8601>",   # the watermark field
      "data":       { ... },       # the event payload the recipe reads via {payload}
    }

`meta` is:

    {
      "state":  <str|None>,   # new opaque state; persisted only after a fully
                              #   routed tick (a partial route keeps the old
                              #   state so the unrouted tail is re-fetched)
      "newest": {"id", "created_at"} | None,
                              # newest RAW upstream item regardless of type /
                              #   watching filters — the poller watermarks from
                              #   this so unwatched traffic still advances the
                              #   cursor and cold starts always get one
    }

Adapters accept a keyword-only injection seam (`fetch=` for HTTP adapters) so
tests can feed canned responses. Production callers leave it unset.
"""

from app.adapters import github, schedule

_ADAPTERS = {
    "github": github.poll,
    "schedule": schedule.poll,
}


def get_adapter(system: str):
    """Return the poll() coroutine for a system, or None if unsupported."""
    return _ADAPTERS.get(system)

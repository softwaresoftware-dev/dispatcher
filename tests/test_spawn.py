"""Tests for static-spawn brief composition.

On the static path there is no LLM dispatcher to compose a brief, so the
channels.yaml route's `brief:` block fills the recipe template's
{{placeholders}}. A required placeholder with no override is an error —
the dispatcher must never spawn an agent missing its operating context.
"""

from app.spawn_helper import _compose_brief


def _compose(brief, overrides, optional=()):
    return _compose_brief(
        brief,
        overrides,
        event_id="evt-1",
        task_id="calendar-reader-evt-1",
        optional_keys=set(optional),
    )


def test_required_placeholder_filled_from_overrides():
    brief = {"context": {"output_path": "{{output_path}}", "window": "{{window}}"}}
    composed, err = _compose(
        brief, {"output_path": "/tmp/x.log", "window": "24h"}
    )
    assert err is None
    assert composed["context"] == {"output_path": "/tmp/x.log", "window": "24h"}


def test_unfilled_required_placeholder_is_an_error():
    brief = {"context": {"output_path": "{{output_path}}", "window": "{{window}}"}}
    composed, err = _compose(brief, {"output_path": "/tmp/x.log"})
    assert composed is None
    assert err is not None
    assert "window" in err
    assert "channels.yaml" in err


def test_unfilled_optional_placeholder_resolves_to_empty():
    brief = {"context": {"calendar_id": "{{calendar_id}}"}}
    composed, err = _compose(brief, {}, optional=("calendar_id",))
    assert err is None
    assert composed["context"]["calendar_id"] == ""


def test_event_id_substituted_into_override_values():
    brief = {"context": {"output_path": "{{output_path}}"}}
    composed, err = _compose(
        brief, {"output_path": "/tmp/calendar-agent-{event_id}.log"}
    )
    assert err is None
    assert composed["context"]["output_path"] == "/tmp/calendar-agent-evt-1.log"


def test_placeholder_inside_list_element_is_filled():
    brief = {"success_criteria": ["{{success_criteria}}"]}
    composed, err = _compose(brief, {"success_criteria": "file written"})
    assert err is None
    assert composed["success_criteria"] == ["file written"]


def test_no_placeholders_passes_through_unchanged():
    brief = {"context": {"window": "24h"}}
    composed, err = _compose(brief, {})
    assert err is None
    assert composed == brief


def test_non_string_override_preserves_type():
    brief = {"context": {"retries": "{{retries}}"}}
    composed, err = _compose(brief, {"retries": 3})
    assert err is None
    assert composed["context"]["retries"] == 3

"""ui.state callbacks without a browser: double clicks, stale previews, cache keys.

The callbacks only use st.session_state, so a plain dict stands in for it.
This drives cases AppTest cannot, such as clicking a confirm button that
was drawn for a preview that has since been applied or replaced.
"""

from types import SimpleNamespace

import pytest

from refine.quality import ValidityRule
from ui import state


@pytest.fixture
def ss(monkeypatch, sample_bytes):
    fake = SimpleNamespace(session_state={})
    monkeypatch.setattr(state, "st", fake)
    assert state.load_bytes("messy_customers.csv", sample_bytes)
    return fake.session_state


def preview(cols):
    state.request_preview("drop_columns", {"columns": cols})
    return state.get_preview()


def test_double_click_applies_once(ss):
    p = preview(["email"])
    state.confirm_apply(p.token)
    state.confirm_apply(p.token)
    assert len(state.current_session().steps) == 1
    assert ss[state.FLASH_KEY][0] == "warning" and "already applied" in ss[state.FLASH_KEY][1]


def test_button_for_replaced_preview_does_nothing(ss):
    old = preview(["email"])
    new = preview(["age"])
    state.confirm_apply(old.token)
    assert state.current_session().steps == ()
    assert "no longer current" in ss[state.FLASH_KEY][1]
    state.confirm_apply(new.token)
    assert state.current_session().columns.count("age") == 0


def test_preview_made_stale_by_undo_is_rejected_on_confirm(ss):
    first = preview(["email"])
    state.confirm_apply(first.token)
    p = preview(["age"])
    state.undo_last()
    state.confirm_apply(p.token)                           # Button still on screen from before undo
    assert state.current_session().steps == ()
    kind, message = ss[state.FLASH_KEY]
    assert kind == "error" and "Nothing was applied" in message and "changed since" in message
    assert state.PREVIEW_KEY not in ss


def test_get_preview_discards_stale_preview(ss):
    preview(["email"])
    session = state.current_session()
    session.apply(session.preview("drop_columns", {"columns": ["age"]}), confirmed=True)
    assert state.get_preview() is None
    assert state.PREVIEW_KEY not in ss
    assert "discarded" in ss[state.FLASH_KEY][1]


def test_new_upload_invalidates_preview(ss, sample_bytes):
    p = preview(["email"])
    old = state.current_session()
    state.load_bytes("again.csv", sample_bytes)
    state.confirm_apply(p.token)
    assert state.current_session() is not old and state.current_session().steps == ()
    assert old.steps == ()


def test_invalid_preview_request_flashes_error(ss):
    state.request_preview("drop_columns", {"columns": []})
    assert state.PREVIEW_KEY not in ss and ss[state.FLASH_KEY][0] == "error"


def test_undo_and_reset_with_nothing_to_do_flash_errors(ss):
    state.undo_last()
    assert ss[state.FLASH_KEY][0] == "error"
    state.reset_to_original()
    assert ss[state.FLASH_KEY][0] == "error"


def test_profile_cache_follows_data_state(ss):
    ds = state.get_dataset()
    p0 = state.get_profile(ds)
    assert state.get_profile(ds) is p0                     # No recompute without a change
    state.confirm_apply(preview(["email"]).token)
    p1 = state.get_profile(ds)
    assert p1 is not p0 and len(p1.columns) == 8
    state.undo_last()
    p2 = state.get_profile(ds)
    assert len(p2.columns) == 9 and p2 is not p1
    state.confirm_apply(preview(["age"]).token)
    state.reset_to_original()
    assert len(state.get_profile(ds).columns) == 9


def test_quality_cache_follows_rules_and_data(ss):
    ds = state.get_dataset()
    q0 = state.get_quality(ds)
    profile = state.get_profile(ds)
    assert state.get_quality(ds) is q0
    rule = ValidityRule("age", "range", minimum=0, maximum=50)
    state.set_rules(ds, state.get_rules(ds) + (rule,))
    q1 = state.get_quality(ds)
    assert q1 is not q0 and state.get_profile(ds) is profile        # Rules do not re-profile
    state.confirm_apply(preview(["age"]).token)
    q2 = state.get_quality(ds)
    assert q2 is not q1
    outcome = next(o for o in q2.not_assessed if o.column == "age")  # Rule on a dropped column
    assert "no column 'age'" in outcome.detail


def test_fingerprint_key_uses_current_state(ss):
    ds = state.get_dataset()
    before = state.fingerprint(ds)
    state.confirm_apply(preview(["email"]).token)
    assert state.fingerprint(ds) != before
    state.undo_last()
    assert state.fingerprint(ds) == before

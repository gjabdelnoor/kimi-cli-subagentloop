"""Decision-table tests for the review-loop Stop-hook gate.

The gate is the only piece of the loop that contains branching logic;
everything else (state schema, install file edits, prompt template) is
data-shaped and tested implicitly via these scenarios.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from kimi_cli.review_loop import gate as gate_mod
from kimi_cli.review_loop.state import (
    LoopState,
    load_loop_state,
    prompt_path,
    review_path,
    save_loop_state,
)


@pytest.fixture(autouse=True)
def _stub_session_todos(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Stub the session-state lookup so tests can set todos directly."""
    state: dict = {"todos": None}

    def fake_load(cwd: Path, session_id: str):
        return state["todos"]

    monkeypatch.setattr(gate_mod, "_load_session_todos", fake_load)
    return state


def _run_gate(
    payload: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[int, dict | None]:
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    out = io.StringIO()
    monkeypatch.setattr("sys.stdout", out)
    code = gate_mod.gate_main()
    raw = out.getvalue().strip()
    parsed = json.loads(raw) if raw else None
    return code, parsed


def _payload(cwd: Path, *, stop_hook_active: bool = False) -> dict:
    return {
        "hook_event_name": "Stop",
        "session_id": "test-session",
        "cwd": str(cwd),
        "stop_hook_active": stop_hook_active,
    }


def test_stop_hook_active_short_circuits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """If we're already inside a hook-re-driven turn, never block again."""
    code, out = _run_gate(_payload(tmp_path, stop_hook_active=True), monkeypatch)
    assert code == 0
    assert out is None


def test_todos_not_all_done_passes_silently(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _stub_session_todos: dict,
) -> None:
    _stub_session_todos["todos"] = [
        {"title": "a", "status": "done"},
        {"title": "b", "status": "in_progress"},
    ]
    code, out = _run_gate(_payload(tmp_path), monkeypatch)
    assert code == 0
    assert out is None


def test_empty_todo_list_passes_silently(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _stub_session_todos: dict,
) -> None:
    _stub_session_todos["todos"] = []
    code, out = _run_gate(_payload(tmp_path), monkeypatch)
    assert code == 0
    assert out is None


def test_first_iteration_all_done_requests_review(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _stub_session_todos: dict,
) -> None:
    _stub_session_todos["todos"] = [
        {"title": "a", "status": "done"},
        {"title": "b", "status": "done"},
    ]
    code, out = _run_gate(_payload(tmp_path), monkeypatch)
    assert code == 0
    assert out is not None
    reason = out["hookSpecificOutput"]["permissionDecisionReason"]
    assert "iteration 1" in reason
    assert "reviewer" in reason
    assert prompt_path(tmp_path, 1).exists()


def test_done_flag_short_circuits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _stub_session_todos: dict,
) -> None:
    _stub_session_todos["todos"] = [{"title": "a", "status": "done"}]
    save_loop_state(LoopState(done=True), tmp_path)
    code, out = _run_gate(_payload(tmp_path), monkeypatch)
    assert code == 0
    assert out is None


def test_accept_verdict_marks_done(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _stub_session_todos: dict,
) -> None:
    _stub_session_todos["todos"] = [{"title": "a", "status": "done"}]
    review_path(tmp_path, 1).write_text(
        "Review summary.\n\nVERDICT: Accept minor revisions\n", encoding="utf-8"
    )
    code, out = _run_gate(_payload(tmp_path), monkeypatch)
    assert code == 0
    assert out is not None
    reason = out["hookSpecificOutput"]["permissionDecisionReason"]
    assert "accepted" in reason.lower()
    state = load_loop_state(tmp_path)
    assert state.done is True


def test_non_accept_verdict_with_all_done_bumps_iteration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _stub_session_todos: dict,
) -> None:
    _stub_session_todos["todos"] = [{"title": "a", "status": "done"}]
    review_path(tmp_path, 1).write_text(
        "Issues found.\n\nVERDICT: Major revisions\n", encoding="utf-8"
    )
    code, out = _run_gate(_payload(tmp_path), monkeypatch)
    assert code == 0
    assert out is not None
    reason = out["hookSpecificOutput"]["permissionDecisionReason"]
    assert "iteration 2" in reason
    state = load_loop_state(tmp_path)
    assert state.iteration == 2
    assert prompt_path(tmp_path, 2).exists()


def test_iteration_ceiling_emits_surface_block(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _stub_session_todos: dict,
) -> None:
    _stub_session_todos["todos"] = [{"title": "a", "status": "done"}]
    save_loop_state(LoopState(iteration=5, max_iterations=5), tmp_path)
    # Pre-write a non-accept review so the gate tries to bump past the ceiling.
    review_path(tmp_path, 5).write_text(
        "More issues.\n\nVERDICT: Major revisions\n", encoding="utf-8"
    )
    code, out = _run_gate(_payload(tmp_path), monkeypatch)
    assert code == 0
    assert out is not None
    reason = out["hookSpecificOutput"]["permissionDecisionReason"]
    assert "ceiling" in reason.lower()
    state = load_loop_state(tmp_path)
    assert state.done is True


def test_malformed_review_asks_for_verdict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _stub_session_todos: dict,
) -> None:
    _stub_session_todos["todos"] = [{"title": "a", "status": "done"}]
    review_path(tmp_path, 1).write_text(
        "Lots of words but no verdict line at the end.\n", encoding="utf-8"
    )
    code, out = _run_gate(_payload(tmp_path), monkeypatch)
    assert code == 0
    assert out is not None
    reason = out["hookSpecificOutput"]["permissionDecisionReason"]
    assert "VERDICT" in reason


def test_unparseable_stdin_fails_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO("not-json"))
    out = io.StringIO()
    monkeypatch.setattr("sys.stdout", out)
    assert gate_mod.gate_main() == 0
    assert out.getvalue() == ""


def test_missing_cwd_fails_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    code, out = _run_gate({"hook_event_name": "Stop", "session_id": "x"}, monkeypatch)
    assert code == 0
    assert out is None


def test_resume_clause_appears_after_first_review(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _stub_session_todos: dict,
) -> None:
    """After iteration 1 stores reviewer_agent_id, iteration 2's
    instruction must include the ``resume=`` clause."""
    _stub_session_todos["todos"] = [{"title": "a", "status": "done"}]
    save_loop_state(LoopState(iteration=2, reviewer_agent_id="a12345678"), tmp_path)
    code, out = _run_gate(_payload(tmp_path), monkeypatch)
    assert code == 0
    assert out is not None
    reason = out["hookSpecificOutput"]["permissionDecisionReason"]
    assert 'resume="a12345678"' in reason

"""Stop-hook gate for the adversarial review loop.

Reads the Stop hook payload from stdin, decides whether the main agent
owes a review, owes follow-up work on a prior review, has accepted, or
should be left alone. Emits a ``hookSpecificOutput`` block when it wants
to re-drive the main agent; exits silently (code 0, no stdout) otherwise.

The full state machine is documented in
``docs/en/customization/review-loop.md``. In short:

1. If todos are not all "done", exit silently.
2. Else if no review file exists for the current iteration, build the
   reviewer prompt and emit a block telling the main agent to run the
   reviewer.
3. Else parse the verdict line. If it starts with "Accept", mark the
   loop done and emit a final block telling the main agent to report
   success.
4. Else bump the iteration counter (subject to the max-iterations
   ceiling) and request a fresh review of the new deliverable.

The gate is fail-open by design: any unexpected exception or missing
artifact results in exit code 0 with no block, so the user's session
never gets stuck inside the loop.
"""

from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path
from typing import Any

from kimi_cli.review_loop.state import (
    LoopState,
    files_manifest_path,
    load_loop_state,
    prompt_path,
    review_path,
    save_loop_state,
)


def gate_main() -> int:
    """Hook entry point. Reads stdin, prints stdout, returns exit code."""
    try:
        payload = _read_payload()
    except Exception:
        # Unparseable stdin → fail open.
        return 0
    try:
        return _gate(payload)
    except Exception:
        # Any internal error: print the traceback to stderr (visible in
        # ``kimi`` debug logs) and fail open so the user's session is
        # never blocked by a buggy hook.
        traceback.print_exc(file=sys.stderr)
        return 0


def _read_payload() -> dict[str, Any]:
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    data = json.loads(raw)
    if not isinstance(data, dict):
        return {}
    return data


def _gate(payload: dict[str, Any]) -> int:
    if payload.get("stop_hook_active"):
        return 0

    cwd_str = payload.get("cwd")
    session_id = payload.get("session_id")
    if not cwd_str or not session_id:
        return 0
    cwd = Path(cwd_str)

    state = load_loop_state(cwd)
    if state.done:
        return 0

    if state.iteration > state.max_iterations:
        state.done = True
        save_loop_state(state, cwd)
        _emit_block(
            f"Review-loop iteration ceiling ({state.max_iterations}) reached without "
            "an Accept verdict. Summarise the latest reviewer findings to the user "
            "and ask whether to continue manually. Do not call the reviewer again."
        )
        return 0

    todos = _load_session_todos(cwd, session_id)
    if todos is None:
        return 0
    if not todos or not all(t.get("status") == "done" for t in todos):
        return 0

    current_review = review_path(cwd, state.iteration)
    if not current_review.exists():
        return _request_review(cwd, state)

    verdict = _parse_verdict(current_review)
    if verdict is None:
        # Review file exists but no VERDICT line was found. Treat as a
        # malformed review and ask the main agent to fix it rather than
        # silently bumping forward.
        _emit_block(
            f"The review at {current_review} does not end with a 'VERDICT: ...' "
            "line. Re-read the reviewer's output, edit the file to ensure the "
            "last non-empty line is exactly one of: 'VERDICT: Accept minor "
            "revisions', 'VERDICT: Major revisions', 'VERDICT: Fundamental "
            "rethink', 'VERDICT: Destroy and restart'. Then end your turn."
        )
        return 0

    if verdict.startswith("Accept"):
        state.done = True
        save_loop_state(state, cwd)
        _emit_block(
            f"Reviewer accepted iteration {state.iteration} with verdict "
            f"'{verdict}'. Summarise the deliverable to the user and conclude. "
            "Do not invoke the reviewer again."
        )
        return 0

    # Non-Accept verdict + all todos done = the main agent finished
    # working on the issues raised in the previous review. Advance to
    # the next iteration and request a fresh review.
    state.iteration += 1
    save_loop_state(state, cwd)
    if state.iteration > state.max_iterations:
        state.done = True
        save_loop_state(state, cwd)
        _emit_block(
            f"Review-loop iteration ceiling ({state.max_iterations}) reached. "
            "Summarise the latest reviewer findings to the user and ask whether "
            "to continue manually."
        )
        return 0
    return _request_review(cwd, state)


def _request_review(cwd: Path, state: LoopState) -> int:
    iteration = state.iteration
    p_path = prompt_path(cwd, iteration)
    r_path = review_path(cwd, iteration)
    manifest = files_manifest_path(cwd, iteration)
    prev_review = review_path(cwd, iteration - 1) if iteration > 1 else None

    if not p_path.exists():
        p_path.write_text(_build_prompt(iteration, manifest, prev_review), encoding="utf-8")

    resume_clause = f'resume="{state.reviewer_agent_id}", ' if state.reviewer_agent_id else ""
    record_id_clause = (
        ""
        if state.reviewer_agent_id
        else (
            "\n4. After the Agent call returns, extract the `agent_id` line from its output "
            "and store it by editing `.kimi/review-loop/state.json` so the field "
            '`"reviewer_agent_id"` becomes the returned id. This lets future iterations '
            "resume the same reviewer context."
        )
    )

    instruction = (
        f"The TODO list for **iteration {iteration}** is complete. A reviewer "
        f"subagent must now audit it.\n\n"
        f"1. Read the contents of `{p_path}` into a variable, then call:\n"
        f"   ```\n"
        f"   Agent(\n"
        f'     subagent_type="reviewer",\n'
        f"     timeout=9999,\n"
        f'     {resume_clause}description="Review iteration {iteration}",\n'
        f"     prompt=<contents of {p_path}>\n"
        f"   )\n"
        f"   ```\n"
        f"2. Write the reviewer's full response **verbatim** to `{r_path}`. The "
        f"last non-empty line MUST be `VERDICT: <one of: Accept minor revisions "
        f"| Major revisions | Fundamental rethink | Destroy and restart>`. If "
        f"the reviewer's output is missing that line, append it based on the "
        f"reviewer's intent.\n"
        f"3. If the verdict begins with 'Accept', end your turn — the hook will "
        f"close the loop. Otherwise, call `SetTodoList` with new items, "
        f"**verbatim-quoting each reviewer flag in the title of the TODO item "
        f"that addresses it** (Critical → Major → Minor → Taste order). Then "
        f"work the new TODO list to completion.{record_id_clause}\n\n"
        f"While working, append every file path you create or modify (one per "
        f"line, absolute paths) to `{manifest}` so the next reviewer can find "
        f"them without depending on git."
    )
    _emit_block(instruction)
    return 0


def _build_prompt(iteration: int, manifest: Path, prev_review: Path | None) -> str:
    lines: list[str] = []
    lines.append(f"# Iteration {iteration} — reviewer prompt\n")
    lines.append(
        "You have been invoked by the adversarial review loop. The reviewer "
        "role prompt is already in your system prompt; do not re-read it.\n"
    )
    lines.append("## Files modified during this iteration\n")
    if manifest.exists():
        files_text = manifest.read_text(encoding="utf-8").strip()
        if files_text:
            lines.append("```\n" + files_text + "\n```\n")
        else:
            lines.append("_(manifest file exists but is empty — investigate.)_\n")
    else:
        lines.append(
            f"_No manifest at `{manifest}`. The main agent was supposed to "
            "record every file it touched. Treat this as a flag: enumerate the "
            "working directory yourself and report which files changed._\n"
        )
    lines.append("## Your task\n")
    lines.append(
        f"Execute the review protocol on iteration {iteration}. Read the "
        "load-bearing artifacts before the narrative. Cross-reference every "
        "claim. End your response with a single `VERDICT: ...` line whose "
        "value is exactly one of the four options.\n"
    )
    if prev_review is not None and prev_review.exists():
        lines.append("## Previous iteration: your verdict\n")
        lines.append(
            "Your previous review of the previous iteration is below. The "
            "main agent should have addressed every flag you raised. Verify "
            "they did.\n\n"
        )
        lines.append("```\n" + prev_review.read_text(encoding="utf-8") + "\n```\n")
    return "\n".join(lines)


def _parse_verdict(path: Path) -> str | None:
    """Return the verdict string from the last 'VERDICT:' line, or None."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    for line in reversed(text.splitlines()):
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("VERDICT:"):
            return stripped[len("VERDICT:") :].strip()
        # Only the LAST non-empty line counts; if it isn't a VERDICT line,
        # the review is malformed.
        return None
    return None


def _load_session_todos(cwd: Path, session_id: str) -> list[dict[str, Any]] | None:
    """Return the parsed todos list for ``session_id`` rooted at ``cwd``.

    Returns ``None`` on any error (treated as "do not act"). The import is
    done here, lazily, so this module is cheap to import for tests that
    monkeypatch ``_load_session_todos`` directly.
    """
    try:
        from kaos.path import KaosPath

        from kimi_cli.metadata import load_metadata
        from kimi_cli.session_state import load_session_state
    except Exception:
        return None

    try:
        work_dir = KaosPath.unsafe_from_local_path(cwd).canonical()
        metadata = load_metadata()
        work_dir_meta = metadata.get_work_dir_meta(work_dir)
        if work_dir_meta is None:
            return None
        session_dir = work_dir_meta.sessions_dir / session_id
        if not session_dir.is_dir():
            return None
        state = load_session_state(session_dir)
    except Exception:
        return None
    return [t.model_dump() for t in state.todos]


def _emit_block(reason: str) -> None:
    """Emit the JSON envelope that re-drives the main agent."""
    payload = {
        "hookSpecificOutput": {
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }
    print(json.dumps(payload))


if __name__ == "__main__":
    sys.exit(gate_main())

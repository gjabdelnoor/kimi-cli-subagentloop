"""Persistent state for the review loop.

One JSON file per session, kept in ``<cwd>/.kimi/review-loop/state.json``.
This is the only mutable state the loop owns; iteration prompts and
reviews live next to it as ``v{n}-prompt.md`` / ``v{n}-review.md``.
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, Field, ValidationError

DEFAULT_MAX_ITERATIONS = 5
DIR_NAME = ".kimi/review-loop"
STATE_FILE_NAME = "state.json"


class LoopState(BaseModel):
    """Tiny state record. Everything else lives in the iteration files."""

    version: int = 1
    iteration: int = Field(default=1, ge=1)
    """The iteration the main agent is currently working on or just finished."""
    max_iterations: int = Field(default=DEFAULT_MAX_ITERATIONS, ge=1, le=100)
    """Hard ceiling — the loop surfaces to the user when this is reached."""
    reviewer_agent_id: str | None = None
    """The reviewer subagent's id, recorded by the main agent after the first
    Agent(subagent_type="reviewer", ...) call so subsequent iterations can
    ``resume=`` it."""
    done: bool = False
    """Sticky: once the reviewer accepts (or the ceiling is hit), the gate
    exits immediately on every subsequent fire until the user clears it."""


def review_loop_dir(cwd: Path) -> Path:
    """Return ``<cwd>/.kimi/review-loop``, creating it if necessary."""
    path = cwd / DIR_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def state_path(cwd: Path) -> Path:
    return review_loop_dir(cwd) / STATE_FILE_NAME


def load_loop_state(cwd: Path) -> LoopState:
    """Load loop state, falling back to defaults on missing or corrupt file."""
    path = state_path(cwd)
    if not path.exists():
        return LoopState()
    try:
        return LoopState.model_validate_json(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, ValidationError, UnicodeDecodeError, OSError):
        # Fail-open: a corrupt state file should not break the user's session.
        # The gate will write fresh defaults on the next save.
        return LoopState()


def save_loop_state(state: LoopState, cwd: Path) -> None:
    """Atomically write loop state to disk."""
    path = state_path(cwd)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(state.model_dump_json(indent=2), encoding="utf-8")
    tmp.replace(path)


def prompt_path(cwd: Path, iteration: int) -> Path:
    return review_loop_dir(cwd) / f"v{iteration}-prompt.md"


def review_path(cwd: Path, iteration: int) -> Path:
    return review_loop_dir(cwd) / f"v{iteration}-review.md"


def files_manifest_path(cwd: Path, iteration: int) -> Path:
    """Per-iteration list of files modified by the main agent.

    The main agent is instructed to write absolute paths here, one per
    line. The reviewer prompt includes this list so the reviewer knows
    where to look without depending on git.
    """
    return review_loop_dir(cwd) / f"v{iteration}-files.txt"

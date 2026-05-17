"""Adversarial review loop for autonomous deliverables.

This package wires Kimi's Stop hook to a reviewer subagent so that every
completed TODO list is audited by a structurally-independent reviewer
before the main agent moves on.

Public entry points:

- :func:`gate_main` — the Stop-hook gate. Reads the hook payload from
  stdin and decides whether to re-drive the main agent into running the
  reviewer, accepting the iteration, or surfacing the result to the user.
- :func:`install_hook` / :func:`uninstall_hook` — idempotent edits to
  ``~/.kimi/config.toml`` that turn the loop on or off for the user.

The loop's design goals are validity (the reviewer cannot inherit the
main agent's bias), QC (every state transition is explicit and visible
on disk), and maintainability (one state file, one prompt file per
iteration, no git or VCS coupling).
"""

from kimi_cli.review_loop.gate import gate_main
from kimi_cli.review_loop.install import install_hook, uninstall_hook
from kimi_cli.review_loop.state import (
    LoopState,
    load_loop_state,
    review_loop_dir,
    save_loop_state,
)

__all__ = [
    "LoopState",
    "gate_main",
    "install_hook",
    "load_loop_state",
    "review_loop_dir",
    "save_loop_state",
    "uninstall_hook",
]

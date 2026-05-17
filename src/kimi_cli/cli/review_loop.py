"""``kimi review-loop`` subcommand: enable / disable / status."""

from __future__ import annotations

import typer

from kimi_cli.config import get_config_file
from kimi_cli.review_loop.install import (
    HOOK_COMMAND_MARKER,
    install_hook,
    uninstall_hook,
)
from kimi_cli.review_loop.state import load_loop_state, review_loop_dir

cli = typer.Typer(
    help=(
        "Manage the adversarial review loop. Enables a Stop hook that "
        "automatically invokes a fresh reviewer subagent after the main "
        "agent finishes its TODO list, then feeds the reviewer's flags "
        "back into the next iteration's TODO list. See "
        "docs/customization/review-loop for the full design."
    ),
    no_args_is_help=True,
)


@cli.command()
def enable() -> None:
    """Install the review-loop Stop hook into ``~/.kimi/config.toml``."""
    path = get_config_file()
    result = install_hook(path)
    if result.action == "installed":
        typer.echo(f"Review loop enabled. Added Stop hook to {path}.")
        typer.echo(f"  command = {result.command}")
        typer.echo("Start (or restart) `kimi` in your project to pick up the new hook.")
    else:
        typer.echo(f"Review loop already enabled in {path}.")
        typer.echo(f"  command = {result.command}")


@cli.command()
def disable() -> None:
    """Remove the review-loop Stop hook from ``~/.kimi/config.toml``."""
    path = get_config_file()
    result = uninstall_hook(path)
    if result.action == "uninstalled":
        typer.echo(f"Review loop disabled. Removed Stop hook from {path}.")
    else:
        typer.echo(f"Review loop hook not present in {path}; nothing to do.")


@cli.command()
def status(
    cwd: str = typer.Option(
        ".",
        "--cwd",
        help="Project directory whose review-loop state to inspect.",
    ),
) -> None:
    """Show whether the loop is enabled and the current loop state."""
    from pathlib import Path

    config_path = get_config_file()
    if config_path.exists():
        text = config_path.read_text(encoding="utf-8")
        installed = HOOK_COMMAND_MARKER in text
    else:
        installed = False
    typer.echo(f"Hook installed: {installed}")
    typer.echo(f"  config: {config_path}")

    cwd_path = Path(cwd).resolve()
    state = load_loop_state(cwd_path)
    typer.echo("")
    typer.echo(f"Loop state for {cwd_path}:")
    typer.echo(f"  dir:               {review_loop_dir(cwd_path)}")
    typer.echo(f"  iteration:         {state.iteration}")
    typer.echo(f"  max_iterations:    {state.max_iterations}")
    typer.echo(f"  reviewer_agent_id: {state.reviewer_agent_id or '(unset)'}")
    typer.echo(f"  done:              {state.done}")


@cli.command()
def reset(
    cwd: str = typer.Option(
        ".", "--cwd", help="Project directory whose review-loop state to reset."
    ),
    confirm: bool = typer.Option(False, "--yes", help="Skip the confirmation prompt."),
) -> None:
    """Clear the loop state file for this project.

    Use this after the loop has run to completion if you want to start a
    fresh iteration cycle without uninstalling the hook. Iteration files
    (``v{n}-prompt.md``, ``v{n}-review.md``, ``v{n}-files.txt``) are left
    on disk for inspection — delete them manually if you want a clean
    slate.
    """
    from pathlib import Path

    from kimi_cli.review_loop.state import state_path

    cwd_path = Path(cwd).resolve()
    target = state_path(cwd_path)
    if not target.exists():
        typer.echo(f"No state file at {target}; nothing to do.")
        return
    if not confirm and not typer.confirm(f"Delete {target}?"):
        typer.echo("Aborted.")
        raise typer.Exit(code=1)
    target.unlink()
    typer.echo(f"Removed {target}.")

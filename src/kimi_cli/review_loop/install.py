"""Idempotent install/uninstall of the review-loop Stop hook in config.toml.

The hook entry is identified by its ``command`` containing the marker
``__review-loop-gate`` so we can find and remove it without trampling
on other Stop hooks the user has configured.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

import tomlkit
from tomlkit.items import AoT, Table

HOOK_COMMAND_MARKER = "__review-loop-gate"
HOOK_TIMEOUT_S = 30


def _hook_command() -> str:
    """Return the absolute command string to invoke the gate.

    We resolve the kimi executable so the hook works even when launched
    from a shell whose PATH differs from the one kimi was installed in
    (a common gotcha with ``uv tool install``).
    """
    kimi = shutil.which("kimi") or "kimi"
    return f'"{kimi}" {HOOK_COMMAND_MARKER}'


@dataclass(frozen=True, slots=True, kw_only=True)
class InstallResult:
    config_path: Path
    action: str  # "installed" | "already-present" | "uninstalled" | "not-present"
    command: str


def install_hook(config_path: Path) -> InstallResult:
    """Add the review-loop Stop hook to ``config_path``. Idempotent."""
    doc = _load_or_create(config_path)
    hooks = _get_hooks_array(doc)
    cmd = _hook_command()

    for hook in hooks:
        if HOOK_COMMAND_MARKER in str(hook.get("command", "")):
            return InstallResult(
                config_path=config_path,
                action="already-present",
                command=str(hook.get("command", "")),
            )

    entry = tomlkit.table()
    entry["event"] = "Stop"
    entry["command"] = cmd
    entry["timeout"] = HOOK_TIMEOUT_S
    entry.comment("Adversarial review loop — see `kimi review-loop --help`")
    hooks.append(entry)
    _write(config_path, doc)
    return InstallResult(config_path=config_path, action="installed", command=cmd)


def uninstall_hook(config_path: Path) -> InstallResult:
    """Remove the review-loop Stop hook. Safe to call when not installed."""
    if not config_path.exists():
        return InstallResult(config_path=config_path, action="not-present", command="")
    doc = tomlkit.parse(config_path.read_text(encoding="utf-8"))
    hooks = _get_hooks_array(doc)
    removed: str | None = None
    keep: list[Table] = []
    for hook in hooks:
        if HOOK_COMMAND_MARKER in str(hook.get("command", "")):
            removed = str(hook.get("command", ""))
            continue
        keep.append(hook)
    if removed is None:
        return InstallResult(config_path=config_path, action="not-present", command="")
    # tomlkit doesn't expose a clean "replace AoT" API; rebuild by
    # clearing and re-appending the survivors.
    while len(hooks) > 0:
        hooks.pop(0)
    for h in keep:
        hooks.append(h)
    _write(config_path, doc)
    return InstallResult(config_path=config_path, action="uninstalled", command=removed)


def _load_or_create(config_path: Path) -> tomlkit.TOMLDocument:
    if config_path.exists():
        return tomlkit.parse(config_path.read_text(encoding="utf-8"))
    config_path.parent.mkdir(parents=True, exist_ok=True)
    return tomlkit.document()


def _get_hooks_array(doc: tomlkit.TOMLDocument) -> AoT:
    """Return the ``[[hooks]]`` array, creating it if missing."""
    hooks = doc.get("hooks")
    if hooks is None:
        aot = tomlkit.aot()
        doc["hooks"] = aot
        return aot
    if isinstance(hooks, AoT):
        return hooks
    # Edge case: user has a malformed `hooks = ...` scalar. Replace it.
    aot = tomlkit.aot()
    doc["hooks"] = aot
    return aot


def _write(config_path: Path, doc: tomlkit.TOMLDocument) -> None:
    tmp = config_path.with_suffix(config_path.suffix + ".tmp")
    tmp.write_text(tomlkit.dumps(doc), encoding="utf-8")
    tmp.replace(config_path)

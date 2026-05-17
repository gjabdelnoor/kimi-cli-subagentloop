"""Tests for the ``kimi review-loop enable/disable`` config writer."""

from __future__ import annotations

from pathlib import Path

import tomlkit

from kimi_cli.review_loop.install import (
    HOOK_COMMAND_MARKER,
    install_hook,
    uninstall_hook,
)


def test_install_creates_config_if_missing(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    result = install_hook(config)
    assert result.action == "installed"
    assert config.exists()
    doc = tomlkit.parse(config.read_text(encoding="utf-8"))
    hooks = doc["hooks"]
    assert len(hooks) == 1
    assert hooks[0]["event"] == "Stop"
    assert HOOK_COMMAND_MARKER in hooks[0]["command"]


def test_install_is_idempotent(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    install_hook(config)
    result = install_hook(config)
    assert result.action == "already-present"
    doc = tomlkit.parse(config.read_text(encoding="utf-8"))
    assert len(doc["hooks"]) == 1  # not duplicated


def test_install_preserves_existing_hooks(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text(
        '[[hooks]]\nevent = "PreToolUse"\ncommand = "echo hi"\ntimeout = 5\n',
        encoding="utf-8",
    )
    install_hook(config)
    doc = tomlkit.parse(config.read_text(encoding="utf-8"))
    assert len(doc["hooks"]) == 2
    commands = [h["command"] for h in doc["hooks"]]
    assert "echo hi" in commands
    assert any(HOOK_COMMAND_MARKER in c for c in commands)


def test_uninstall_removes_only_review_loop_hook(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text(
        '[[hooks]]\nevent = "PreToolUse"\ncommand = "echo hi"\ntimeout = 5\n',
        encoding="utf-8",
    )
    install_hook(config)
    result = uninstall_hook(config)
    assert result.action == "uninstalled"
    doc = tomlkit.parse(config.read_text(encoding="utf-8"))
    assert len(doc["hooks"]) == 1
    assert doc["hooks"][0]["command"] == "echo hi"


def test_uninstall_when_not_present(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    result = uninstall_hook(config)
    assert result.action == "not-present"
    config.write_text("", encoding="utf-8")
    result = uninstall_hook(config)
    assert result.action == "not-present"

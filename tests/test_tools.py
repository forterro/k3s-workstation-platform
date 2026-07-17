"""Tests for CLI tool detection."""

from __future__ import annotations

import pytest

from workstation_bootstrap import tools


def _all_present(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tools.shutil, "which", lambda name: f"/usr/local/bin/{name}")


def test_tools_needing_install_all_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tools.shutil, "which", lambda name: None)
    pending = tools.tools_needing_install()
    assert {tool.name for tool, _ in pending} == {tool.name for tool in tools.TOOLS}
    assert all(status == "missing" for _, status in pending)


def test_tools_needing_install_all_current(monkeypatch: pytest.MonkeyPatch) -> None:
    _all_present(monkeypatch)
    monkeypatch.setattr(
        tools, "_installed_version", lambda name: tools.pinned_version(name).lstrip("v")
    )
    assert tools.tools_needing_install() == []


def test_tools_needing_install_outdated(monkeypatch: pytest.MonkeyPatch) -> None:
    _all_present(monkeypatch)
    monkeypatch.setattr(tools, "_installed_version", lambda name: "0.0.1")
    pending = tools.tools_needing_install()
    assert {status for _, status in pending} == {"outdated"}


def test_tools_needing_install_unknown_version_left_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    _all_present(monkeypatch)
    monkeypatch.setattr(tools, "_installed_version", lambda name: None)
    assert tools.tools_needing_install() == []


def test_age_key_path_respects_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SOPS_AGE_KEY_FILE", "/tmp/custom/keys.txt")
    assert str(tools.age_key_path()) == "/tmp/custom/keys.txt"


def test_every_tool_has_pinned_version() -> None:
    for tool in tools.TOOLS:
        assert tools.pinned_version(tool.name)


def test_pinned_version_unknown_tool_raises() -> None:
    with pytest.raises(tools.ToolsError):
        tools.pinned_version("does-not-exist")

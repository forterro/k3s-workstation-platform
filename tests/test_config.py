"""Tests for repository-root detection and value-file layering."""

from __future__ import annotations

from pathlib import Path

from workstation_bootstrap.config import find_repo_root, value_files


def _write(path: Path, content: str = "{}\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_find_repo_root_by_pyproject(tmp_path: Path) -> None:
    _write(tmp_path / "pyproject.toml", "[project]\n")
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)

    assert find_repo_root(nested) == tmp_path


def test_find_repo_root_fallback_to_start(tmp_path: Path) -> None:
    # No marker anywhere up to tmp_path; returns the resolved start directory.
    assert find_repo_root(tmp_path) == tmp_path.resolve()


def test_value_files_ordering(tmp_path: Path) -> None:
    base = tmp_path / "umbrella-charts" / "core-stack" / "traefik" / "values.yaml"
    glob = tmp_path / "config" / "values.yaml"
    brick = tmp_path / "config" / "core-stack" / "traefik" / "values.yaml"
    for path in (base, glob, brick):
        _write(path)

    resolved = value_files(tmp_path, "core-stack", "traefik")

    assert resolved == [base, glob, brick]


def test_value_files_only_existing(tmp_path: Path) -> None:
    base = tmp_path / "umbrella-charts" / "core-stack" / "traefik" / "values.yaml"
    _write(base)

    resolved = value_files(tmp_path, "core-stack", "traefik")

    assert resolved == [base]


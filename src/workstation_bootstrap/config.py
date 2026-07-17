"""Repository paths and value-file layering for the workstation.

The generator runs from the cloned repository. Optional value overrides live in a local ``config/``
directory next to the base umbrella charts.
"""

from __future__ import annotations

from pathlib import Path


def find_repo_root(start: Path | None = None) -> Path:
    """Return the repository root, detected by walking up to a pyproject.toml or .git marker."""
    current = (start or Path.cwd()).resolve()
    for candidate in [current, *current.parents]:
        if (candidate / "pyproject.toml").exists() or (candidate / ".git").exists():
            return candidate
    return current


def value_files(root: Path, group: str, brick: str) -> list[Path]:
    """Return the ordered, existing value files for a brick (lowest to highest precedence).

    Layering:
        1. umbrella-charts/<group>/<brick>/values.yaml   (base defaults)
        2. config/values.yaml                            (optional global overrides)
        3. config/<group>/<brick>/values.yaml            (optional per-brick overrides)
    """
    candidates = [
        root / "umbrella-charts" / group / brick / "values.yaml",
        root / "config" / "values.yaml",
        root / "config" / group / brick / "values.yaml",
    ]
    return [path for path in candidates if path.exists()]

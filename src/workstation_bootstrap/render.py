"""Render Helm charts to Kubernetes manifests via `helm template`."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from . import command


def _template_args(
    chart_dir: Path,
    release: str,
    namespace: str,
    values: Sequence[Path],
) -> list[str]:
    args = [
        "helm",
        "template",
        release,
        str(chart_dir),
        "--namespace",
        namespace,
        "--include-crds",
    ]
    for value_file in values:
        args += ["-f", str(value_file)]
    return args


def update_dependencies(chart_dir: Path) -> None:
    """Resolve chart dependencies (helm dependency update) from Chart.yaml."""
    if (chart_dir / "Chart.yaml").exists():
        command.run(["helm", "dependency", "update", str(chart_dir)])


def render_chart(
    chart_dir: Path,
    *,
    release: str,
    namespace: str,
    values: Sequence[Path] = (),
) -> str:
    """Return the rendered manifests for a chart as a single YAML stream."""
    return command.run(
        _template_args(chart_dir, release, namespace, values),
        capture=True,
    ).stdout

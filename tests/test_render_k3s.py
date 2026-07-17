"""Tests for k3s install flags and Helm render arguments."""

from __future__ import annotations

from pathlib import Path

from workstation_bootstrap import k3s
from workstation_bootstrap.render import _template_args


def test_install_exec_disables_bundled_networking() -> None:
    exec_line = k3s.install_exec()
    assert "--flannel-backend=none" in exec_line
    assert "--disable-network-policy" in exec_line
    assert "--disable=traefik" in exec_line
    assert "--disable=servicelb" in exec_line


def test_template_args_builds_helm_command(tmp_path: Path) -> None:
    chart = tmp_path / "chart"
    values = tmp_path / "values.yaml"

    args = _template_args(chart, "rel", "ns", [values])

    assert args[:3] == ["helm", "template", "rel"]
    assert "--include-crds" in args
    assert args[args.index("--namespace") + 1] == "ns"
    assert args[args.index("-f") + 1] == str(values)

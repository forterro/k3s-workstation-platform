"""Tests for k3s install flags and Helm install arguments."""

from __future__ import annotations

from pathlib import Path

import pytest

from workstation_bootstrap import k3s
from workstation_bootstrap.helm import install_args


def test_install_exec_disables_bundled_networking() -> None:
    exec_line = k3s.install_exec()
    assert "--flannel-backend=none" in exec_line
    assert "--disable-network-policy" in exec_line
    assert "--disable-kube-proxy" in exec_line
    assert "--disable=traefik" in exec_line
    assert "--disable=servicelb" in exec_line


def test_uninstall_without_script_is_noop(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(k3s, "UNINSTALL_SCRIPT", tmp_path / "missing.sh")

    def _fail(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("command.run must not run when the script is absent")

    monkeypatch.setattr(k3s.command, "run", _fail)
    k3s.uninstall()


def test_install_args_builds_helm_command(tmp_path: Path) -> None:
    chart = tmp_path / "chart"
    values = tmp_path / "values.yaml"
    kubeconfig = tmp_path / "kubeconfig"

    args = install_args(chart, "rel", "ns", [values], kubeconfig, "5m")

    assert args[:4] == ["helm", "upgrade", "--install", "rel"]
    assert "--create-namespace" in args
    assert "--wait" in args
    assert args[args.index("--namespace") + 1] == "ns"
    assert args[args.index("--kubeconfig") + 1] == str(kubeconfig)
    assert args[args.index("-f") + 1] == str(values)

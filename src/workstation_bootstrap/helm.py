"""Install Helm charts imperatively with `helm upgrade --install`.

Using helm directly (rather than rendering and applying manifests) honors chart hooks, installs
CRDs, orders resources, and waits for readiness.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from . import command
from .k3s import KUBECONFIG


def update_dependencies(chart_dir: Path) -> None:
    """Resolve chart dependencies (helm dependency update) from Chart.yaml."""
    if (chart_dir / "Chart.yaml").exists():
        command.run(["helm", "dependency", "update", str(chart_dir)])


def release_exists(release: str, namespace: str, *, kubeconfig: Path = KUBECONFIG) -> bool:
    """Return True if a Helm release with this name already exists in the namespace.

    Used to make the seed install-once: once a brick is seeded and ArgoCD adopts its resources, a
    second `helm upgrade` would fight ArgoCD's field manager, so the phase skips it instead.
    """
    result = command.run(
        ["helm", "status", release, "--namespace", namespace, "--kubeconfig", str(kubeconfig)],
        check=False,
        capture=True,
    )
    return result.returncode == 0



def install_args(
    chart_dir: Path,
    release: str,
    namespace: str,
    values: Sequence[Path],
    kubeconfig: Path,
    wait_timeout: str,
    set_values: Sequence[str] = (),
) -> list[str]:
    args = [
        "helm",
        "upgrade",
        "--install",
        release,
        str(chart_dir),
        "--namespace",
        namespace,
        "--create-namespace",
        "--wait",
        "--timeout",
        wait_timeout,
        "--kubeconfig",
        str(kubeconfig),
    ]
    for value_file in values:
        args += ["-f", str(value_file)]
    for override in set_values:
        args += ["--set", override]
    return args


def install(
    chart_dir: Path,
    *,
    release: str,
    namespace: str,
    values: Sequence[Path] = (),
    set_values: Sequence[str] = (),
    kubeconfig: Path = KUBECONFIG,
    wait_timeout: str = "5m",
) -> None:
    """Install or upgrade a chart and wait for its resources to be ready."""
    command.run(
        install_args(chart_dir, release, namespace, values, kubeconfig, wait_timeout, set_values)
    )

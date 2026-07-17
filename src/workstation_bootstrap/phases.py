"""Ordered seed phases and the bootstrap runner.

The seed installs only what ArgoCD needs to exist and take over. Phases execute in order; each
depends on the previous one.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from pathlib import Path

from . import console, helm, k3s, settings, tools


class PhaseError(RuntimeError):
    """Raised when a phase cannot complete."""


@dataclass(frozen=True)
class Context:
    root: Path
    dry_run: bool


@dataclass(frozen=True)
class Phase:
    name: str
    description: str
    run: Callable[[Context], None]


def _seed_values(root: Path, brick: str) -> list[Path]:
    override = root / "config" / brick / "values.yaml"
    return [override] if override.exists() else []


def _phase_k3s(context: Context) -> None:
    k3s.ensure_k3s(dry_run=context.dry_run)


def _phase_root_app(context: Context) -> None:
    chart = context.root / "bootstrap" / "helm" / "root-app"
    if not chart.exists():
        raise PhaseError(f"seed chart not found: {chart}")
    config = settings.load_or_init_config(context.root)
    values_file = settings.render_root_app_values(config)
    helm.update_dependencies(chart)
    helm.install(chart, release="root-app", namespace="argocd", values=[values_file])


def _apply_seed_chart(context: Context, *, brick: str, namespace: str, release: str) -> None:
    chart = context.root / "bootstrap" / "helm" / brick
    if not chart.exists():
        raise PhaseError(f"seed chart not found: {chart}")
    helm.update_dependencies(chart)
    helm.install(
        chart,
        release=release,
        namespace=namespace,
        values=_seed_values(context.root, brick),
    )


PHASE_PLAN: tuple[Phase, ...] = (
    Phase("k3s", "Install and start k3s (flannel CNI, standard NetworkPolicy)", _phase_k3s),
    Phase(
        "cert-manager",
        "Apply cert-manager with its CRDs",
        partial(
            _apply_seed_chart,
            brick="cert-manager",
            namespace="cert-manager",
            release="cert-manager",
        ),
    ),
    Phase(
        "argocd",
        "Install ArgoCD",
        partial(_apply_seed_chart, brick="argo-cd", namespace="argocd", release="argocd"),
    ),
    Phase("root-app", "Apply the ArgoCD root app-of-apps", _phase_root_app),
)


def run_bootstrap(context: Context) -> bool:
    """Execute the seed phases in order. Returns True on success."""
    console.step("Bootstrapping the workstation cluster")

    console.step("Ensuring prerequisites")
    tools.ensure_tools(dry_run=context.dry_run)
    tools.ensure_age_key(dry_run=context.dry_run)

    if context.dry_run:
        console.info("Dry run: listing the planned phase sequence")
        for index, phase in enumerate(PHASE_PLAN, start=1):
            console.sub(f"[{index}] {phase.name}: {phase.description}")
        return True

    for index, phase in enumerate(PHASE_PLAN, start=1):
        console.step(f"Phase [{index}] {phase.name}")
        phase.run(context)
        console.ok(f"Phase {phase.name} complete")

    console.ok("Seed complete; ArgoCD now reconciles the platform")
    return True

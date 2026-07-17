"""Ordered seed phases and the bootstrap runner.

The seed installs only what ArgoCD needs to exist and take over. Phases execute in order; each
depends on the previous one.
"""

from __future__ import annotations

import base64
import os
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from pathlib import Path

from . import command, console, helm, k3s, settings, tools

_STEP_CA_NAMESPACE = "step-ca"
_STEP_CA_ISSUER_NAME = "step-ca-acme"
_STEP_CA_ACME_SERVER = "https://step-ca.step-ca.svc.cluster.local/acme/acme/directory"
_STEP_CA_INGRESS_CLASS = "traefik"


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


def _kubectl_env() -> dict[str, str]:
    return {**os.environ, "KUBECONFIG": str(k3s.KUBECONFIG)}


def _kubectl_apply(manifest: str, env: dict[str, str]) -> None:
    command.run(["kubectl", "apply", "-f", "-"], input_text=manifest, env=env)


def _apply_namespace(name: str, env: dict[str, str]) -> None:
    manifest = command.run(
        ["kubectl", "create", "namespace", name, "--dry-run=client", "-o", "yaml"],
        capture=True,
        env=env,
    ).stdout
    _kubectl_apply(manifest, env)


def _apply_generated(create_args: list[str], env: dict[str, str]) -> None:
    manifest = command.run(
        [*create_args, "--dry-run=client", "-o", "yaml"],
        capture=True,
        env=env,
    ).stdout
    _kubectl_apply(manifest, env)


def _ensure_sops_age_secret(context: Context) -> None:
    """Publish the local age key as the argocd/sops-age secret consumed by the KSOPS plugin."""
    key_path = tools.age_key_path()
    if not key_path.exists():
        raise PhaseError(f"age key not found: {key_path}")
    env = _kubectl_env()
    _apply_namespace("argocd", env)
    _apply_generated(
        [
            "kubectl",
            "create",
            "secret",
            "generic",
            "sops-age",
            "--namespace",
            "argocd",
            f"--from-file=keys.txt={key_path}",
        ],
        env,
    )
    console.ok("sops-age secret ensured in the argocd namespace")


def _ensure_local_ca(context: Context) -> Path:
    """Generate the workstation CA locally if absent and return its directory."""
    ca = settings.ca_dir()
    if (ca / "root_ca.crt").exists():
        console.ok(f"CA present: {ca}")
        return ca
    script = context.root / "scripts" / "generate-ca.sh"
    if not script.exists():
        raise PhaseError(f"CA generator not found: {script}")
    console.step("Generating the workstation CA")
    command.run(["bash", str(script)])
    if not (ca / "root_ca.crt").exists():
        raise PhaseError(f"CA generation produced no material in {ca}")
    return ca


def _clusterissuer_manifest(ca: Path) -> str:
    ca_bundle = base64.b64encode((ca / "root_ca.crt").read_bytes()).decode("ascii")
    return (
        "apiVersion: cert-manager.io/v1\n"
        "kind: ClusterIssuer\n"
        f"metadata:\n  name: {_STEP_CA_ISSUER_NAME}\n"
        "spec:\n"
        "  acme:\n"
        f"    server: {_STEP_CA_ACME_SERVER}\n"
        f"    caBundle: {ca_bundle}\n"
        "    privateKeySecretRef:\n"
        f"      name: {_STEP_CA_ISSUER_NAME}-account-key\n"
        "    solvers:\n"
        "      - http01:\n"
        "          ingress:\n"
        f"            ingressClassName: {_STEP_CA_INGRESS_CLASS}\n"
    )


def _phase_step_ca_material(context: Context) -> None:
    """Generate (if absent) and apply the local CA material and the ACME ClusterIssuer."""
    ca = _ensure_local_ca(context)
    env = _kubectl_env()
    _apply_namespace(_STEP_CA_NAMESPACE, env)
    _apply_generated(
        [
            "kubectl",
            "create",
            "configmap",
            "step-ca-certs",
            "--namespace",
            _STEP_CA_NAMESPACE,
            f"--from-file=root_ca.crt={ca / 'root_ca.crt'}",
            f"--from-file=intermediate_ca.crt={ca / 'intermediate_ca.crt'}",
        ],
        env,
    )
    _apply_generated(
        [
            "kubectl",
            "create",
            "configmap",
            "step-ca-config",
            "--namespace",
            _STEP_CA_NAMESPACE,
            f"--from-file=ca.json={ca / 'ca.json'}",
        ],
        env,
    )
    _apply_generated(
        [
            "kubectl",
            "create",
            "secret",
            "generic",
            "step-ca-secrets",
            "--namespace",
            _STEP_CA_NAMESPACE,
            "--type",
            "smallstep.com/private-keys",
            f"--from-file=root_ca_key={ca / 'root_ca_key'}",
            f"--from-file=intermediate_ca_key={ca / 'intermediate_ca_key'}",
        ],
        env,
    )
    _apply_generated(
        [
            "kubectl",
            "create",
            "secret",
            "generic",
            "step-ca-ca-password",
            "--namespace",
            _STEP_CA_NAMESPACE,
            "--type",
            "smallstep.com/ca-password",
            f"--from-file=password={ca / 'ca.pass'}",
        ],
        env,
    )
    _kubectl_apply(_clusterissuer_manifest(ca), env)
    console.ok("step-ca material and ClusterIssuer applied")


def _phase_argocd(context: Context) -> None:
    _ensure_sops_age_secret(context)
    _apply_seed_chart(context, brick="argo-cd", namespace="argocd", release="argocd")


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
        "step-ca-material",
        "Generate and apply the local CA material and ClusterIssuer",
        _phase_step_ca_material,
    ),
    Phase("argocd", "Install ArgoCD with the KSOPS plugin", _phase_argocd),
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

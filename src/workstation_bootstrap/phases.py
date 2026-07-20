"""Ordered seed phases and the bootstrap runner.

The seed installs only what ArgoCD needs to exist and take over. Phases execute in order; each
depends on the previous one.
"""

from __future__ import annotations

import base64
import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from pathlib import Path

from . import command, console, helm, k3s, settings, tools

_STEP_CA_NAMESPACE = "step-ca"
_STEP_CA_ISSUER_NAME = "step-ca-acme"
_STEP_CA_ACME_SERVER = "https://step-ca.step-ca.svc.cluster.local/acme/acme/directory"
_STEP_CA_INGRESS_CLASS = "traefik"

_METALLB_NAMESPACE = "metallb-system"
_METALLB_POOL_NAME = "workstation-pool"
_METALLB_L2_NAME = "workstation-l2"
_TRAEFIK_NAMESPACE = "traefik"

_OBSERVABILITY_NAMESPACE = "observability"
_GRAFANA_ADMIN_SECRET = "grafana-admin"

# Marks resources that are managed imperatively (outside git) so the ArgoCD step-ca application
# does not treat them as extraneous and prune them.
_ARGOCD_IGNORE = "argocd.argoproj.io/compare-options=IgnoreExtraneous"


class PhaseError(RuntimeError):
    """Raised when a phase cannot complete."""


@dataclass(frozen=True)
class Context:
    root: Path
    dry_run: bool
    loadbalancer_ip: str | None = None


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


def _annotate_local(manifest: str, annotations: list[str], env: dict[str, str]) -> str:
    """Add annotations to a rendered manifest locally (without contacting the API server)."""
    return command.run(
        ["kubectl", "annotate", "--local", "-f", "-", "-o", "yaml", *annotations],
        capture=True,
        env=env,
        input_text=manifest,
    ).stdout


def _apply_namespace(
    name: str, env: dict[str, str], *, annotations: list[str] | None = None
) -> None:
    manifest = command.run(
        ["kubectl", "create", "namespace", name, "--dry-run=client", "-o", "yaml"],
        capture=True,
        env=env,
    ).stdout
    if annotations:
        manifest = _annotate_local(manifest, annotations, env)
    _kubectl_apply(manifest, env)


def _apply_generated(
    create_args: list[str], env: dict[str, str], *, annotations: list[str] | None = None
) -> None:
    manifest = command.run(
        [*create_args, "--dry-run=client", "-o", "yaml"],
        capture=True,
        env=env,
    ).stdout
    if annotations:
        manifest = _annotate_local(manifest, annotations, env)
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
        "  annotations:\n"
        "    argocd.argoproj.io/compare-options: IgnoreExtraneous\n"
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


def _normalize_ca_json(ca: Path) -> None:
    """Force ca.json paths to the step-ca container layout (idempotent; self-heals older CAs).

    step ca init writes absolute paths based on the temporary STEPPATH used at generation time; the
    step-ca container expects them under /home/step where the ConfigMaps, Secrets and PVC mount.
    """
    path = ca / "ca.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["root"] = "/home/step/certs/root_ca.crt"
    data["crt"] = "/home/step/certs/intermediate_ca.crt"
    data["key"] = "/home/step/secrets/intermediate_ca_key"
    db = data.get("db")
    if isinstance(db, dict) and db.get("dataSource"):
        db["dataSource"] = "/home/step/db"
    path.write_text(json.dumps(data, indent=3) + "\n", encoding="utf-8")


def _phase_step_ca_material(context: Context) -> None:
    """Generate (if absent) and apply the local CA material and the ACME ClusterIssuer."""
    ca = _ensure_local_ca(context)
    _normalize_ca_json(ca)
    env = _kubectl_env()
    ignore = [_ARGOCD_IGNORE]
    _apply_namespace(_STEP_CA_NAMESPACE, env, annotations=ignore)
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
        annotations=ignore,
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
        annotations=ignore,
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
        annotations=ignore,
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
        annotations=ignore,
    )
    _kubectl_apply(_clusterissuer_manifest(ca), env)
    console.ok("step-ca material and ClusterIssuer applied")


def _phase_argocd(context: Context) -> None:
    _ensure_sops_age_secret(context)
    # The Traefik IngressRoute CRD does not exist yet at seed time, so install ArgoCD with its
    # ingress disabled. Once Traefik is reconciled, ArgoCD self-manages this Application and the
    # umbrella default (ingressRoute.enabled=true) re-enables the ingress.
    _apply_seed_chart(
        context,
        group="kube-mgmt",
        brick="argocd",
        namespace="argocd",
        release="argocd",
        set_values=["ingressRoute.enabled=false"],
    )


def _metallb_pool_manifest(ip: str) -> str:
    """Render the single-address IPAddressPool and its L2Advertisement for MetalLB.

    A single-address pool plus the one LoadBalancer service (Traefik, annotated with the pool name)
    pins Traefik to a stable, well-known address.
    """
    return (
        "apiVersion: metallb.io/v1beta1\n"
        "kind: IPAddressPool\n"
        "metadata:\n"
        f"  name: {_METALLB_POOL_NAME}\n"
        f"  namespace: {_METALLB_NAMESPACE}\n"
        "spec:\n"
        "  addresses:\n"
        f"    - {ip}/32\n"
        "---\n"
        "apiVersion: metallb.io/v1beta1\n"
        "kind: L2Advertisement\n"
        "metadata:\n"
        f"  name: {_METALLB_L2_NAME}\n"
        f"  namespace: {_METALLB_NAMESPACE}\n"
        "spec:\n"
        "  ipAddressPools:\n"
        f"    - {_METALLB_POOL_NAME}\n"
    )


def _apply_metallb_pool(ip: str, env: dict[str, str]) -> None:
    """Apply the pool, retrying while the MetalLB admission webhook is still coming up.

    helm --wait reports the controller Deployment as Available before its validating webhook is
    reachable, so the first apply often hits a 502 from the API server proxy. Retry until it serves.
    """
    manifest = _metallb_pool_manifest(ip)
    attempts = 30
    delay = 5
    for attempt in range(1, attempts + 1):
        try:
            _kubectl_apply(manifest, env)
            return
        except command.CommandError:
            if attempt == attempts:
                raise
            console.sub(f"MetalLB webhook not ready yet; retrying ({attempt}/{attempts})")
            time.sleep(delay)


def _phase_metallb(context: Context) -> None:
    """Install MetalLB and pin the LoadBalancer pool to the configured Traefik IP."""
    _apply_seed_chart(
        context,
        group="core-stack",
        brick="metallb",
        namespace=_METALLB_NAMESPACE,
        release="metallb",
    )
    ip = settings.ensure_loadbalancer_ip(context.root, override=context.loadbalancer_ip)
    _apply_metallb_pool(ip, _kubectl_env())
    console.ok(f"MetalLB address pool pinned to {ip}")


def reconfigure_loadbalancer_ip(context: Context, ip: str) -> None:
    """Change the Traefik LoadBalancer IP and restart the affected services.

    Persists the new IP, re-applies the MetalLB pool so the address is reassigned, and restarts
    Traefik so it re-announces on the new address.
    """
    new_ip = settings.ensure_loadbalancer_ip(context.root, override=ip)
    env = _kubectl_env()
    _apply_metallb_pool(new_ip, env)
    command.run(
        ["kubectl", "-n", _TRAEFIK_NAMESPACE, "rollout", "restart", "deployment"],
        env=env,
        check=False,
    )
    console.ok(f"Traefik LoadBalancer IP updated to {new_ip}")


def _phase_grafana_admin_secret(context: Context) -> None:
    """Publish the stable Grafana admin secret before ArgoCD deploys Grafana.

    The Grafana chart regenerates a random admin password on every render, so ArgoCD would rewrite
    it on each reconcile (perpetual rollouts). Instead the chart references this out-of-git secret
    via ``admin.existingSecret``. The password is generated once and persisted locally, so the value
    stays stable; the apply is idempotent.
    """
    password = settings.ensure_grafana_admin_password()
    env = _kubectl_env()
    ignore = [_ARGOCD_IGNORE]
    _apply_namespace(_OBSERVABILITY_NAMESPACE, env, annotations=ignore)
    _apply_generated(
        [
            "kubectl",
            "create",
            "secret",
            "generic",
            _GRAFANA_ADMIN_SECRET,
            "--namespace",
            _OBSERVABILITY_NAMESPACE,
            f"--from-literal=admin-user={settings.GRAFANA_ADMIN_USER}",
            f"--from-literal=admin-password={password}",
        ],
        env,
        annotations=ignore,
    )
    console.ok("Grafana admin secret ensured in the observability namespace")


def _phase_root_app(context: Context) -> None:
    chart = context.root / "bootstrap" / "helm" / "root-app"
    if not chart.exists():
        raise PhaseError(f"seed chart not found: {chart}")
    if helm.release_exists("root-app", "argocd"):
        console.sub("root-app already seeded; ArgoCD owns it now \u2014 skipping")
        return
    config = settings.load_or_init_config(context.root)
    values_file = settings.render_root_app_values(config)
    helm.update_dependencies(chart)
    helm.install(chart, release="root-app", namespace="argocd", values=[values_file])


def _phase_extra_root_apps(context: Context) -> None:
    """Apply optional extra root app-of-apps declared in the consumer config.

    This keeps the base platform decoupled: layers built on top (e.g. an AI stack in a sibling
    repository) register their own root app-of-apps via ``extra_root_apps`` in the consumer config,
    without the base code referencing them. Each chart path is resolved relative to the platform
    repository root, so a sibling submodule is reachable as ``../<repo>/bootstrap/helm/root-app``.
    """
    config = settings.load_or_init_config(context.root)
    for entry in settings.extra_root_apps(config):
        name = entry.get("name")
        raw_path = entry.get("path")
        if not name or not raw_path:
            raise PhaseError(f"invalid extra_root_apps entry: {entry!r}")
        chart = (context.root / raw_path).resolve()
        if not chart.exists():
            raise PhaseError(f"extra root app chart not found: {chart}")
        if helm.release_exists(name, "argocd"):
            console.sub(f"extra root app '{name}' already seeded \u2014 skipping")
            continue
        console.sub(f"applying extra root app '{name}' from {chart}")
        helm.update_dependencies(chart)
        helm.install(chart, release=name, namespace="argocd")


def _apply_seed_chart(
    context: Context,
    *,
    group: str,
    brick: str,
    namespace: str,
    release: str,
    set_values: list[str] | None = None,
) -> None:
    # Seed charts are installed from their single source of truth under umbrella-charts/, the same
    # charts ArgoCD adopts and reconciles afterwards via the apps/ manifests.
    chart = context.root / "umbrella-charts" / group / brick
    if not chart.exists():
        raise PhaseError(f"seed chart not found: {chart}")
    if helm.release_exists(release, namespace):
        console.sub(f"{release} already seeded; ArgoCD owns it now \u2014 skipping")
        return
    helm.update_dependencies(chart)
    helm.install(
        chart,
        release=release,
        namespace=namespace,
        values=_seed_values(context.root, brick),
        set_values=set_values or [],
    )


PHASE_PLAN: tuple[Phase, ...] = (
    Phase("k3s", "Install and start k3s (flannel CNI, standard NetworkPolicy)", _phase_k3s),
    Phase(
        "cert-manager",
        "Apply cert-manager with its CRDs",
        partial(
            _apply_seed_chart,
            group="core-stack",
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
    Phase(
        "metallb",
        "Install MetalLB and pin the LoadBalancer pool to the configured Traefik IP",
        _phase_metallb,
    ),
    Phase(
        "grafana-admin-secret",
        "Publish the stable Grafana admin secret consumed by the observability stack",
        _phase_grafana_admin_secret,
    ),
    Phase("root-app", "Apply the ArgoCD root app-of-apps", _phase_root_app),
    Phase(
        "extra-root-apps",
        "Apply optional extra root app-of-apps declared in the consumer config",
        _phase_extra_root_apps,
    ),
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

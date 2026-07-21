"""Ordered seed phases and the bootstrap runner.

The seed installs only what ArgoCD needs to exist and take over. Phases execute in order; each
depends on the previous one.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import time
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from pathlib import Path

from . import command, console, gpu, helm, k3s, settings, tools

_STEP_CA_NAMESPACE = "step-ca"
_STEP_CA_ISSUER_NAME = "step-ca-acme"
_STEP_CA_ACME_SERVER = "https://step-ca.step-ca.svc.cluster.local/acme/acme/directory"
_STEP_CA_INGRESS_CLASS = "traefik"

_METALLB_NAMESPACE = "metallb-system"
_METALLB_POOL_NAME = "workstation-pool"
_METALLB_L2_NAME = "workstation-l2"
_TRAEFIK_NAMESPACE = "traefik"

# Host-side split DNS for the *.workstation.internal zone (the WSL mirror of the Windows
# Acrylic + NRPT setup). A local dnsmasq answers the wildcard zone; systemd-resolved routes
# only that zone to it and keeps forwarding everything else to the host upstream.
_WORKSTATION_DOMAIN = "workstation.internal"
_HOST_DNS_PORT = 5353
_HOST_DNS_SERVICE = "workstation-internal-dns"
_HOST_DNS_CONF = Path("/etc/dnsmasq.d/workstation-internal.conf")
_HOST_DNS_UNIT = Path("/etc/systemd/system/workstation-internal-dns.service")
_RESOLVED_DROPIN = Path("/etc/systemd/resolved.conf.d/workstation-internal.conf")
_NSSWITCH_PATH = Path("/etc/nsswitch.conf")
_HOST_CA_CERT = Path("/usr/local/share/ca-certificates/workstation-root-ca.crt")

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


def _phase_gpu_runtime(context: Context) -> None:
    gpu.ensure_nvidia_runtime(dry_run=context.dry_run)


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


def _trust_ca_on_host(ca: Path) -> None:
    """Install the workstation root CA into the host trust store.

    Lets host clients (curl, VS Code extensions, the local coding agent) reach
    ``*.workstation.internal`` over HTTPS without passing the CA explicitly. Idempotent: only reruns
    update-ca-certificates when the installed copy is missing or stale.
    """
    source = ca / "root_ca.crt"
    if not source.exists():
        return
    desired = source.read_text(encoding="ascii")
    try:
        if _HOST_CA_CERT.read_text(encoding="ascii") == desired:
            console.ok("workstation root CA already trusted by the host")
            return
    except OSError:
        pass
    _sudo_write(_HOST_CA_CERT, desired)
    command.run(_sudo(["update-ca-certificates"]))
    console.ok("workstation root CA installed into the host trust store")


def _phase_step_ca_material(context: Context) -> None:
    """Generate (if absent) and apply the local CA material and the ACME ClusterIssuer."""
    ca = _ensure_local_ca(context)
    _normalize_ca_json(ca)
    _trust_ca_on_host(ca)
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
    _apply_host_dns(new_ip, dry_run=context.dry_run)


def _sudo(cmd: list[str]) -> list[str]:
    return cmd if os.geteuid() == 0 else ["sudo", *cmd]


def _sudo_write(path: Path, content: str) -> None:
    """Write a file owned by root via sudo, creating the parent directory if needed."""
    command.run(_sudo(["mkdir", "-p", str(path.parent)]))
    command.run(_sudo(["tee", str(path)]), input_text=content, capture=True)


def _host_upstream_dns() -> list[str]:
    """Return the host upstream nameservers from resolv.conf, excluding loopback.

    dnsmasq forwards everything outside the workstation.internal zone to these, so general DNS keeps
    working. Loopback servers are dropped so we never forward back into our own resolver.
    """
    servers: list[str] = []
    try:
        text = Path("/etc/resolv.conf").read_text(encoding="utf-8")
    except OSError:
        return servers
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] == "nameserver" and not parts[1].startswith("127."):
            servers.append(parts[1])
    return servers


def _host_dns_conf(ip: str, upstreams: list[str]) -> str:
    lines = [
        f"# Wildcard responder for *.{_WORKSTATION_DOMAIN} plus a plain forwarder.",
        "# systemd-resolved sends every query here (see the resolved drop-in); this answers the",
        "# zone locally and forwards the rest to the host upstream, so general DNS keeps working",
        "# and nothing here depends on the cluster. Managed by k3s-workstation-bootstrap.",
        f"port={_HOST_DNS_PORT}",
        "listen-address=127.0.0.1",
        "bind-interfaces",
        "no-hosts",
        f"address=/{_WORKSTATION_DOMAIN}/{ip}",
    ]
    if upstreams:
        lines.append("no-resolv")
        lines.extend(f"server={upstream}" for upstream in upstreams)
    return "\n".join(lines) + "\n"


def _host_dns_unit() -> str:
    return (
        "[Unit]\n"
        f"Description=Wildcard DNS for *.{_WORKSTATION_DOMAIN} (workstation platform)\n"
        "After=network.target\n"
        "\n"
        "[Service]\n"
        f"ExecStart=/usr/sbin/dnsmasq --keep-in-foreground --conf-file={_HOST_DNS_CONF}\n"
        "Restart=on-failure\n"
        "\n"
        "[Install]\n"
        "WantedBy=multi-user.target\n"
    )


def _resolved_dropin() -> str:
    return (
        "# Route all DNS through the local dnsmasq forwarder: it answers the workstation.internal\n"
        "# zone and forwards everything else upstream. Managed by k3s-workstation-bootstrap.\n"
        "[Resolve]\n"
        f"DNS=127.0.0.1:{_HOST_DNS_PORT}\n"
    )


def _ensure_nss_resolve() -> None:
    """Put systemd-resolved in the glibc lookup path so the zone routing takes effect.

    WSL leaves nsswitch as ``hosts: files dns`` and points resolv.conf straight at the host
    upstream, so resolved is bypassed. Switching to ``resolve [!UNAVAIL=return]`` makes glibc ask
    resolved first (which owns the zone route) and only falls back to plain ``dns`` if resolved is
    down, so general resolution keeps working either way.
    """
    try:
        current = _NSSWITCH_PATH.read_text(encoding="utf-8")
    except OSError:
        return
    lines = current.splitlines()
    for index, line in enumerate(lines):
        if line.startswith("hosts:"):
            if "resolve" in line:
                return
            lines[index] = "hosts:          files resolve [!UNAVAIL=return] dns"
            _sudo_write(_NSSWITCH_PATH, "\n".join(lines) + "\n")
            console.sub("nsswitch hosts set to query systemd-resolved")
            return


def _apply_host_dns(ip: str, *, dry_run: bool) -> None:
    """Make the host resolve *.workstation.internal to the Traefik LoadBalancer IP.

    Installs a local dnsmasq that answers the wildcard zone, runs it under a dedicated systemd
    unit on 127.0.0.1:5353, and scopes systemd-resolved to forward only that zone to it. Idempotent
    and reversible; safe to re-run on IP changes.
    """
    if dry_run:
        console.sub(f"[dry-run] would resolve *.{_WORKSTATION_DOMAIN} to {ip} via local dnsmasq")
        return
    if shutil.which("dnsmasq") is None:
        console.sub("installing dnsmasq and libnss-resolve")
        command.run(_sudo(["apt-get", "install", "-y", "dnsmasq", "libnss-resolve"]))
        # The packaged service binds :53 and would clash with the resolved stub; we run our own
        # scoped instance on :5353 instead, so disable the default one.
        command.run(_sudo(["systemctl", "disable", "--now", "dnsmasq"]), check=False)
    _sudo_write(_HOST_DNS_CONF, _host_dns_conf(ip, _host_upstream_dns()))
    _sudo_write(_HOST_DNS_UNIT, _host_dns_unit())
    command.run(_sudo(["systemctl", "daemon-reload"]))
    command.run(_sudo(["systemctl", "enable", _HOST_DNS_SERVICE]))
    command.run(_sudo(["systemctl", "restart", _HOST_DNS_SERVICE]))
    _ensure_nss_resolve()
    _sudo_write(_RESOLVED_DROPIN, _resolved_dropin())
    command.run(_sudo(["systemctl", "restart", "systemd-resolved"]), check=False)
    console.ok(f"Host resolves *.{_WORKSTATION_DOMAIN} to {ip}")


def _phase_host_dns(context: Context) -> None:
    """Resolve *.workstation.internal from the host (WSL) via a local wildcard DNS."""
    ip = settings.ensure_loadbalancer_ip(context.root, override=context.loadbalancer_ip)
    _apply_host_dns(ip, dry_run=context.dry_run)



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


def _ensure_config_repo_credential(config: dict, env: dict[str, str]) -> None:
    """Seed the ArgoCD repository credential for a private config repo, if any.

    ArgoCD authenticates to pull the personal config repository (composition roots, value overlays,
    encrypted secrets). The credential is read non-interactively from the host git credential helper
    and published as an ArgoCD repository secret. If no credential is returned (public repository)
    the step is skipped. The secret is applied as a manifest so the password never appears in a
    process argument or the command log.
    """
    url = settings.config_repo_url(config)
    if not url or not url.startswith("https://"):
        return
    host = urllib.parse.urlparse(url).hostname
    if not host:
        return
    filled = command.run(
        ["git", "credential", "fill"],
        input_text=f"protocol=https\nhost={host}\n\n",
        capture=True,
        check=False,
    ).stdout
    creds = dict(line.split("=", 1) for line in filled.splitlines() if "=" in line)
    password = creds.get("password")
    if not password:
        console.sub(f"no stored git credential for {host}; assuming a public config repo")
        return
    username = creds.get("username") or "git"
    _apply_namespace("argocd", env)
    manifest = (
        "apiVersion: v1\n"
        "kind: Secret\n"
        "metadata:\n"
        "  name: repo-workstation-config\n"
        "  namespace: argocd\n"
        "  labels:\n"
        "    argocd.argoproj.io/secret-type: repository\n"
        "type: Opaque\n"
        "stringData:\n"
        "  type: git\n"
        f"  url: {json.dumps(url)}\n"
        f"  username: {json.dumps(username)}\n"
        f"  password: {json.dumps(password)}\n"
    )
    _kubectl_apply(manifest, env)
    console.ok("ArgoCD credential for the config repo ensured (repo-workstation-config)")


def _apply_appproject(name: str, env: dict[str, str]) -> None:
    """Apply a permissive ArgoCD AppProject the layer Applications run under."""
    manifest = (
        "apiVersion: argoproj.io/v1alpha1\n"
        "kind: AppProject\n"
        "metadata:\n"
        f"  name: {name}\n"
        "  namespace: argocd\n"
        "spec:\n"
        f"  description: Per-workstation layer project ({name})\n"
        "  sourceRepos:\n"
        "    - '*'\n"
        "  destinations:\n"
        "    - namespace: '*'\n"
        "      server: https://kubernetes.default.svc\n"
        "  clusterResourceWhitelist:\n"
        "    - group: '*'\n"
        "      kind: '*'\n"
        "  namespaceResourceWhitelist:\n"
        "    - group: '*'\n"
        "      kind: '*'\n"
    )
    _kubectl_apply(manifest, env)


def _layer_application_manifest(
    *,
    name: str,
    project: str,
    repo_url: str,
    revision: str,
    path: str,
    config_repo_url: str,
    config_repo_revision: str,
) -> str:
    """Render the ArgoCD Application for a layer component, injecting the config repo URL."""
    params = (
        ("project", project),
        ("configRepoURL", config_repo_url),
        ("configRepoRevision", config_repo_revision),
    )
    param_lines = "".join(
        f"        - name: {n}\n          value: {json.dumps(v)}\n" for n, v in params
    )
    return (
        "apiVersion: argoproj.io/v1alpha1\n"
        "kind: Application\n"
        "metadata:\n"
        f"  name: {name}\n"
        "  namespace: argocd\n"
        "  finalizers:\n"
        "    - resources-finalizer.argocd.argoproj.io/background\n"
        "spec:\n"
        f"  project: {json.dumps(project)}\n"
        "  source:\n"
        f"    repoURL: {json.dumps(repo_url)}\n"
        f"    targetRevision: {json.dumps(revision)}\n"
        f"    path: {json.dumps(path)}\n"
        "    helm:\n"
        "      parameters:\n"
        f"{param_lines}"
        "  destination:\n"
        "    server: https://kubernetes.default.svc\n"
        "    namespace: argocd\n"
        "  syncPolicy:\n"
        "    automated:\n"
        "      prune: true\n"
        "      selfHeal: true\n"
        "    syncOptions:\n"
        "      - ServerSideApply=true\n"
    )


def _phase_extra_root_apps(context: Context) -> None:
    """Create the layer root Applications declared in the consumer config.

    Each ``extra_root_apps`` entry names a public layer chart (``repo_url`` + ``path``) and the
    ArgoCD project it runs under. The bootstrap turns it into an ArgoCD Application, injecting this
    workstation's private config repo (``config_repo_url``) as the ``configRepoURL`` Helm parameter
    so the layer's child apps pick up the value overlays kept there. Nothing about the layers is
    templated in the config repo: it only declares them. The ArgoCD credential for a private config
    repo is seeded first so ArgoCD can pull those overlays.
    """
    config = settings.load_or_init_config(context.root)
    entries = settings.extra_root_apps(config)
    if not entries:
        return
    env = _kubectl_env()
    _ensure_config_repo_credential(config, env)
    cfg_url = settings.config_repo_url(config) or ""
    cfg_rev = settings.config_repo_revision(config)
    for entry in entries:
        name = entry.get("name")
        repo_url = entry.get("repo_url")
        path = entry.get("path")
        if not name or not repo_url or not path:
            raise PhaseError(f"invalid extra_root_apps entry: {entry!r}")
        project = entry.get("project") or "default"
        revision = entry.get("revision") or settings.DEFAULT_REVISION
        if project != "default":
            _apply_appproject(project, env)
        console.sub(f"applying layer root '{name}' from {repo_url} ({path})")
        _kubectl_apply(
            _layer_application_manifest(
                name=name,
                project=project,
                repo_url=repo_url,
                revision=revision,
                path=path,
                config_repo_url=cfg_url,
                config_repo_revision=cfg_rev,
            ),
            env,
        )


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
        "gpu-runtime",
        "Enable the NVIDIA GPU runtime for k3s when a GPU is present",
        _phase_gpu_runtime,
    ),
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
        "host-dns",
        "Resolve *.workstation.internal from the host via a local wildcard DNS (dnsmasq)",
        _phase_host_dns,
    ),
    Phase(
        "grafana-admin-secret",
        "Publish the stable Grafana admin secret consumed by the observability stack",
        _phase_grafana_admin_secret,
    ),
    Phase("root-app", "Apply the ArgoCD root app-of-apps", _phase_root_app),
    Phase(
        "extra-root-apps",
        "Create the layer root Applications declared in the consumer config",
        _phase_extra_root_apps,
    ),
)


def _sync_config_repo(context: Context, override: str | None) -> None:
    """Clone or update the per-workstation config repo before any phase reads CONFIG_DIR."""
    url = override
    revision = None
    if settings.CONFIG_FILE.exists():
        config = settings.load_or_init_config(context.root)
        url = url or settings.config_repo_url(config)
        revision = config.get(settings.CONFIG_REPO_REVISION_KEY)
    if url:
        settings.ensure_config_repo(url, revision)


def run_bootstrap(context: Context, config_repo: str | None = None) -> bool:
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

    _sync_config_repo(context, config_repo)

    for index, phase in enumerate(PHASE_PLAN, start=1):
        console.step(f"Phase [{index}] {phase.name}")
        phase.run(context)
        console.ok(f"Phase {phase.name} complete")

    console.ok("Seed complete; ArgoCD now reconciles the platform")
    return True

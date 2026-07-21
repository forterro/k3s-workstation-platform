"""Consumer configuration stored under ~/.k3s-workstation-platform.

The platform repository URL (which ArgoCD tracks) is auto-detected from the clone's git origin on
first bootstrap and persisted so it can be overridden later.
"""

from __future__ import annotations

import ipaddress
import secrets
from pathlib import Path

import yaml

from . import command, console

CONFIG_DIR = Path.home() / ".k3s-workstation-platform"
CONFIG_FILE = CONFIG_DIR / "config.yaml"
CA_DIR = CONFIG_DIR / "ca"
DEFAULT_REPO_URL = "https://github.com/forterro/k3s-workstation-platform.git"
DEFAULT_REVISION = "main"
LOADBALANCER_IP_KEY = "loadbalancer_ip"
CONFIG_REPO_URL_KEY = "config_repo_url"
CONFIG_REPO_REVISION_KEY = "config_repo_revision"
GRAFANA_ADMIN_USER = "admin"


def ca_dir() -> Path:
    """Local directory holding the workstation CA material (never committed to git)."""
    return CA_DIR


def grafana_admin_password_path() -> Path:
    """Local file holding the Grafana admin password (never committed to git)."""
    return CONFIG_DIR / "grafana" / "admin-password"


def ensure_grafana_admin_password() -> str:
    """Return the Grafana admin password, generating and persisting it on first use.

    The value is stored locally under ~/.k3s-workstation-platform/grafana so it stays stable across
    cluster resets and re-bootstraps. The seed publishes it as the ``grafana-admin`` secret that the
    Grafana chart consumes via ``admin.existingSecret``; without a stable secret the chart would
    regenerate a random password on every ArgoCD reconcile.
    """
    path = grafana_admin_password_path()
    if path.exists():
        existing = path.read_text(encoding="utf-8").strip()
        if existing:
            return existing
    path.parent.mkdir(parents=True, exist_ok=True)
    password = secrets.token_urlsafe(24)
    path.write_text(password + "\n", encoding="utf-8")
    path.chmod(0o600)
    console.ok(f"generated Grafana admin password at {path}")
    return password


def _detect_repo_url(root: Path) -> str | None:
    result = command.run(
        ["git", "-C", str(root), "remote", "get-url", "origin"],
        check=False,
        capture=True,
    )
    url = result.stdout.strip()
    return url or None


def load_or_init_config(root: Path) -> dict:
    """Load the consumer config, initializing it from the git origin on first run."""
    if CONFIG_FILE.exists():
        return yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8")) or {}
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    config = {
        "platform_repo_url": _detect_repo_url(root) or DEFAULT_REPO_URL,
        "platform_revision": DEFAULT_REVISION,
    }
    CONFIG_FILE.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    console.ok(f"initialized config at {CONFIG_FILE}")
    return config


def _save_config(config: dict) -> None:
    """Persist the consumer config to disk."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")


def _valid_ipv4(value: str) -> bool:
    try:
        ipaddress.IPv4Address(value)
    except ipaddress.AddressValueError:
        return False
    return True


def _prompt_loadbalancer_ip() -> str:
    """Interactively ask for the fixed Traefik LoadBalancer IP served by MetalLB."""
    console.info(
        "Traefik needs a fixed LoadBalancer IP served by MetalLB. Pick a free address inside the "
        "WSL2 NAT subnet (the same range as the node eth0 address), for example 172.17.47.200."
    )
    while True:
        raw = input("Traefik LoadBalancer IP: ").strip()
        if _valid_ipv4(raw):
            return raw
        console.warn(f"'{raw}' is not a valid IPv4 address; try again")


def ensure_loadbalancer_ip(
    root: Path, *, override: str | None = None, interactive: bool = True
) -> str:
    """Return the configured Traefik LoadBalancer IP, prompting or overriding as needed.

    The IP is stored under ``loadbalancer_ip`` in the consumer config. When ``override`` is given it
    replaces the stored value. When absent and interactive, the user is prompted for it.
    """
    config = load_or_init_config(root)
    if override is not None:
        if not _valid_ipv4(override):
            raise ValueError(f"invalid IPv4 address: {override}")
        config[LOADBALANCER_IP_KEY] = override
        _save_config(config)
        return override
    existing = config.get(LOADBALANCER_IP_KEY)
    if existing:
        return existing
    if not interactive:
        raise ValueError(
            f"{LOADBALANCER_IP_KEY} is not set in {CONFIG_FILE}; pass an explicit IP instead"
        )
    ip = _prompt_loadbalancer_ip()
    config[LOADBALANCER_IP_KEY] = ip
    _save_config(config)
    console.ok(f"stored {LOADBALANCER_IP_KEY}={ip} in {CONFIG_FILE}")
    return ip


def config_repo_url(config: dict) -> str | None:
    """Return the per-workstation config repository URL, if declared.

    This is the personal composition repository cloned into ``CONFIG_DIR``. When set, the bootstrap
    keeps ``CONFIG_DIR`` in sync with it (clone or pull) before applying the extra root app-of-apps.
    """
    url = config.get(CONFIG_REPO_URL_KEY)
    return str(url) if url else None


def ensure_config_repo(url: str | None, revision: str | None = None) -> None:
    """Clone or update the per-workstation config repository at ``CONFIG_DIR``.

    ``CONFIG_DIR`` holds both the tracked composition (charts, apps, overlays) and local root-of-
    trust material that stays gitignored (CA, grafana password, rendered values). On a fresh
    machine the directory may already exist with those local files but no git checkout, so cloning
    is done via ``git init`` + ``fetch`` + ``checkout`` to avoid clobbering them; on an existing
    checkout the tracked content is fast-forwarded with ``git pull``.
    """
    if not url:
        return
    branch = revision or DEFAULT_REVISION
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    is_repo = (CONFIG_DIR / ".git").exists()
    if is_repo:
        console.sub(f"updating config repo at {CONFIG_DIR} ({branch})")
        command.run(["git", "-C", str(CONFIG_DIR), "pull", "--ff-only"], check=False)
        return
    console.sub(f"cloning config repo {url} into {CONFIG_DIR} ({branch})")
    command.run(["git", "-C", str(CONFIG_DIR), "init", "-q"])
    command.run(["git", "-C", str(CONFIG_DIR), "remote", "add", "origin", url])
    command.run(["git", "-C", str(CONFIG_DIR), "fetch", "--depth", "1", "origin", branch])
    command.run(["git", "-C", str(CONFIG_DIR), "checkout", "-f", "-B", branch, f"origin/{branch}"])


def config_repo_revision(config: dict) -> str:
    """Return the config repository revision (branch), defaulting to ``DEFAULT_REVISION``."""
    return str(config.get(CONFIG_REPO_REVISION_KEY) or DEFAULT_REVISION)


def extra_root_apps(config: dict) -> list[dict]:
    """Return the layer components declared by the consumer.

    Each entry describes one public layer chart the base platform should turn into an ArgoCD
    Application: ``name`` (the Application name), ``project`` (its ArgoCD project), ``repo_url`` /
    ``revision`` / ``path`` (the public chart source). The bootstrap injects this workstation's
    ``config_repo_url`` as the ``configRepoURL`` Helm parameter. Defaults to an empty list so the
    base platform stays decoupled from any layer built on top of it.
    """
    return list(config.get("extra_root_apps") or [])


def render_root_app_values(config: dict) -> Path:
    """Write a values override that points the root app at the configured platform repository."""
    override = {
        "rootApp": {
            "repoURL": config.get("platform_repo_url", DEFAULT_REPO_URL),
            "targetRevision": config.get("platform_revision", DEFAULT_REVISION),
        }
    }
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    path = CONFIG_DIR / "root-app-values.yaml"
    path.write_text(yaml.safe_dump(override, sort_keys=False), encoding="utf-8")
    return path

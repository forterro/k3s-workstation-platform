"""Consumer configuration stored under ~/.k3s-workstation-platform.

The platform repository URL (which ArgoCD tracks) is auto-detected from the clone's git origin on
first bootstrap and persisted so it can be overridden later.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from . import command, console

CONFIG_DIR = Path.home() / ".k3s-workstation-platform"
CONFIG_FILE = CONFIG_DIR / "config.yaml"
CA_DIR = CONFIG_DIR / "ca"
DEFAULT_REPO_URL = "https://github.com/forterro/k3s-workstation-platform.git"
DEFAULT_REVISION = "main"


def ca_dir() -> Path:
    """Local directory holding the workstation CA material (never committed to git)."""
    return CA_DIR


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

"""Installation of the CLI tools the bootstrap depends on.

Missing tools are downloaded from their official release locations, pinned to known versions, and
placed in the install directory (default /usr/local/bin, override with WORKSTATION_BIN_DIR). k3s is
installed by its own phase because it needs specific server flags.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import urllib.request
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

from . import command, console
from .config import find_repo_root

ARCH = "amd64"

VERSIONS_FILE = "tool-versions.yaml"

DEFAULT_BIN_DIR = Path("/usr/local/bin")


class ToolsError(Exception):
    """Raised when tool metadata is missing or invalid."""


@lru_cache(maxsize=1)
def _versions() -> dict[str, str]:
    path = find_repo_root() / VERSIONS_FILE
    if not path.exists():
        raise ToolsError(f"tool versions file not found: {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    versions = data.get("tools")
    if not isinstance(versions, dict):
        raise ToolsError(f"{VERSIONS_FILE} must define a 'tools' mapping")
    return {str(name): str(value) for name, value in versions.items()}


def pinned_version(tool: str) -> str:
    """Return the pinned version for a tool from tool-versions.yaml."""
    try:
        return _versions()[tool]
    except KeyError as exc:
        raise ToolsError(f"no version pinned for '{tool}' in {VERSIONS_FILE}") from exc


_VERSION_COMMANDS: dict[str, list[str]] = {
    "kubectl": ["kubectl", "version", "--client"],
    "helm": ["helm", "version", "--template", "{{.Version}}"],
    "sops": ["sops", "--version"],
    "age": ["age", "--version"],
    "step": ["step", "version"],
}
_SEMVER = re.compile(r"(\d+\.\d+\.\d+)")


def _installed_version(tool: str) -> str | None:
    """Return the installed version (x.y.z) of a tool, or None if not determinable."""
    cmd = _VERSION_COMMANDS.get(tool, [tool, "--version"])
    result = command.run(cmd, check=False, capture=True)
    match = _SEMVER.search((result.stdout or "") + (result.stderr or ""))
    return match.group(1) if match else None


def _bin_dir() -> Path:
    return Path(os.environ.get("WORKSTATION_BIN_DIR", str(DEFAULT_BIN_DIR)))


def _run(cmd: list[str]) -> None:
    subprocess.run(cmd, check=True)


def _download(url: str, dest: Path) -> None:
    console.sub(f"downloading {url}")
    with urllib.request.urlopen(url) as response, dest.open("wb") as out:
        shutil.copyfileobj(response, out)


def _place(src: Path, dest: Path) -> None:
    """Install a binary at dest, using sudo only when the target directory is not writable."""
    if os.access(dest.parent, os.W_OK):
        shutil.move(str(src), str(dest))
        dest.chmod(0o755)
    else:
        _run(["sudo", "install", "-m", "0755", str(src), str(dest)])


def _install_kubectl(bin_dir: Path) -> None:
    url = f"https://dl.k8s.io/release/{pinned_version('kubectl')}/bin/linux/{ARCH}/kubectl"
    with tempfile.TemporaryDirectory() as tmp:
        binary = Path(tmp) / "kubectl"
        _download(url, binary)
        _place(binary, bin_dir / "kubectl")


def _install_helm(bin_dir: Path) -> None:
    url = f"https://get.helm.sh/helm-{pinned_version('helm')}-linux-{ARCH}.tar.gz"
    with tempfile.TemporaryDirectory() as tmp:
        tarball = Path(tmp) / "helm.tar.gz"
        _download(url, tarball)
        _run(["tar", "-xzf", str(tarball), "-C", tmp])
        _place(Path(tmp) / f"linux-{ARCH}" / "helm", bin_dir / "helm")


def _install_sops(bin_dir: Path) -> None:
    tag = pinned_version("sops")
    url = (
        f"https://github.com/getsops/sops/releases/download/"
        f"{tag}/sops-{tag}.linux.{ARCH}"
    )
    with tempfile.TemporaryDirectory() as tmp:
        binary = Path(tmp) / "sops"
        _download(url, binary)
        _place(binary, bin_dir / "sops")


def _install_age(bin_dir: Path) -> None:
    tag = pinned_version("age")
    url = (
        f"https://github.com/FiloSottile/age/releases/download/"
        f"{tag}/age-{tag}-linux-{ARCH}.tar.gz"
    )
    with tempfile.TemporaryDirectory() as tmp:
        tarball = Path(tmp) / "age.tar.gz"
        _download(url, tarball)
        _run(["tar", "-xzf", str(tarball), "-C", tmp])
        _place(Path(tmp) / "age" / "age", bin_dir / "age")
        _place(Path(tmp) / "age" / "age-keygen", bin_dir / "age-keygen")


def _install_step(bin_dir: Path) -> None:
    tag = pinned_version("step")
    ver = tag.lstrip("v")
    url = (
        f"https://github.com/smallstep/cli/releases/download/"
        f"{tag}/step_linux_{ver}_{ARCH}.tar.gz"
    )
    with tempfile.TemporaryDirectory() as tmp:
        tarball = Path(tmp) / "step.tar.gz"
        _download(url, tarball)
        _run(["tar", "-xzf", str(tarball), "-C", tmp])
        _place(Path(tmp) / f"step_{ver}" / "bin" / "step", bin_dir / "step")


@dataclass(frozen=True)
class Tool:
    name: str
    install: Callable[[Path], None]


TOOLS: tuple[Tool, ...] = (
    Tool("kubectl", _install_kubectl),
    Tool("helm", _install_helm),
    Tool("sops", _install_sops),
    Tool("age", _install_age),
    Tool("step", _install_step),
)


def _tool_status(tool: Tool) -> str:
    if shutil.which(tool.name) is None:
        return "missing"
    installed = _installed_version(tool.name)
    if installed is None:
        return "unknown"
    return "current" if installed == pinned_version(tool.name).lstrip("v") else "outdated"


def tools_needing_install(tools: Iterable[Tool] = TOOLS) -> list[tuple[Tool, str]]:
    """Return (tool, status) pairs for tools that are missing or outdated."""
    pending: list[tuple[Tool, str]] = []
    for tool in tools:
        status = _tool_status(tool)
        if status in ("missing", "outdated"):
            pending.append((tool, status))
    return pending


def ensure_tools(dry_run: bool = False, tools: Iterable[Tool] = TOOLS) -> None:
    """Install any missing or outdated CLI tools into the install directory."""
    pending = tools_needing_install(tools)
    if not pending:
        console.ok("all required CLI tools present and current")
        return

    bin_dir = _bin_dir()
    for tool, status in pending:
        target = pinned_version(tool.name)
        if dry_run:
            console.sub(f"would install {tool.name} {target} ({status}) into {bin_dir}")
            continue
        console.step(f"Installing {tool.name} {target} ({status})")
        tool.install(bin_dir)
        console.ok(f"{tool.name} {target} installed")


def age_key_path() -> Path:
    override = os.environ.get("SOPS_AGE_KEY_FILE")
    if override:
        return Path(override)
    return Path.home() / ".config" / "sops" / "age" / "keys.txt"


def ensure_age_key(dry_run: bool = False) -> None:
    """Generate a local age key for SOPS if none exists yet."""
    path = age_key_path()
    if path.exists():
        console.ok(f"age key present: {path}")
        return
    if dry_run:
        console.sub(f"would generate age key at {path}")
        return
    console.step("Generating age key")
    path.parent.mkdir(parents=True, exist_ok=True)
    _run(["age-keygen", "-o", str(path)])
    path.chmod(0o600)
    console.ok(f"age key generated: {path}")

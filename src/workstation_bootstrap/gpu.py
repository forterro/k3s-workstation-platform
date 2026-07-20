"""Optional NVIDIA GPU runtime enablement for k3s.

When the host exposes an NVIDIA GPU (``nvidia-smi`` works), install the NVIDIA Container Toolkit so
k3s's embedded containerd gains an ``nvidia`` runtime handler. k3s auto-detects the
``nvidia-container-runtime`` binary at startup and writes the runtime into its generated containerd
config; a restart is required only when k3s was already running before the toolkit was installed.

On a host without a GPU (or without WSL2 GPU-PV), every step is a no-op, so the base platform stays
generic: nothing NVIDIA-specific is installed unless a GPU is actually present.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from . import command, console, k3s

_KEYRING = Path("/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg")
_APT_LIST = Path("/etc/apt/sources.list.d/nvidia-container-toolkit.list")
_GPGKEY_URL = "https://nvidia.github.io/libnvidia-container/gpgkey"
_REPO_LIST_URL = (
    "https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list"
)
# k3s renders its containerd config here; the nvidia runtime handler appears once the toolkit is
# installed and k3s has (re)started.
_CONTAINERD_CONFIG = Path("/var/lib/rancher/k3s/agent/etc/containerd/config.toml")


def _sudo(cmd: list[str]) -> list[str]:
    return cmd if os.geteuid() == 0 else ["sudo", *cmd]


def _gpu_present() -> bool:
    """Return True when an NVIDIA GPU is usable on the host."""
    if shutil.which("nvidia-smi") is None:
        return False
    result = command.run(["nvidia-smi", "-L"], check=False, capture=True)
    return result.returncode == 0 and "GPU" in (result.stdout or "")


def _toolkit_installed() -> bool:
    return shutil.which("nvidia-container-runtime") is not None


def _containerd_has_nvidia_runtime() -> bool:
    """Return True when k3s's containerd config already declares the nvidia runtime."""
    result = command.run(
        _sudo(["grep", "-q", "nvidia", str(_CONTAINERD_CONFIG)]),
        check=False,
    )
    return result.returncode == 0


def _install_toolkit() -> None:
    """Add the NVIDIA Container Toolkit apt repository and install the toolkit."""
    console.sub("adding the NVIDIA Container Toolkit apt repository")
    gpgkey = command.run(["curl", "-fsSL", _GPGKEY_URL], capture=True).stdout
    command.run(
        _sudo(["gpg", "--dearmor", "--yes", "-o", str(_KEYRING)]),
        input_text=gpgkey,
    )
    repo_list = command.run(["curl", "-fsSL", _REPO_LIST_URL], capture=True).stdout
    # Pin the repository to the keyring we just wrote.
    repo_list = repo_list.replace(
        "deb https://",
        f"deb [signed-by={_KEYRING}] https://",
    )
    command.run(_sudo(["tee", str(_APT_LIST)]), input_text=repo_list, capture=True)

    console.sub("installing the NVIDIA Container Toolkit")
    command.run(_sudo(["apt-get", "update"]))
    command.run(_sudo(["apt-get", "install", "-y", "nvidia-container-toolkit"]))


def ensure_nvidia_runtime(dry_run: bool = False) -> None:
    """Install the NVIDIA Container Toolkit and ensure k3s exposes the nvidia runtime.

    Idempotent and GPU-conditional: it does nothing when no NVIDIA GPU is present, and re-running it
    on an already-configured host is a no-op.
    """
    if not _gpu_present():
        console.ok("no NVIDIA GPU detected; skipping GPU runtime setup")
        return

    if _toolkit_installed() and _containerd_has_nvidia_runtime():
        console.ok("NVIDIA Container Toolkit present and k3s nvidia runtime configured")
        return

    if dry_run:
        console.sub("would install the NVIDIA Container Toolkit and enable the k3s nvidia runtime")
        return

    console.step("Enabling the NVIDIA GPU runtime for k3s")
    if not _toolkit_installed():
        _install_toolkit()

    # k3s writes the nvidia runtime into its containerd config at startup, but only sees the toolkit
    # if it was installed beforehand. Restart a running k3s so containerd picks the runtime up.
    if k3s.is_running() and not _containerd_has_nvidia_runtime():
        console.sub("restarting k3s so containerd picks up the nvidia runtime")
        k3s.restart()

    console.ok("NVIDIA GPU runtime enabled")

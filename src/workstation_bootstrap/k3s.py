"""k3s cluster provisioning.

k3s is installed with a Cilium-ready configuration: the bundled flannel CNI, servicelb, traefik,
and the kube-router network policy controller are disabled so Cilium owns networking and policy
enforcement.
"""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

from . import command, console

KUBECONFIG = Path("/etc/rancher/k3s/k3s.yaml")
INSTALL_URL = "https://get.k3s.io"

K3S_EXEC_FLAGS = (
    "--flannel-backend=none",
    "--disable-network-policy",
    "--disable=traefik",
    "--disable=servicelb",
    "--write-kubeconfig-mode=644",
)


def install_exec() -> str:
    """Return the INSTALL_K3S_EXEC value passed to the k3s installer."""
    return " ".join(K3S_EXEC_FLAGS)


def is_running() -> bool:
    if shutil.which("k3s") is None:
        return False
    result = command.run(["k3s", "kubectl", "get", "--raw=/readyz"], check=False, capture=True)
    return result.returncode == 0


def _wait_api(timeout_seconds: int = 120) -> None:
    console.sub("waiting for the k3s API to become ready")
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        result = command.run(["k3s", "kubectl", "get", "--raw=/readyz"], check=False, capture=True)
        if result.returncode == 0:
            return
        time.sleep(3)
    raise command.CommandError("k3s API did not become ready in time")


def ensure_k3s(dry_run: bool = False) -> None:
    """Install and start k3s with the Cilium-ready flags if it is not already running."""
    if is_running():
        console.ok("k3s already running")
        return
    if dry_run:
        console.sub(f"would install k3s with INSTALL_K3S_EXEC='{install_exec()}'")
        return

    console.step("Installing k3s")
    script = command.run(["curl", "-sfL", INSTALL_URL], capture=True).stdout
    command.run(
        ["sh", "-s", "-"],
        input_text=script,
        env={**os.environ, "INSTALL_K3S_EXEC": install_exec()},
    )
    _wait_api()
    console.ok("k3s installed and API ready")

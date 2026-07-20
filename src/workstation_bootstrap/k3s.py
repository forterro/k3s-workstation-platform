"""k3s cluster provisioning.

k3s is installed with its default networking: the flannel CNI, kube-proxy, and the kube-router
network policy controller, which enforces standard Kubernetes NetworkPolicy. Traefik is disabled so
ingress can be managed as a platform brick, and the built-in servicelb (klipper) is disabled so
MetalLB owns LoadBalancer services and can pin Traefik to a fixed address.
"""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

from . import command, console

KUBECONFIG = Path("/etc/rancher/k3s/k3s.yaml")
INSTALL_URL = "https://get.k3s.io"
UNINSTALL_SCRIPT = Path("/usr/local/bin/k3s-uninstall.sh")

K3S_EXEC_FLAGS = (
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
    """Install and start k3s with its default networking if it is not already running."""
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


_STALE_CNI_INTERFACES = ("cni0", "flannel.1", "cilium_host", "cilium_net", "cilium_vxlan")


def _sudo(cmd: list[str]) -> list[str]:
    return cmd if os.geteuid() == 0 else ["sudo", *cmd]


def _clean_cni_interfaces() -> None:
    """Delete leftover CNI interfaces; a stale VXLAN device blocks a fresh flannel start."""
    for iface in _STALE_CNI_INTERFACES:
        command.run(_sudo(["ip", "link", "delete", iface]), check=False)


def uninstall(dry_run: bool = False) -> None:
    """Completely remove k3s and its cluster state via the k3s uninstall script."""
    if not UNINSTALL_SCRIPT.exists():
        console.warn(f"k3s uninstall script not found ({UNINSTALL_SCRIPT}); nothing to reset")
        return
    if dry_run:
        console.sub(f"would run {UNINSTALL_SCRIPT} and clean stale CNI interfaces")
        return
    console.step("Removing k3s (cluster and all workloads)")
    command.run(_sudo([str(UNINSTALL_SCRIPT)]))
    _clean_cni_interfaces()
    console.ok("k3s removed")

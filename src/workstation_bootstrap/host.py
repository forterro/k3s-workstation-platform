"""Host checks and settings the cluster depends on: inotify limits and the LoadBalancer IP."""

from __future__ import annotations

import ipaddress
import json
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from . import command

# k3s, its pods and the agents' tools each hold inotify instances; Ubuntu's default (128) runs out.
INOTIFY_LIMITS = {
    "fs.inotify.max_user_instances": 8192,
    "fs.inotify.max_user_watches": 1048576,
}
SYSCTL_DROPIN = Path("/etc/sysctl.d/90-k3s-workstation.conf")


def _proc_path(key: str) -> Path:
    return Path("/proc/sys") / key.replace(".", "/")


def inotify_shortfall(read: Callable[[str], int | None] | None = None) -> dict[str, int]:
    """Return the inotify limits below the platform minimum, with their current value."""
    read = read or _read_sysctl
    low: dict[str, int] = {}
    for key, minimum in INOTIFY_LIMITS.items():
        current = read(key)
        if current is not None and current < minimum:
            low[key] = current
    return low


def _read_sysctl(key: str) -> int | None:
    try:
        return int(_proc_path(key).read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def sysctl_dropin() -> str:
    lines = ["# Managed by k3s-workstation-bootstrap: k3s and its pods exhaust the default limits."]
    lines += [f"{key} = {value}" for key, value in INOTIFY_LIMITS.items()]
    return "\n".join(lines) + "\n"


@dataclass(frozen=True)
class HostNetwork:
    """The interface of the default route, its IPv4 networks and its gateway."""

    interface: str
    interfaces: tuple[ipaddress.IPv4Interface, ...]
    gateway: ipaddress.IPv4Address | None


def _ip_json(*args: str) -> list[dict]:
    result = command.run(["ip", "-j", *args], capture=True, check=False)
    if result.returncode != 0 or not result.stdout.strip():
        return []
    return json.loads(result.stdout)


def host_network() -> HostNetwork | None:
    """Read the default-route interface; None when it cannot be determined (no `ip`, no route)."""
    if shutil.which("ip") is None:
        return None
    routes = _ip_json("-4", "route", "show", "default")
    if not routes:
        return None
    dev = routes[0].get("dev")
    gateway = routes[0].get("gateway")
    interfaces = tuple(
        ipaddress.IPv4Interface(f"{addr['local']}/{addr['prefixlen']}")
        for link in _ip_json("-4", "addr", "show", "dev", dev)
        for addr in link.get("addr_info", [])
        if addr.get("family", "inet") == "inet"
    )
    if not interfaces:
        return None
    return HostNetwork(dev, interfaces, ipaddress.IPv4Address(gateway) if gateway else None)


def route_device(ip: str) -> str | None:
    """The interface the kernel would use to reach ``ip`` (longest-prefix match)."""
    routes = _ip_json("-4", "route", "get", ip)
    return routes[0].get("dev") if routes else None


def neighbours() -> set[str]:
    """Addresses currently seen on the network (ARP table)."""
    return {n["dst"] for n in _ip_json("-4", "neigh", "show") if "dst" in n}


def loadbalancer_ip_problem(
    ip: str,
    network: HostNetwork,
    route_dev: Callable[[str], str | None] = route_device,
) -> str | None:
    """Explain why ``ip`` cannot be the Traefik LoadBalancer IP, or None when it can.

    MetalLB answers ARP for it on the node, so it must sit in the default-route interface's subnet,
    be free there, and be routed through that interface (not shadowed by docker0 or another bridge).
    """
    address = ipaddress.IPv4Address(ip)
    subnet = next((i.network for i in network.interfaces if address in i.network), None)
    if subnet is None:
        nets = ", ".join(str(i.network) for i in network.interfaces)
        return f"{ip} is outside the {network.interface} subnet ({nets})"
    if address in (subnet.network_address, subnet.broadcast_address):
        return f"{ip} is the network or broadcast address of {subnet}"
    if address == network.gateway or any(address == i.ip for i in network.interfaces):
        return f"{ip} is already used by the host or its gateway"
    dev = route_dev(ip)
    if dev and dev != network.interface:
        return f"{ip} is routed through {dev}, not {network.interface} (overlapping subnet)"
    return None


def suggest_loadbalancer_ip(
    network: HostNetwork,
    route_dev: Callable[[str], str | None] = route_device,
    taken: set[str] | None = None,
) -> str | None:
    """A free address of the default-route subnet: offset 200 and up, then lower."""
    taken = taken if taken is not None else neighbours()
    subnet = network.interfaces[0].network
    size = subnet.num_addresses
    offsets = [*range(200, min(size - 1, 255)), *range(min(199, size - 2), 1, -1)]
    for offset in offsets:
        candidate = str(subnet.network_address + offset)
        if candidate in taken:
            continue
        if loadbalancer_ip_problem(candidate, network, route_dev) is None:
            return candidate
    return None

import ipaddress

from workstation_bootstrap import host

# A typical WSL2 NAT eth0, and Docker's default bridge overlapping 172.17.0.0/16.
WSL = host.HostNetwork(
    "eth0", (ipaddress.IPv4Interface("172.24.80.5/20"),), ipaddress.IPv4Address("172.24.80.1")
)


def routes(ip: str) -> str:
    return (
        "docker0" if ipaddress.IPv4Address(ip) in ipaddress.IPv4Network("172.17.0.0/16") else "eth0"
    )


def test_the_readme_example_ip_is_refused_when_eth0_is_elsewhere_and_docker_owns_it() -> None:
    assert "outside the eth0 subnet" in host.loadbalancer_ip_problem("172.17.47.200", WSL, routes)


def test_an_address_of_the_subnet_shadowed_by_a_bridge_route_is_refused() -> None:
    wsl_in_docker_range = host.HostNetwork(
        "eth0", (ipaddress.IPv4Interface("172.17.46.91/20"),), ipaddress.IPv4Address("172.17.32.1")
    )
    problem = host.loadbalancer_ip_problem(
        "172.17.47.200", wsl_in_docker_range, lambda ip: "docker0"
    )
    assert problem and "routed through docker0" in problem


def test_host_gateway_network_and_broadcast_addresses_are_refused() -> None:
    for ip in ("172.24.80.5", "172.24.80.1", "172.24.80.0", "172.24.95.255"):
        assert host.loadbalancer_ip_problem(ip, WSL, routes), ip


def test_a_free_address_of_the_subnet_is_accepted_and_suggested() -> None:
    assert host.loadbalancer_ip_problem("172.24.80.200", WSL, routes) is None
    assert host.suggest_loadbalancer_ip(WSL, routes, taken={"172.24.80.200"}) == "172.24.80.201"


def test_inotify_shortfall_lists_only_the_limits_below_the_minimum() -> None:
    values = {"fs.inotify.max_user_instances": 128, "fs.inotify.max_user_watches": 2_000_000}
    assert host.inotify_shortfall(values.get) == {"fs.inotify.max_user_instances": 128}
    assert "fs.inotify.max_user_instances = 8192" in host.sysctl_dropin()

"""MetalLB runs in L2 mode on a single node: nothing in it may need a second node to roll out."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

CHART = Path(__file__).resolve().parents[1] / "umbrella-charts" / "core-stack" / "metallb"

pytestmark = pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")


def render() -> list[dict]:
    out = subprocess.run(
        ["helm", "template", "metallb", str(CHART), "--namespace", "metallb-system"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return [d for d in yaml.safe_load_all(out) if d]


def test_no_host_network_deployment_that_could_not_roll_out_on_one_node() -> None:
    deployments = [d for d in render() if d["kind"] == "Deployment"]
    assert [d["metadata"]["name"] for d in deployments] == ["metallb-controller"]
    assert not any(d["spec"]["template"]["spec"].get("hostNetwork") for d in deployments)


def test_l2_speaker_without_the_bgp_backend() -> None:
    objs = render()
    daemonsets = {d["metadata"]["name"]: d for d in objs if d["kind"] == "DaemonSet"}
    assert list(daemonsets) == ["metallb-speaker"]
    containers = daemonsets["metallb-speaker"]["spec"]["template"]["spec"]["containers"]
    assert [c["name"] for c in containers] == ["speaker"]
    crds = {d["metadata"]["name"] for d in objs if d["kind"] == "CustomResourceDefinition"}
    assert {"ipaddresspools.metallb.io", "l2advertisements.metallb.io"} <= crds
    assert not any(c.endswith("frrk8s.metallb.io") for c in crds)

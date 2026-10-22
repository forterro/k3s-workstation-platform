"""The Keycloak chart syncs its database before anything that needs it."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

CHART = Path(__file__).resolve().parents[1] / "umbrella-charts" / "identity" / "keycloak"
WAVE = "argocd.argoproj.io/sync-wave"

pytestmark = pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")


def render() -> dict[tuple[str, str], dict]:
    out = subprocess.run(
        ["helm", "template", "kc", str(CHART), "--namespace", "keycloak"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return {(d["kind"], d["metadata"]["name"]): d for d in yaml.safe_load_all(out) if d}


def wave(obj: dict) -> int:
    return int((obj["metadata"].get("annotations") or {}).get(WAVE, "0"))


def test_database_cluster_syncs_before_the_job_waiting_for_its_secret() -> None:
    objs = render()
    cluster = objs[("Cluster", "keycloak-pg")]
    job = objs[("Job", "keycloak-db-init")]
    secrets = {
        e["valueFrom"]["secretKeyRef"]["name"]
        for e in job["spec"]["template"]["spec"]["containers"][0]["env"]
        if "valueFrom" in e
    }
    # KubeBlocks names the account Secret after the Cluster.
    assert secrets == {"keycloak-pg-postgresql-account-postgres"}
    # ArgoCD waits for a wave to be healthy before the next one: the Job must not come first.
    assert wave(cluster) < wave(job)

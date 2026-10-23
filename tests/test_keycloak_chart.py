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


def test_admins_manage_the_workstation_realm_with_their_own_account() -> None:
    objs = render()
    group = objs[("KeycloakRealmGroup", "workstation-workstation-admins")]["spec"]
    assert group["name"] == "workstation-admins"
    assert {"clientId": "realm-management", "roles": ["realm-admin"]} in group["clientRoles"]


def test_a_username_keycloak_would_refuse_fails_the_render_instead_of_never_being_created() -> None:
    def helm(username: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                "helm",
                "template",
                "kc",
                str(CHART),
                "--namespace",
                "keycloak",
                "--set",
                f"users[0].username={username}",
                "--set",
                "users[0].email=x@example.com",
            ],
            capture_output=True,
            text=True,
        )

    short = helm("sv")
    assert short.returncode != 0 and 'username "sv" is too short' in short.stderr
    assert helm("seb").returncode == 0

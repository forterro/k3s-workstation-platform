"""The shared chat: people sign in through the workstation SSO; bots join as appservices."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

CHART = Path(__file__).resolve().parents[1] / "umbrella-charts" / "collaboration" / "matrix"
HARNESS = [
    "--set",
    "appservices[0].name=agent-harness",
    "--set",
    "appservices[0].namespace=agent-harness",
    "--set",
    "appservices[0].serviceAccount=agent-harness",
]

pytestmark = pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")


def render(*args: str, crd: bool = True) -> dict[tuple[str, str], dict]:
    # Argo CD passes the cluster's API versions; the keycloak app brings KeycloakClient.
    api = ["--api-versions", "v1.edp.epam.com/v1"] if crd else []
    out = subprocess.run(
        ["helm", "template", "matrix", str(CHART), "--namespace", "matrix", *api, *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return {(d["kind"], d["metadata"]["name"]): d for d in yaml.safe_load_all(out) if d}


def pod(objs: dict) -> dict:
    return objs[("StatefulSet", "matrix")]["spec"]["template"]["spec"]


def test_people_sign_in_through_the_workstation_sso_only() -> None:
    objs = render()
    toml = objs[("ConfigMap", "matrix")]["data"]["tuwunel.toml"]
    assert 'issuer_url = "https://sso.workstation.internal/realms/workstation"' in toml
    assert "login_with_password = false" in toml and "allow_registration = false" in toml
    assert "unique_id_fallbacks = false" in toml  # never a random Matrix ID
    client = objs[("KeycloakClient", "matrix")]["spec"]
    callback = (
        "https://matrix.workstation.internal/_matrix/client/unstable/login/sso/callback/matrix"
    )
    assert client["public"] is False and client["redirectUris"] == [callback]
    volumes = {v["name"]: v for v in pod(objs)["volumes"]}
    # The client secret the keycloak operator generates for the KeycloakClient.
    assert volumes["oidc"]["secret"]["secretName"] == "keycloak-client-matrix-secret"
    assert volumes["workstation-ca"]["configMap"]["name"] == "workstation-ca"


def test_the_client_waits_for_the_keycloak_crd_instead_of_failing_the_sync() -> None:
    objs = render(crd=False)
    assert ("StatefulSet", "matrix") in objs and ("KeycloakClient", "matrix") not in objs


def test_each_appservice_consumer_writes_only_its_own_registration() -> None:
    assert "projected" not in {v["name"]: v for v in pod(render())["volumes"]}["appservices"]
    objs = render(*HARNESS)
    sources = {v["name"]: v for v in pod(objs)["volumes"]}["appservices"]["projected"]["sources"]
    assert sources == [{"secret": {"name": "matrix-appservice-agent-harness", "optional": True}}]
    rules = objs[("Role", "matrix-appservice-agent-harness")]["rules"]
    named = [r for r in rules if r.get("resourceNames")]
    assert {(r["resources"][0], tuple(r["resourceNames"])) for r in named} == {
        ("secrets", ("matrix-appservice-agent-harness",)),
        ("statefulsets", ("matrix",)),
    }
    assert all("get" not in r["verbs"] for r in rules if not r.get("resourceNames"))
    subject = objs[("RoleBinding", "matrix-appservice-agent-harness")]["subjects"][0]
    assert (subject["namespace"], subject["name"]) == ("agent-harness", "agent-harness")

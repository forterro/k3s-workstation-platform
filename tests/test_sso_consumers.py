"""Tools signing in through the workstation Keycloak: client and settings exist only with SSO."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

CHARTS = Path(__file__).resolve().parents[1] / "umbrella-charts"
CA = "/etc/workstation-ca/ca-certificates.crt"

pytestmark = pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")


def render(release: str, chart: str, sso: bool) -> dict[tuple[str, str], dict]:
    args = ["helm", "template", release, str(CHARTS / chart), "--namespace", release]
    if sso:
        args += ["-f", str(CHARTS / chart / "values-sso.yaml")]
    out = subprocess.run(args, check=True, capture_output=True, text=True).stdout
    return {(d["kind"], d["metadata"]["name"]): d for d in yaml.safe_load_all(out) if d}


@pytest.mark.parametrize("chart", ["kube-mgmt/argocd", "observability/kube-prometheus-stack"])
def test_no_keycloak_client_without_sso(chart: str) -> None:
    assert not [k for k in render("t", chart, sso=False) if k[0] == "KeycloakClient"]


def test_argocd_signs_in_with_a_public_pkce_client_and_maps_admins() -> None:
    objs = render("argocd", "kube-mgmt/argocd", sso=True)
    client = objs[("KeycloakClient", "argocd")]["spec"]
    assert client["public"] is True
    assert client["attributes"]["pkce.code.challenge.method"] == "S256"
    assert "https://argocd.workstation.internal/pkce/verify" in client["redirectUris"]
    assert "groups" in client["defaultClientScopes"]

    oidc = yaml.safe_load(objs[("ConfigMap", "argocd-cm")]["data"]["oidc.config"])
    assert oidc["clientID"] == "argocd" and oidc["enablePKCEAuthentication"] is True
    assert "clientSecret" not in oidc
    rbac = objs[("ConfigMap", "argocd-rbac-cm")]["data"]
    assert rbac["scopes"] == "[groups]"
    assert "g, workstation-admins, role:admin" in rbac["policy.csv"]

    server = objs[("Deployment", "argocd-server")]["spec"]["template"]["spec"]
    assert {"name": "SSL_CERT_FILE", "value": CA} in server["containers"][0]["env"]
    assert {"name": "workstation-ca", "configMap": {"name": "workstation-ca"}} in server["volumes"]


def test_grafana_uses_the_operator_generated_secret_and_trusts_the_workstation_ca() -> None:
    objs = render("kube-prometheus-stack", "observability/kube-prometheus-stack", sso=True)
    client = objs[("KeycloakClient", "grafana")]["spec"]
    assert client["public"] is False
    assert client["redirectUris"] == ["https://grafana.workstation.internal/login/generic_oauth"]

    ini = objs[("ConfigMap", "grafana")]["data"]["grafana.ini"]
    assert "client_id = grafana" in ini and f"tls_client_ca = {CA}" in ini
    roles = "contains(groups[*], 'workstation-admins') && 'Admin' || 'Viewer'"
    assert f"role_attribute_path = {roles}" in ini

    pod = objs[("Deployment", "grafana")]["spec"]["template"]["spec"]
    grafana = next(c for c in pod["containers"] if c["name"] == "grafana")
    secret = next(e for e in grafana["env"] if e["name"] == "GF_AUTH_GENERIC_OAUTH_CLIENT_SECRET")
    # The operator names the generated secret after the KeycloakClient.
    assert secret["valueFrom"]["secretKeyRef"] == {
        "name": "keycloak-client-grafana-secret",
        "key": "clientSecret",
    }

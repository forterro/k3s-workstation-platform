"""The platform chart renders the base Applications, optionally with the config repo overlays."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
CHART = ROOT / "bootstrap" / "helm" / "platform"

pytestmark = pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")


def render(*args: str) -> dict[str, dict]:
    out = subprocess.run(
        ["helm", "template", "root", str(CHART), "--namespace", "argocd", *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return {d["metadata"]["name"]: _normalize(d) for d in yaml.safe_load_all(out) if d}


def _normalize(app: dict) -> dict:
    """Sync options are a set for ArgoCD: compare them without order."""
    app["spec"]["syncPolicy"]["syncOptions"] = sorted(app["spec"]["syncPolicy"]["syncOptions"])
    return app


def test_standalone_renders_every_base_component_without_overlay() -> None:
    apps = render()
    assert set(apps) == {
        "prometheus-operator-crds",
        "cert-manager",
        "metallb",
        "kubeblocks",
        "workstation-dns",
        "traefik",
        "argocd",
        "headlamp",
        "kube-prometheus-stack",
        "loki",
        "alloy",
        "step-ca",
        "workstation-dashboards",
        "trust-manager",
    }
    for app in apps.values():
        assert app["spec"]["source"]["targetRevision"] == "main"
        assert "finalizers" not in app["metadata"]  # a pruned app must never delete its workloads
    assert apps["kubeblocks"]["metadata"]["annotations"] == {
        "argocd.argoproj.io/sync-wave": "-1",
        "argocd.argoproj.io/compare-options": "ServerSideDiff=true",
    }
    assert apps["argocd"]["spec"]["syncPolicy"]["syncOptions"] == ["ServerSideApply=true"]


def test_config_repo_adds_an_optional_overlay_per_app() -> None:
    apps = render("--set", "configRepoURL=https://git.example/cfg.git")
    spec = apps["argocd"]["spec"]
    assert "source" not in spec
    chart, values = spec["sources"]
    assert chart["path"] == "umbrella-charts/kube-mgmt/argocd"
    assert chart["helm"] == {
        "valueFiles": ["$values/values/argocd.yaml"],
        "ignoreMissingValueFiles": True,
    }
    assert values == {
        "repoURL": "https://git.example/cfg.git",
        "targetRevision": "main",
        "ref": "values",
    }

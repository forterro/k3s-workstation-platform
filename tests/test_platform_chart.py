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


def test_standalone_matches_the_legacy_apps_directory() -> None:
    legacy = {}
    for f in (ROOT / "apps").glob("*.yaml"):
        doc = _normalize(yaml.safe_load(f.read_text(encoding="utf-8")))
        legacy[doc["metadata"]["name"]] = doc
    assert render() == legacy


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

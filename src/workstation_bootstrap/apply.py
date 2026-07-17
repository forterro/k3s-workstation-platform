"""Ordered, server-side apply of rendered manifests.

CustomResourceDefinitions are applied first and waited on until Established, so that custom
resources depending on them apply cleanly in the second pass.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from . import command, console
from .k3s import KUBECONFIG

_SEPARATOR = re.compile(r"^---\s*$", re.MULTILINE)


def _documents(manifests: str) -> list[str]:
    return [doc.strip() for doc in _SEPARATOR.split(manifests) if doc.strip()]


def _parsed(document: str) -> dict | None:
    try:
        data = yaml.safe_load(document)
    except yaml.YAMLError:
        return None
    return data if isinstance(data, dict) else None


def split_crds(manifests: str) -> tuple[list[str], list[str]]:
    """Split rendered documents into (CRDs, everything else)."""
    crds: list[str] = []
    rest: list[str] = []
    for document in _documents(manifests):
        parsed = _parsed(document)
        if parsed and parsed.get("kind") == "CustomResourceDefinition":
            crds.append(document)
        else:
            rest.append(document)
    return crds, rest


def _crd_name(document: str) -> str | None:
    parsed = _parsed(document)
    if parsed:
        return parsed.get("metadata", {}).get("name")
    return None


def _kubectl(
    args: list[str],
    *,
    input_text: str | None = None,
    kubeconfig: Path = KUBECONFIG,
) -> None:
    command.run(["kubectl", "--kubeconfig", str(kubeconfig), *args], input_text=input_text)


def _apply(documents: list[str], kubeconfig: Path) -> None:
    if not documents:
        return
    manifest = "\n---\n".join(documents)
    _kubectl(
        ["apply", "--server-side", "--force-conflicts", "-f", "-"],
        input_text=manifest,
        kubeconfig=kubeconfig,
    )


def _wait_crds_established(crds: list[str], kubeconfig: Path) -> None:
    for name in filter(None, (_crd_name(document) for document in crds)):
        _kubectl(
            ["wait", "--for=condition=Established", f"crd/{name}", "--timeout=90s"],
            kubeconfig=kubeconfig,
        )


def ensure_namespace(namespace: str, kubeconfig: Path = KUBECONFIG) -> None:
    """Create the namespace if it does not exist (kubectl apply does not create it)."""
    manifest = f"apiVersion: v1\nkind: Namespace\nmetadata:\n  name: {namespace}\n"
    _kubectl(
        ["apply", "--server-side", "-f", "-"],
        input_text=manifest,
        kubeconfig=kubeconfig,
    )


def apply_manifests(manifests: str, kubeconfig: Path = KUBECONFIG) -> None:
    """Apply rendered manifests, CRDs first."""
    crds, rest = split_crds(manifests)
    if crds:
        console.sub(f"applying {len(crds)} CRD(s)")
        _apply(crds, kubeconfig)
        _wait_crds_established(crds, kubeconfig)
    if rest:
        console.sub(f"applying {len(rest)} resource(s)")
        _apply(rest, kubeconfig)

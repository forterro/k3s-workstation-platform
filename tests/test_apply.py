"""Tests for manifest splitting."""

from __future__ import annotations

from workstation_bootstrap.apply import split_crds

_CRD = """
apiVersion: apiextensions.k8s.io/v1
kind: CustomResourceDefinition
metadata:
  name: widgets.example.com
"""

_DEPLOYMENT = """
apiVersion: apps/v1
kind: Deployment
metadata:
  name: app
"""


def test_split_crds_separates_documents() -> None:
    crds, rest = split_crds(_CRD + "\n---\n" + _DEPLOYMENT)
    assert len(crds) == 1
    assert len(rest) == 1
    assert "CustomResourceDefinition" in crds[0]
    assert "Deployment" in rest[0]


def test_split_crds_drops_empty_documents() -> None:
    crds, rest = split_crds(_DEPLOYMENT + "\n---\n\n---\n" + _CRD)
    assert len(crds) == 1
    assert len(rest) == 1


def test_split_crds_without_crds() -> None:
    crds, rest = split_crds(_DEPLOYMENT)
    assert crds == []
    assert len(rest) == 1

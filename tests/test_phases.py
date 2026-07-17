import pytest

from workstation_bootstrap import phases


def test_ensure_sops_age_secret_applies_namespace_and_secret(monkeypatch, tmp_path):
    key_file = tmp_path / "keys.txt"
    key_file.write_text("AGE-SECRET-KEY-TEST", encoding="utf-8")
    monkeypatch.setattr(phases.tools, "age_key_path", lambda: key_file)

    calls: list[list[str]] = []

    class _Result:
        stdout = "rendered-manifest"

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        return _Result()

    monkeypatch.setattr(phases.command, "run", fake_run)

    phases._ensure_sops_age_secret(phases.Context(root=tmp_path, dry_run=False))

    joined = [" ".join(cmd) for cmd in calls]
    assert any("create namespace argocd" in c for c in joined)
    assert any("create secret generic sops-age" in c for c in joined)
    assert any(f"--from-file=keys.txt={key_file}" in c for c in joined)
    assert sum(1 for c in joined if c.startswith("kubectl apply -f -")) == 2


def test_ensure_sops_age_secret_requires_age_key(monkeypatch, tmp_path):
    missing = tmp_path / "absent.txt"
    monkeypatch.setattr(phases.tools, "age_key_path", lambda: missing)

    with pytest.raises(phases.PhaseError):
        phases._ensure_sops_age_secret(phases.Context(root=tmp_path, dry_run=False))


def test_clusterissuer_manifest_embeds_root_ca_bundle(tmp_path):
    ca = tmp_path
    (ca / "root_ca.crt").write_bytes(b"ROOT-CERT-PEM")

    manifest = phases._clusterissuer_manifest(ca)

    assert "kind: ClusterIssuer" in manifest
    assert "name: step-ca-acme" in manifest
    assert "caBundle: Uk9PVC1DRVJULVBFTQ==" in manifest
    assert "server: https://step-ca.step-ca.svc.cluster.local/acme/acme/directory" in manifest
    assert "ingressClassName: traefik" in manifest


def test_ensure_local_ca_skips_when_present(monkeypatch, tmp_path):
    ca = tmp_path / "ca"
    ca.mkdir()
    (ca / "root_ca.crt").write_text("cert", encoding="utf-8")
    monkeypatch.setattr(phases.settings, "ca_dir", lambda: ca)

    def fail_run(*args, **kwargs):
        raise AssertionError("generate-ca.sh should not run when the CA already exists")

    monkeypatch.setattr(phases.command, "run", fail_run)

    assert phases._ensure_local_ca(phases.Context(root=tmp_path, dry_run=False)) == ca


def test_phase_step_ca_material_applies_all_resources(monkeypatch, tmp_path):
    ca = tmp_path / "ca"
    ca.mkdir()
    for name in (
        "root_ca.crt",
        "intermediate_ca.crt",
        "ca.json",
        "root_ca_key",
        "intermediate_ca_key",
        "ca.pass",
    ):
        (ca / name).write_text(name, encoding="utf-8")
    monkeypatch.setattr(phases.settings, "ca_dir", lambda: ca)

    calls: list[list[str]] = []

    class _Result:
        stdout = "manifest"

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        return _Result()

    monkeypatch.setattr(phases.command, "run", fake_run)

    phases._phase_step_ca_material(phases.Context(root=tmp_path, dry_run=False))

    joined = [" ".join(c) for c in calls]
    assert any("create namespace step-ca" in c for c in joined)
    assert any("create configmap step-ca-certs" in c for c in joined)
    assert any("create configmap step-ca-config" in c for c in joined)
    assert any("create secret generic step-ca-secrets" in c for c in joined)
    assert any("create secret generic step-ca-ca-password" in c for c in joined)
    assert sum(1 for c in joined if c.startswith("kubectl apply -f -")) == 6
    assert any(
        "annotate --local" in c and "argocd.argoproj.io/compare-options=IgnoreExtraneous" in c
        for c in joined
    )

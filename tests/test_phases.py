import json

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


def test_phase_grafana_admin_secret_applies_namespace_and_secret(monkeypatch, tmp_path):
    monkeypatch.setattr(phases.settings, "ensure_grafana_admin_password", lambda: "s3cr3t-pw")

    calls: list[list[str]] = []

    class _Result:
        stdout = "rendered-manifest"

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        return _Result()

    monkeypatch.setattr(phases.command, "run", fake_run)

    phases._phase_grafana_admin_secret(phases.Context(root=tmp_path, dry_run=False))

    joined = [" ".join(cmd) for cmd in calls]
    assert any("create namespace observability" in c for c in joined)
    assert any("create secret generic grafana-admin" in c for c in joined)
    assert any("--from-literal=admin-user=admin" in c for c in joined)
    assert any("--from-literal=admin-password=s3cr3t-pw" in c for c in joined)
    assert any("--namespace observability" in c for c in joined)



def test_clusterissuer_manifest_embeds_root_ca_bundle(tmp_path):
    ca = tmp_path
    (ca / "root_ca.crt").write_bytes(b"ROOT-CERT-PEM")

    manifest = phases._clusterissuer_manifest(ca)

    assert "kind: ClusterIssuer" in manifest
    assert "name: step-ca-acme" in manifest
    assert "caBundle: Uk9PVC1DRVJULVBFTQ==" in manifest
    assert "server: https://step-ca.step-ca.svc.cluster.local/acme/acme/directory" in manifest
    assert "ingressClassName: traefik" in manifest


def test_normalize_ca_json_rewrites_paths_to_container_layout(tmp_path):
    ca = tmp_path
    (ca / "ca.json").write_text(
        json.dumps(
            {
                "root": "/tmp/tmp.abc/certs/root_ca.crt",
                "crt": "/tmp/tmp.abc/certs/intermediate_ca.crt",
                "key": "/tmp/tmp.abc/secrets/intermediate_ca_key",
                "db": {"type": "badgerv2", "dataSource": "/tmp/tmp.abc/db"},
            }
        ),
        encoding="utf-8",
    )

    phases._normalize_ca_json(ca)

    data = json.loads((ca / "ca.json").read_text(encoding="utf-8"))
    assert data["root"] == "/home/step/certs/root_ca.crt"
    assert data["crt"] == "/home/step/certs/intermediate_ca.crt"
    assert data["key"] == "/home/step/secrets/intermediate_ca_key"
    assert data["db"]["dataSource"] == "/home/step/db"


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
        "root_ca_key",
        "intermediate_ca_key",
        "ca.pass",
    ):
        (ca / name).write_text(name, encoding="utf-8")
    (ca / "ca.json").write_text(json.dumps({"db": {"dataSource": "/tmp/x/db"}}), encoding="utf-8")
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


def test_metallb_pool_manifest_pins_single_address():
    manifest = phases._metallb_pool_manifest("172.17.47.200")

    assert "kind: IPAddressPool" in manifest
    assert "name: workstation-pool" in manifest
    assert "namespace: metallb-system" in manifest
    assert "- 172.17.47.200/32" in manifest
    assert "kind: L2Advertisement" in manifest
    assert "- workstation-pool" in manifest


def test_phase_metallb_installs_chart_and_applies_pool(monkeypatch, tmp_path):
    chart = tmp_path / "umbrella-charts" / "core-stack" / "metallb"
    chart.mkdir(parents=True)

    installed: list[dict] = []
    monkeypatch.setattr(phases.helm, "update_dependencies", lambda c: None)
    monkeypatch.setattr(phases.helm, "release_exists", lambda release, namespace: False)
    monkeypatch.setattr(
        phases.helm,
        "install",
        lambda c, release, namespace, values, set_values=(): installed.append(
            {"release": release, "namespace": namespace}
        ),
    )
    monkeypatch.setattr(
        phases.settings, "ensure_loadbalancer_ip", lambda root, override=None: "172.17.47.200"
    )

    applied: list[str] = []

    def fake_run(cmd, **kwargs):
        if cmd[:3] == ["kubectl", "apply", "-f"]:
            applied.append(kwargs.get("input_text", ""))

        class _Result:
            stdout = ""

        return _Result()

    monkeypatch.setattr(phases.command, "run", fake_run)

    phases._phase_metallb(phases.Context(root=tmp_path, dry_run=False))

    assert installed == [{"release": "metallb", "namespace": "metallb-system"}]
    assert any("kind: IPAddressPool" in m and "172.17.47.200/32" in m for m in applied)


def test_apply_seed_chart_skips_when_release_exists(monkeypatch, tmp_path):
    chart = tmp_path / "umbrella-charts" / "core-stack" / "cert-manager"
    chart.mkdir(parents=True)

    installed: list[dict] = []
    monkeypatch.setattr(phases.helm, "update_dependencies", lambda c: None)
    monkeypatch.setattr(phases.helm, "release_exists", lambda release, namespace: True)
    monkeypatch.setattr(phases.helm, "install", lambda *a, **k: installed.append(k))

    phases._apply_seed_chart(
        phases.Context(root=tmp_path, dry_run=False),
        group="core-stack",
        brick="cert-manager",
        namespace="cert-manager",
        release="cert-manager",
    )

    assert installed == []


def test_reconfigure_loadbalancer_ip_reapplies_and_restarts(monkeypatch, tmp_path):
    monkeypatch.setattr(
        phases.settings, "ensure_loadbalancer_ip", lambda root, override=None: override
    )

    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))

        class _Result:
            stdout = ""

        return _Result()

    monkeypatch.setattr(phases.command, "run", fake_run)

    phases.reconfigure_loadbalancer_ip(
        phases.Context(root=tmp_path, dry_run=False), "172.17.47.210"
    )

    joined = [" ".join(c) for c in calls]
    assert any("kubectl apply -f -" in c for c in joined)
    assert any("kubectl -n traefik rollout restart deployment" in c for c in joined)

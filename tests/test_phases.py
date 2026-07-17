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

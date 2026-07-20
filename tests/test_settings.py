from pathlib import Path

import pytest
import yaml

from workstation_bootstrap import settings


def _redirect_config(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(settings, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(settings, "CONFIG_FILE", tmp_path / "config.yaml")


def test_load_or_init_config_detects_origin(monkeypatch, tmp_path):
    _redirect_config(monkeypatch, tmp_path)
    monkeypatch.setattr(settings, "_detect_repo_url", lambda root: "https://example.com/fork.git")

    config = settings.load_or_init_config(tmp_path)

    assert config["platform_repo_url"] == "https://example.com/fork.git"
    assert config["platform_revision"] == settings.DEFAULT_REVISION
    persisted = yaml.safe_load((tmp_path / "config.yaml").read_text(encoding="utf-8"))
    assert persisted == config


def test_load_or_init_config_falls_back_to_default(monkeypatch, tmp_path):
    _redirect_config(monkeypatch, tmp_path)
    monkeypatch.setattr(settings, "_detect_repo_url", lambda root: None)

    config = settings.load_or_init_config(tmp_path)

    assert config["platform_repo_url"] == settings.DEFAULT_REPO_URL


def test_load_or_init_config_reuses_existing(monkeypatch, tmp_path):
    _redirect_config(monkeypatch, tmp_path)
    existing = {"platform_repo_url": "https://example.com/kept.git", "platform_revision": "dev"}
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(existing), encoding="utf-8")

    config = settings.load_or_init_config(tmp_path)

    assert config == existing


def test_render_root_app_values(monkeypatch, tmp_path):
    _redirect_config(monkeypatch, tmp_path)
    config = {"platform_repo_url": "https://example.com/fork.git", "platform_revision": "dev"}

    path = settings.render_root_app_values(config)

    rendered = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert rendered["rootApp"]["repoURL"] == "https://example.com/fork.git"
    assert rendered["rootApp"]["targetRevision"] == "dev"


def test_ensure_loadbalancer_ip_reuses_existing(monkeypatch, tmp_path):
    _redirect_config(monkeypatch, tmp_path)
    existing = {"platform_repo_url": "https://example.com/kept.git", "loadbalancer_ip": "10.0.0.5"}
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(existing), encoding="utf-8")

    def fail_prompt() -> str:
        raise AssertionError("must not prompt when the IP is already stored")

    monkeypatch.setattr(settings, "_prompt_loadbalancer_ip", fail_prompt)

    assert settings.ensure_loadbalancer_ip(tmp_path) == "10.0.0.5"


def test_ensure_loadbalancer_ip_prompts_and_persists(monkeypatch, tmp_path):
    _redirect_config(monkeypatch, tmp_path)
    monkeypatch.setattr(settings, "_detect_repo_url", lambda root: None)
    monkeypatch.setattr(settings, "_prompt_loadbalancer_ip", lambda: "172.17.47.200")

    ip = settings.ensure_loadbalancer_ip(tmp_path)

    assert ip == "172.17.47.200"
    persisted = yaml.safe_load((tmp_path / "config.yaml").read_text(encoding="utf-8"))
    assert persisted["loadbalancer_ip"] == "172.17.47.200"


def test_ensure_grafana_admin_password_generates_and_persists(monkeypatch, tmp_path):
    _redirect_config(monkeypatch, tmp_path)

    password = settings.ensure_grafana_admin_password()

    assert password
    path = settings.grafana_admin_password_path()
    assert path.read_text(encoding="utf-8").strip() == password
    assert path.stat().st_mode & 0o777 == 0o600


def test_ensure_grafana_admin_password_reuses_existing(monkeypatch, tmp_path):
    _redirect_config(monkeypatch, tmp_path)
    path = settings.grafana_admin_password_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("kept-password\n", encoding="utf-8")

    assert settings.ensure_grafana_admin_password() == "kept-password"



def test_ensure_loadbalancer_ip_override_replaces_value(monkeypatch, tmp_path):
    _redirect_config(monkeypatch, tmp_path)
    existing = {"platform_repo_url": "https://example.com/kept.git", "loadbalancer_ip": "10.0.0.5"}
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(existing), encoding="utf-8")

    ip = settings.ensure_loadbalancer_ip(tmp_path, override="172.17.47.201")

    assert ip == "172.17.47.201"
    persisted = yaml.safe_load((tmp_path / "config.yaml").read_text(encoding="utf-8"))
    assert persisted["loadbalancer_ip"] == "172.17.47.201"


def test_ensure_loadbalancer_ip_rejects_invalid_override(monkeypatch, tmp_path):
    _redirect_config(monkeypatch, tmp_path)
    monkeypatch.setattr(settings, "_detect_repo_url", lambda root: None)

    with pytest.raises(ValueError):
        settings.ensure_loadbalancer_ip(tmp_path, override="not-an-ip")


def test_ensure_loadbalancer_ip_non_interactive_requires_value(monkeypatch, tmp_path):
    _redirect_config(monkeypatch, tmp_path)
    monkeypatch.setattr(settings, "_detect_repo_url", lambda root: None)

    with pytest.raises(ValueError):
        settings.ensure_loadbalancer_ip(tmp_path, interactive=False)

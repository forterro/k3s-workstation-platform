from pathlib import Path

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

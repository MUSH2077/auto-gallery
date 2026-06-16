import asyncio
import json

from app.services.settings import (
    apply_gallerydl_defaults,
    build_effective_gallerydl_config,
    ensure_gallerydl_config,
    extractor_key_for_source,
    merge_gallerydl_effective_config,
    source_key_for_extractor,
)
from app.api.admin import GalleryDLMultiConfig, PixivSourceConfig, _rebuild_managed_postprocessors, update_gallerydl_config


def test_x_source_maps_to_twitter_extractor():
    assert extractor_key_for_source("x") == "twitter"
    assert source_key_for_extractor("twitter") == "x"


def test_effective_gallerydl_config_keeps_user_values_and_fills_defaults():
    provider_cfg = {
        "extractor": {
            "twitter": {
                "cookies": "/gallerydl-config/cookies/twitter.txt",
                "strategy": "media",
                "filename": "{id}.{extension}",
            }
        }
    }
    user_cfg = {
        "extractor": {
            "twitter": {
                "cookies": "/custom/twitter.txt",
                "strategy": "tweets",
                "directory": ["x", "{user[name]}"],
            }
        },
        "postprocessors": [{"name": "metadata"}],
    }

    effective = merge_gallerydl_effective_config("x", provider_cfg, user_cfg)
    twitter = effective["extractor"]["twitter"]
    assert twitter["cookies"] == "/custom/twitter.txt"
    assert twitter["strategy"] == "tweets"
    assert twitter["directory"] == ["x", "{user[name]}"]
    assert twitter["filename"] == "{id}.{extension}"
    assert effective["postprocessors"] == [{"name": "metadata"}]


def test_gallerydl_defaults_fill_missing_file_organization_without_overwriting():
    config = {
        "extractor": {
            "pixiv": {
                "directory": ["custom", "{id}"],
                "filename": "{id}.{extension}",
                "refresh-token": "keep-token",
            },
            "lofter": {"auto-enable-on-import": True},
        },
        "postprocessors": [{"name": "custom"}],
    }

    merged, changed = apply_gallerydl_defaults(config)

    assert changed is True
    assert merged["extractor"]["pixiv"]["directory"] == ["custom", "{id}"]
    assert merged["extractor"]["pixiv"]["filename"] == "{id}.{extension}"
    assert merged["extractor"]["pixiv"]["refresh-token"] == "keep-token"
    assert merged["extractor"]["lofter"]["directory"] == ["lofter", "{blog_name}", "{id}"]
    assert merged["extractor"]["lofter"]["filename"] == "{id}_{num}.{extension}"
    assert {"name": "custom"} in merged["postprocessors"]
    assert any(pp.get("name") == "metadata" for pp in merged["postprocessors"])


def test_ensure_gallerydl_config_creates_complete_default_file(tmp_path):
    config_path = tmp_path / "config.json"

    config = ensure_gallerydl_config(config_path)

    assert config_path.exists()
    assert config["extractor"]["lofter"]["directory"] == ["lofter", "{blog_name}", "{id}"]
    assert config["extractor"]["danbooru"]["filename"] == "{id}_{num}.{extension}"
    assert any(pp.get("name") == "metadata" for pp in config["postprocessors"])
    assert json.loads(config_path.read_text()) == config


def test_effective_gallerydl_config_uses_defaults_when_config_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("GALLERYDL_CONFIG_ROOT", str(tmp_path))

    effective = build_effective_gallerydl_config(
        "lofter",
        {"extractor": {"lofter": {}}},
    )

    assert effective["extractor"]["lofter"]["directory"] == ["lofter", "{blog_name}", "{id}"]
    assert effective["extractor"]["lofter"]["filename"] == "{id}_{num}.{extension}"


def test_update_gallerydl_config_preserves_defaults_and_custom_values(tmp_path, monkeypatch):
    monkeypatch.setenv("GALLERYDL_CONFIG_ROOT", str(tmp_path))
    ensure_gallerydl_config(tmp_path / "config.json")

    result = asyncio.run(update_gallerydl_config(GalleryDLMultiConfig(
        pixiv=PixivSourceConfig(directory="custom/{id}", filename="{id}.{extension}"),
    )))

    saved = json.loads((tmp_path / "config.json").read_text())
    assert result["status"] == "ok"
    assert saved["extractor"]["pixiv"]["directory"] == ["custom", "{id}"]
    assert saved["extractor"]["pixiv"]["filename"] == "{id}.{extension}"
    assert saved["extractor"]["lofter"]["directory"] == ["lofter", "{blog_name}", "{id}"]
    assert any(pp.get("name") == "metadata" for pp in saved["postprocessors"])


def test_gallerydl_directory_is_authoritative():
    effective = build_effective_gallerydl_config(
        "pixiv",
        {"extractor": {"pixiv": {"cookies": "/default.txt"}}},
        user_config={"extractor": {"pixiv": {"directory": ["old"]}}},
    )
    assert effective["extractor"]["pixiv"]["directory"] == ["old"]
    assert effective["extractor"]["pixiv"]["cookies"] == "/default.txt"


def test_pixiv_gif_rebuilds_managed_postprocessors():
    config = {"postprocessors": [{"name": "custom"}, {"name": "metadata"}]}

    postprocessors = _rebuild_managed_postprocessors(config, "gif")

    assert postprocessors == [
        {"name": "custom"},
        {"name": "ugoira", "extension": "gif", "keep-files": False},
        {"name": "metadata", "event": "after", "filename": "{filename}.json"},
    ]


def test_pixiv_ugoira_survives_non_pixiv_save_when_final_config_is_gif():
    config = {
        "extractor": {"pixiv": {"ugoira": "gif"}},
        "postprocessors": [{"name": "metadata"}],
    }

    _rebuild_managed_postprocessors(config, config["extractor"]["pixiv"]["ugoira"])

    assert {"name": "ugoira", "extension": "gif", "keep-files": False} in config["postprocessors"]
    assert {"name": "metadata", "event": "after", "filename": "{filename}.json"} in config["postprocessors"]


def test_pixiv_zip_removes_managed_ugoira_postprocessor():
    config = {
        "postprocessors": [
            {"name": "custom"},
            {"name": "ugoira", "extension": "gif", "keep-files": False},
            {"name": "metadata", "event": "after", "filename": "{filename}.json"},
        ]
    }

    postprocessors = _rebuild_managed_postprocessors(config, "zip")

    assert postprocessors == [
        {"name": "custom"},
        {"name": "metadata", "event": "after", "filename": "{filename}.json"},
    ]


def test_effective_gallerydl_config_includes_user_ugoira_postprocessor():
    effective = build_effective_gallerydl_config(
        "pixiv",
        {"extractor": {"pixiv": {"cookies": "/default.txt"}}},
        user_config={
            "extractor": {"pixiv": {"ugoira": "gif"}},
            "postprocessors": [
                {"name": "ugoira", "extension": "gif", "keep-files": False},
                {"name": "metadata", "event": "after", "filename": "{filename}.json"},
            ],
        },
    )

    assert {"name": "ugoira", "extension": "gif", "keep-files": False} in effective["postprocessors"]
    assert {"name": "metadata", "event": "after", "filename": "{filename}.json"} in effective["postprocessors"]

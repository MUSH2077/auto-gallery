from app.services.settings import (
    build_effective_gallerydl_config,
    extractor_key_for_source,
    merge_gallerydl_effective_config,
    source_key_for_extractor,
)
from app.api.admin import _rebuild_managed_postprocessors


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

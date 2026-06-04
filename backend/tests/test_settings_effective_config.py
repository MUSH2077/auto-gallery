from app.services.settings import (
    build_effective_gallerydl_config,
    extractor_key_for_source,
    merge_gallerydl_effective_config,
    source_key_for_extractor,
)


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


def test_naming_template_overrides_gallerydl_directory():
    effective = build_effective_gallerydl_config(
        "pixiv",
        {"extractor": {"pixiv": {"cookies": "/default.txt"}}},
        "pixiv/{user[id]}/{id}",
        user_config={"extractor": {"pixiv": {"directory": ["old"]}}},
    )
    assert effective["extractor"]["pixiv"]["directory"] == ["pixiv", "{user[id]}", "{id}"]
    assert effective["extractor"]["pixiv"]["cookies"] == "/default.txt"

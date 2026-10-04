import asyncio
import json
import pytest

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


def test_effective_config_preserves_inline_cookie_values():
    cookies = {"cookies": "fixture-value", "auth_token": "fixture-token"}
    effective = build_effective_gallerydl_config("x", {}, user_config={"extractor": {"twitter": {"cookies": cookies}}})
    assert effective["extractor"]["twitter"]["cookies"] == cookies


@pytest.mark.parametrize("source,cookie_name", [("pixiv", "pixiv"), ("x", "twitter"), ("iwara", "iwara"), ("weibo", "weibo")])
def test_effective_preview_reports_default_cookie_symlink_escape(tmp_path, monkeypatch, source, cookie_name):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api.admin.gallerydl import get_effective_gallerydl_config
    from app.database import get_db

    root = tmp_path / "config"
    cookies = root / "cookies"
    cookies.mkdir(parents=True)
    outside = tmp_path / "fixture.txt"
    outside.write_text("non-sensitive fixture")
    (cookies / f"{cookie_name}.txt").symlink_to(outside)
    monkeypatch.setenv("GALLERYDL_CONFIG_ROOT", str(root))
    app = FastAPI()
    app.get("/gallerydl-config/effective")(get_effective_gallerydl_config)
    app.dependency_overrides[get_db] = lambda: None
    response = TestClient(app, raise_server_exceptions=False).get(
        "/gallerydl-config/effective", params={"source": source},
    )
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "invalid_cookie_path"
    assert "allowed configuration directory" in response.json()["detail"]["message"]


@pytest.mark.parametrize("path_kind", ["relative", "absolute", "container", "legacy"])
def test_cookie_paths_read_and_execute_from_config_root(tmp_path, monkeypatch, path_kind):
    from app.api.admin.gallerydl import get_gallerydl_config
    monkeypatch.setenv("GALLERYDL_CONFIG_ROOT", str(tmp_path))
    cookie = tmp_path / "cookies" / "pixiv.txt"
    cookie.parent.mkdir()
    cookie.write_text("fixture cookie")
    path = {"relative": "cookies/pixiv.txt", "absolute": str(cookie),
            "container": "/gallerydl-config/cookies/pixiv.txt", "legacy": "/cookies/pixiv.txt"}[path_kind]
    (tmp_path / "config.json").write_text(json.dumps({"extractor": {"pixiv": {"cookies": path}}}))
    result = asyncio.run(get_gallerydl_config("pixiv"))
    assert result["pixiv"]["cookie_content"] == "fixture cookie"
    effective = build_effective_gallerydl_config("pixiv", {})
    assert effective["extractor"]["pixiv"]["cookies"] == str(cookie)


@pytest.mark.parametrize("path_kind", ["traversal", "absolute", "prefix", "symlink"])
def test_cookie_paths_reject_escape_before_read_or_download(tmp_path, monkeypatch, path_kind):
    from fastapi import HTTPException
    from app.api.admin.gallerydl import get_gallerydl_config
    root = tmp_path / "config"
    root.mkdir()
    outside = tmp_path / "config-other" / "fixture.txt"
    outside.parent.mkdir()
    outside.write_text("non-sensitive fixture")
    (root / "escape.txt").symlink_to(outside)
    path = {"traversal": "../config-other/fixture.txt", "absolute": str(outside),
            "prefix": str(outside), "symlink": "escape.txt"}[path_kind]
    monkeypatch.setenv("GALLERYDL_CONFIG_ROOT", str(root))
    (root / "config.json").write_text(json.dumps({"extractor": {"pixiv": {"cookies": path}}}))
    with pytest.raises(HTTPException) as error:
        asyncio.run(get_gallerydl_config("pixiv"))
    assert error.value.status_code == 400
    assert error.value.detail["code"] == "invalid_cookie_path"
    with pytest.raises(ValueError, match="allowed configuration directory"):
        build_effective_gallerydl_config("pixiv", {})


def test_cookie_save_rejects_escape_before_any_write_and_preserves_valid_content(tmp_path, monkeypatch):
    from fastapi import HTTPException
    from app.api.admin.gallerydl import get_gallerydl_config
    monkeypatch.setenv("GALLERYDL_CONFIG_ROOT", str(tmp_path))
    result = asyncio.run(update_gallerydl_config(GalleryDLMultiConfig(
        pixiv=PixivSourceConfig(cookies_path="cookies/valid.txt", cookie_content="# Netscape HTTP Cookie File\nfixture"),
    )))
    assert result["status"] == "ok"
    assert "fixture" in asyncio.run(get_gallerydl_config("pixiv"))["pixiv"]["cookie_content"]
    original = (tmp_path / "config.json").read_bytes()
    cookie = (tmp_path / "cookies/valid.txt").read_bytes()
    with pytest.raises(HTTPException) as error:
        asyncio.run(update_gallerydl_config(GalleryDLMultiConfig(
            pixiv=PixivSourceConfig(cookies_path="../outside.txt", cookie_content="fixture"),
        )))
    assert error.value.status_code == 400
    assert (tmp_path / "config.json").read_bytes() == original
    assert (tmp_path / "cookies/valid.txt").read_bytes() == cookie
    assert not (tmp_path.parent / "outside.txt").exists()


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


def test_gallerydl_directory_is_authoritative(tmp_path, monkeypatch):
    monkeypatch.setenv("GALLERYDL_CONFIG_ROOT", str(tmp_path))
    effective = build_effective_gallerydl_config(
        "pixiv",
        {"extractor": {"pixiv": {"cookies": "cookies/default.txt"}}},
        user_config={"extractor": {"pixiv": {"directory": ["old"]}}},
    )
    assert effective["extractor"]["pixiv"]["directory"] == ["old"]
    assert effective["extractor"]["pixiv"]["cookies"] == str(tmp_path / "cookies/default.txt")


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
        {"extractor": {"pixiv": {"cookies": "/gallerydl-config/cookies/default.txt"}}},
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

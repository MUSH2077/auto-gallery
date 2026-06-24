import pytest
from app.services import settings as settings_mod


@pytest.mark.asyncio
async def test_download_defaults_has_skip_threshold(monkeypatch):
    async def fake_get_system_setting(db, key):
        return {}
    monkeypatch.setattr(settings_mod, "get_system_setting", fake_get_system_setting)
    result = await settings_mod.get_download_defaults(None)
    assert result["import_skip_threshold"] == 0.5

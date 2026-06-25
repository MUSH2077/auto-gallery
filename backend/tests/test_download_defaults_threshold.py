import pytest
from app.services import settings as settings_mod


@pytest.mark.asyncio
async def test_download_defaults_has_skip_threshold(monkeypatch):
    async def fake_get_system_setting(db, key):
        return {}
    monkeypatch.setattr(settings_mod, "get_system_setting", fake_get_system_setting)
    result = await settings_mod.get_download_defaults(None)
    assert result["import_skip_threshold"] == 0.5


@pytest.mark.asyncio
async def test_stored_threshold_passes_through_but_consumer_clamps(monkeypatch):
    """A stored out-of-range value shadows the clamped entry via **defaults,
    so the consumer (run_import_job) MUST re-clamp. Lock that safety contract."""
    from app.jobs.import_outcome import clamp_threshold

    async def fake_get_system_setting(db, key):
        return {"import_skip_threshold": 5.0}
    monkeypatch.setattr(settings_mod, "get_system_setting", fake_get_system_setting)
    result = await settings_mod.get_download_defaults(None)
    # **defaults shadowing: the raw stored value passes through unclamped...
    assert result["import_skip_threshold"] == 5.0
    # ...so the consumer's clamp_threshold() is the actual safety net.
    assert clamp_threshold(result["import_skip_threshold"]) == 1.0

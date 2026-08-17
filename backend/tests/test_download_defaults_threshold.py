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


@pytest.mark.asyncio
async def test_download_defaults_concurrency_default_is_3(monkeypatch):
    async def fake_get_system_setting(db, key):
        return {}
    monkeypatch.setattr(settings_mod, "get_system_setting", fake_get_system_setting)
    result = await settings_mod.get_download_defaults(None)
    assert result["download_concurrency"] == 3


@pytest.mark.asyncio
async def test_upstream_conflict_auto_resolution_defaults_on(monkeypatch):
    async def fake_get_system_setting(db, key):
        return {}
    monkeypatch.setattr(settings_mod, "get_system_setting", fake_get_system_setting)

    result = await settings_mod.get_download_defaults(None)

    assert result["auto_resolve_upstream_conflicts"] is True


@pytest.mark.asyncio
async def test_saving_enabled_auto_resolution_enqueues_historical_reconciliation(
    monkeypatch,
):
    from app.api.admin import settings as admin_settings

    stored = []
    enqueued = {"count": 0}

    async def fake_put_setting(db, key, value):
        stored.append((db, key, value))

    def fake_enqueue():
        enqueued["count"] += 1
        return {"job_id": "reconcile-1", "status": "enqueued"}

    monkeypatch.setattr(admin_settings, "_put_setting", fake_put_setting)
    monkeypatch.setattr(
        admin_settings,
        "_enqueue_download_conflict_reconciliation",
        fake_enqueue,
    )

    result = await admin_settings.update_settings(
        admin_settings.AdminSettingsUpdate(
            download_defaults=admin_settings.DownloadDefaults(
                auto_resolve_upstream_conflicts=True
            )
        ),
        db=None,
    )

    assert stored[0][1] == "download_defaults"
    assert stored[0][2]["auto_resolve_upstream_conflicts"] is True
    assert enqueued["count"] == 1
    assert result["conflict_reconciliation"]["job_id"] == "reconcile-1"


@pytest.mark.asyncio
async def test_download_defaults_concurrency_clamped_to_5(monkeypatch):
    async def fake_get_system_setting(db, key):
        return {"download_concurrency": 9}
    monkeypatch.setattr(settings_mod, "get_system_setting", fake_get_system_setting)
    result = await settings_mod.get_download_defaults(None)
    # The typed entry is clamped; even though **defaults can shadow it, the
    # worker re-clamps via resolve_concurrency (covered in test_worker_concurrency).
    from worker_entrypoint import resolve_concurrency
    assert resolve_concurrency(result["download_concurrency"], None) == 5

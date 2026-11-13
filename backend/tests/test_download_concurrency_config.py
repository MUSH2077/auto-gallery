def test_empty_device_download_limit_is_none(monkeypatch):
    from app.config import Settings

    monkeypatch.setenv("DOWNLOAD_DEVICE_CONCURRENCY_LIMIT", "")
    settings = Settings()
    assert settings.download_device_concurrency_limit is None


def test_device_download_limit_is_bounded(monkeypatch):
    from app.config import Settings

    monkeypatch.setenv("DOWNLOAD_DEVICE_CONCURRENCY_LIMIT", "4")
    settings = Settings()
    assert settings.download_device_concurrency_limit == 4
    assert settings.download_execution_concurrency == 1
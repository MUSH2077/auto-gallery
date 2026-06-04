import inspect
from pathlib import Path


def test_admin_has_effective_gallerydl_preview_endpoint():
    from app.api import admin

    src = inspect.getsource(admin.get_effective_gallerydl_config)
    assert "build_effective_gallerydl_config" in src
    assert "redacted_manifest_config" in src
    assert "source_key_for_extractor" in src


def test_health_exposes_business_metrics():
    import app.main as main

    src = inspect.getsource(main.health)
    assert '"business"' in src
    assert "DownloadJob" in src
    assert "gallery-dl" in src or "gallerydl" in src


def test_migration_adds_manifest_auth_fields_and_indexes():
    text = (Path(__file__).resolve().parents[1] / "alembic" / "versions" / "e4f8a12b77c3_add_job_manifest_auth_status_and_indexes.py").read_text()
    assert "download_jobs" in text and "manifest" in text
    assert "auth_status" in text
    assert "ix_download_jobs_subscription_source_created" in text
    assert "ix_work_tags_tag_id" in text

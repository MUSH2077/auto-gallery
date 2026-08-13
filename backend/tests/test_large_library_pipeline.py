import inspect
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4


def test_staging_promotion_is_scoped_to_one_job(tmp_path):
    from app.jobs.download import _promote_staged_files

    downloads = tmp_path / "downloads"
    stage_a = downloads / ".staging" / "job-a"
    stage_b = downloads / ".staging" / "job-b"
    file_a = stage_a / "pixiv" / "creator" / "100" / "metadata.json"
    file_b = stage_b / "pixiv" / "creator" / "200" / "metadata.json"
    file_a.parent.mkdir(parents=True)
    file_b.parent.mkdir(parents=True)
    file_a.write_text('{"id": 100}')
    file_b.write_text('{"id": 200}')

    promoted = _promote_staged_files(stage_a, downloads)

    assert promoted == [downloads / "pixiv" / "creator" / "100" / "metadata.json"]
    assert json.loads(promoted[0].read_text()) == {"id": 100}
    assert file_b.exists()


def test_library_path_resolver_handles_nested_values_and_unsafe_segments():
    from app.services.library_sync import LibraryPathResolver

    resolver = LibraryPathResolver({
        "extractor": {"pixiv": {"directory": ["pixiv", "{user[name]}", "{id}"]}}
    })
    assert resolver.creator_directory("pixiv", {"user": {"name": "a/b"}}, "10") == "a_b"


def test_metadata_writer_is_versioned_and_leaves_no_temp_file(tmp_path):
    from app.services.library_sync import metadata_version, write_metadata_json

    work = SimpleNamespace(id=uuid4(), title="title", posted_at=None)
    source = SimpleNamespace(source="pixiv", source_work_id="10")
    write_metadata_json(tmp_path, work, source, "creator", [{"file_name": "a.jpg"}], version="v1")

    assert metadata_version(tmp_path) == "v1"
    assert not list(tmp_path.glob(".metadata.*.tmp"))


def test_download_worker_passes_every_queue_to_each_process():
    import worker_entrypoint

    queues = ["downloads", "downloads:pixiv", "downloads:x"]
    command = worker_entrypoint._worker_command(queues, with_scheduler=False)
    assert command[-len(queues):] == queues
    source = inspect.getsource(worker_entrypoint._worker_command)
    assert "queues[i % len(queues)]" not in source


def test_storage_artifact_migration_has_required_indexes():
    migration = Path(__file__).resolve().parents[1] / "alembic" / "versions" / "a6c8e0f2b4d6_add_storage_artifacts.py"
    text = migration.read_text()
    assert "uq_storage_artifacts_root_path" in text
    assert "ix_storage_artifacts_download_state" in text
    assert "uq_download_jobs_active_source" in text


def test_download_no_longer_publishes_stream_messages():
    from app.jobs import download

    source = inspect.getsource(download.run_download_job)
    assert 'xadd("work:ready"' not in source

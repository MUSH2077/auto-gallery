"""Metadata cleanup must preserve untracked and operational JSON."""
import pytest

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def test_untracked_json_is_never_cleanup_input(tmp_path):
    from app.jobs.import_runner import cleanup_metadata_jsons
    protected = tmp_path / '.import-lists' / 'active.json'
    protected.parent.mkdir()
    protected.write_text('{"active": true}')
    try:
        await cleanup_metadata_jsons(str(tmp_path))
    except RuntimeError:
        pass  # An unfenced legacy call must refuse destructive work.
    assert protected.read_text() == '{"active": true}'
    from app.jobs.admin_operations import run_cleanup_metadata_jsons_operation
    with pytest.raises(RuntimeError, match='Legacy metadata cleanup delivery'):
        run_cleanup_metadata_jsons_operation('obsolete-job-id')
    assert protected.read_text() == '{"active": true}'

from tests.test_import_finalization_recovery import db, _import_fixture  # noqa: E402,F401


async def completed_sidecar(db, tmp_path, monkeypatch, *, number=1, compact=True):
    from sqlalchemy import select
    from app.jobs.import_runner import run_import_job
    from app.models import StorageArtifact, ImportJob, DownloadJob
    from app.services.import_projection import process_import_projection_outbox
    from app.services.operation_attention import compact_terminal_tasks
    parent_id, child_id, metadata = await _import_fixture(db, tmp_path, monkeypatch, number=number)
    for row in (await db.execute(select(StorageArtifact))).scalars():
        path = tmp_path / 'downloads' / row.file_path
        row.file_size, row.mtime_ns = path.stat().st_size, path.stat().st_mtime_ns
    await db.commit()
    await run_import_job(str(child_id))
    await db.rollback()
    assert (await db.get(ImportJob, child_id, populate_existing=True)).status == 'complete', (await db.get(ImportJob, child_id, populate_existing=True)).error_log
    projection = await process_import_projection_outbox(limit=number, max_seconds=120)
    assert projection['metadata_processed'] == number, projection
    if not compact:
        return metadata
    await db.rollback()
    compacted = await compact_terminal_tasks(db, dry_run=False)
    assert compacted['deleted_download_jobs'] == 1, compacted
    assert await db.get(DownloadJob, parent_id, populate_existing=True) is None
    return metadata


async def invoke_cleanup(db, root):
    from app.models import TaskRun
    from app.services.operations import ADMIN_DISPATCH_META_KEY, admin_operation_attempt_context
    from app.jobs.import_runner import cleanup_metadata_jsons
    task = TaskRun(kind='admin', operation_type='admin-cleanup-metadata-jsons', status='running',
                   meta={ADMIN_DISPATCH_META_KEY: {'attempt': 1}})
    db.add(task)
    await db.commit()
    task_id = task.id
    with admin_operation_attempt_context(task_id, 1):
        return await cleanup_metadata_jsons(str(root))


async def invoke_registered_cleanup(db):
    from uuid import UUID
    from app.api.admin.data import cleanup_metadata_jsons
    from app.jobs.admin_operations import _run_registered_admin_operation
    from app.models import TaskRun
    receipt = await cleanup_metadata_jsons()
    assert 'removed' not in receipt, 'admission must not claim a cleanup count'
    task_id = receipt['task_id']
    result = await _run_registered_admin_operation(str(task_id), 1)
    await db.rollback()
    terminal = await db.get(TaskRun, UUID(str(task_id)), populate_existing=True)
    assert terminal.status == 'complete' and terminal.result_data == result
    return result


async def test_natural_import_then_compaction_cleans_only_proved_sidecar(db, tmp_path, monkeypatch):
    from sqlalchemy import select
    from app.models import StorageArtifact, Work, Asset
    metadata = await completed_sidecar(db, tmp_path, monkeypatch)
    protected = tmp_path / 'downloads' / 'manual.json'
    protected.write_text('{"keep": true}')
    before = ({r.id for r in (await db.execute(select(Work))).scalars()},
              {r.id for r in (await db.execute(select(Asset))).scalars()})
    row = (await db.execute(select(StorageArtifact).where(StorageArtifact.artifact_type == 'metadata_json', StorageArtifact.storage_root == 'downloads'))).scalar_one()
    assert row.download_job_id is row.import_job_id is None
    result = await invoke_registered_cleanup(db)
    assert protected.exists(), 'untracked metadata must survive real registered cleanup'
    assert not metadata.exists()
    assert result['removed'] == 1 and result['failed'] == 0
    second = await invoke_registered_cleanup(db)
    assert second['removed'] == 0 and second['skipped_by_reason']['already_absent'] == 1
    assert before == ({r.id for r in (await db.execute(select(Work))).scalars()},
                      {r.id for r in (await db.execute(select(Asset))).scalars()})


async def test_compacted_proof_rejects_replacements_and_domain_changes(db, tmp_path, monkeypatch):
    import os
    from sqlalchemy import select
    from app.models import WorkSource, StorageArtifact
    from app.services.artifact_ledger import ArtifactLedger, artifact_row
    metadata = await completed_sidecar(db, tmp_path, monkeypatch)
    original = metadata.read_bytes()
    saved_stat = metadata.stat()
    changed = original.replace(b'Checkpoint work', b'Unimported work')
    assert len(changed) == len(original)
    metadata.write_bytes(changed)
    os.utime(metadata, ns=(saved_stat.st_atime_ns, saved_stat.st_mtime_ns))
    result = await invoke_cleanup(db, tmp_path / 'downloads')
    assert result['removed'] == 0
    assert result['skipped_by_reason']['metadata_content_changed'] == 1
    metadata.write_bytes(original)
    os.utime(metadata, ns=(saved_stat.st_atime_ns, saved_stat.st_mtime_ns))
    source = (await db.execute(select(WorkSource))).scalar_one()
    source.raw_metadata = {**source.raw_metadata, 'title': 'changed after completion'}
    await db.commit()
    result = await invoke_cleanup(db, tmp_path / 'downloads')
    assert result['skipped_by_reason']['domain_identity_changed'] == 1
    assert metadata.exists()
    candidate = artifact_row(metadata, tmp_path / 'downloads')
    candidate['file_size'] += 1  # Same mtime, different inventory version.
    await ArtifactLedger(db).upsert_many([candidate])
    await db.commit()
    artifact = (await db.execute(select(StorageArtifact).where(StorageArtifact.file_path == candidate['file_path']).execution_options(populate_existing=True))).scalar_one()
    assert artifact.metadata_completion_proof is None


async def test_partial_unlink_error_survives_registered_terminal_result(db, tmp_path, monkeypatch):
    from app.jobs.admin_operations import _run_registered_admin_operation
    from app.services.operations import prepare_admin_operation
    from app.models import TaskRun
    from app.services import metadata_cleanup
    metadata = await completed_sidecar(db, tmp_path, monkeypatch, number=2)
    prepared = await prepare_admin_operation(db, operation_type='admin-cleanup-metadata-jsons',
        scope_key='library:cleanup-metadata-jsons:active', title='Cleanup', entity='metadata-jsons',
        options={}, queue_name='maintenance', job_timeout=7200)
    task_id = prepared.task.id
    await db.commit()
    real_unlink = metadata_cleanup.os.unlink
    def denied(path, *args, **kwargs):
        if path == metadata.name:
            raise PermissionError('deliberate owned fixture unlink denial')
        return real_unlink(path, *args, **kwargs)
    monkeypatch.setattr(metadata_cleanup.os, 'unlink', denied)
    result = await _run_registered_admin_operation(str(task_id), 1)
    await db.rollback()
    task = await db.get(TaskRun, task_id, populate_existing=True)
    assert task.status == 'failed'
    assert task.reason_code == 'metadata_cleanup_partial_failure'
    assert task.result_data['failed'] == result['failed'] == 1
    assert result['status'] == 'partial' and result['removed'] == 1
    assert result['errors'][0]['reason'] == 'PermissionError'
    assert 'partial' in result['message'].lower()
    assert metadata.exists()


async def test_natural_all_existing_import_keeps_proof_after_compaction(db, tmp_path, monkeypatch):
    import os
    from sqlalchemy import select
    from app.models import DownloadJob, ImportJob, SubscriptionSource, StorageArtifact, RepositorySyncReceipt
    from app.services.artifact_ledger import ArtifactLedger, artifact_row
    from app.services.tasks import TaskService
    from app.jobs.download import _successful_repository_import_plan
    from app.repositories.download_job import DownloadJobRepository
    from app.jobs.download_outcome import classify_no_metadata_outcome
    from app.services.download_finalization import finalize_download_job
    from app.services.sync_outcome import build_sync_outcome, had_sync_baseline
    from app.services.operation_attention import compact_terminal_tasks
    metadata = await completed_sidecar(db, tmp_path, monkeypatch)
    source = (await db.execute(select(SubscriptionSource))).scalar_one()
    parent = DownloadJob(subscription_id=source.subscription_id, subscription_source_id=source.id,
                         source='pixiv', source_url=source.source_url, status='downloading',
                         manifest={'had_sync_baseline': bool(source.last_synced_at)})
    db.add(parent)
    await db.flush()
    parent_id = parent.id
    await TaskService(db).ensure_download_task(parent)
    st = metadata.stat()
    os.utime(metadata, ns=(st.st_atime_ns, st.st_mtime_ns + 1000000))
    await ArtifactLedger(db).upsert_many([artifact_row(metadata, tmp_path / 'downloads', parent_id)])
    await DownloadJobRepository(db).update_status(parent, 'downloaded')
    await db.commit()
    # The real successful-download planner recognizes existing content and
    # does not enqueue an ImportJob with an empty assigned file feed.
    pending, paths, reconciliation = await _successful_repository_import_plan(
        db, parent, metadata_count=1, metadata_paths=[str(metadata.relative_to(tmp_path / 'downloads'))])
    assert pending == 0 and not paths
    decision = classify_no_metadata_outcome(image_count=0, auth_warning=None,
        is_subscription=True, had_sync_baseline=had_sync_baseline(parent))
    await finalize_download_job(db, parent, status=decision.status,
        outcome=build_sync_outcome(decision.outcome_code, metadata_count=1, media_count=0,
                                  recovery_detail=reconciliation.outcome_detail))
    assert not (await db.execute(select(ImportJob))).scalars().all()
    receipt = (await db.execute(select(RepositorySyncReceipt).where(
        RepositorySyncReceipt.source_download_job_id == parent_id))).scalar_one()
    assert receipt.status == 'complete' and receipt.works_imported == 0
    artifact = (await db.execute(select(StorageArtifact).where(StorageArtifact.storage_root == 'downloads', StorageArtifact.artifact_type == 'metadata_json'))).scalar_one()
    assert artifact.import_job_id is None and artifact.state == 'done'
    await compact_terminal_tasks(db, dry_run=False)
    await db.refresh(artifact)
    assert artifact.metadata_completion_proof['kind'] == 'terminal_success_existing_content'
    result = await invoke_registered_cleanup(db)
    assert result['removed'] == 1 and not metadata.exists(), result


async def test_row_contention_and_cancel_fence_preserve_sidecar(db, tmp_path, monkeypatch):
    import asyncio
    from sqlalchemy import select, update
    from app.database import async_session
    from app.models import StorageArtifact, TaskRun
    from app.services import metadata_cleanup
    from app.services.operations import AdminOperationAttemptRejected
    metadata = await completed_sidecar(db, tmp_path, monkeypatch)
    artifact_id = (await db.execute(select(StorageArtifact.id).where(StorageArtifact.storage_root == 'downloads', StorageArtifact.artifact_type == 'metadata_json'))).scalar_one()
    await db.rollback()
    async with async_session() as locked:
        await locked.execute(select(StorageArtifact.id).where(StorageArtifact.id == artifact_id).with_for_update())
        result = await asyncio.wait_for(invoke_cleanup(db, tmp_path / 'downloads'), 15)
        assert result['skipped_by_reason']['row_busy'] == 1
        assert metadata.exists()
        await locked.rollback()
    entered, resume = asyncio.Event(), asyncio.Event()
    original_visit = metadata_cleanup.visit_candidate
    async def barrier(*args, **kwargs):
        entered.set()
        await resume.wait()
        return await original_visit(*args, **kwargs)
    monkeypatch.setattr(metadata_cleanup, 'visit_candidate', barrier)
    running = asyncio.create_task(invoke_cleanup(db, tmp_path / 'downloads'))
    await asyncio.wait_for(entered.wait(), 15)
    await db.execute(update(TaskRun).where(TaskRun.kind == 'admin', TaskRun.status == 'running').values(status='cancelled'))
    await db.commit()
    resume.set()
    with pytest.raises(AdminOperationAttemptRejected):
        await running
    assert metadata.exists()


async def test_crash_after_unlink_cannot_delete_a_replacement_on_recovery(db, tmp_path, monkeypatch):
    import os
    from app.models import TaskRun
    from app.services.operations import ADMIN_DISPATCH_META_KEY, admin_operation_attempt_context
    from app.services import metadata_cleanup
    metadata = await completed_sidecar(db, tmp_path, monkeypatch)
    contents, original_stat = metadata.read_bytes(), metadata.stat()
    task = TaskRun(kind='admin', operation_type='admin-cleanup-metadata-jsons', status='running',
                   meta={ADMIN_DISPATCH_META_KEY: {'attempt': 1}})
    db.add(task)
    await db.commit()
    task_id = task.id
    original = metadata_cleanup.finalize_file
    def crash_after_unlink(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError('crash after real unlink before result checkpoint')
    monkeypatch.setattr(metadata_cleanup, 'finalize_file', crash_after_unlink)
    with admin_operation_attempt_context(task_id, 1), pytest.raises(RuntimeError, match='crash after'):
        await metadata_cleanup.cleanup_metadata_jsons(str(tmp_path / 'downloads'))
    assert not metadata.exists()
    metadata.write_bytes(contents)
    os.utime(metadata, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
    monkeypatch.setattr(metadata_cleanup, 'finalize_file', original)
    with admin_operation_attempt_context(task_id, 1):
        result = await metadata_cleanup.cleanup_metadata_jsons(str(tmp_path / 'downloads'))
    assert metadata.exists() and result['removed'] == 0
    assert result['skipped_by_reason']['file_replaced_since_intent'] == 1

async def test_checkpoint_index_rejects_malformed_and_requires_media():
    from app.services.metadata_cleanup_proof import index_checkpoint
    assert index_checkpoint({'version': 2, 'signature': 'bad'}) is None
    assert index_checkpoint({'version': 2, 'signature': {}}) is None

async def test_retained_owners_positive_and_projection_media_veto(db, tmp_path, monkeypatch):
    from sqlalchemy import select
    from app.models import StorageArtifact, DownloadJob, ImportJob
    from app.models.pipeline_outbox import ImportCurationOutbox
    metadata = await completed_sidecar(db, tmp_path, monkeypatch, compact=False)
    parent = (await db.execute(select(DownloadJob))).scalar_one()
    child = (await db.execute(select(ImportJob))).scalar_one()
    original_parent_id = parent.id
    unrelated_parent = DownloadJob(subscription_id=parent.subscription_id, source='pixiv',
                                   source_url='https://www.pixiv.net/users/999', status='complete')
    db.add(unrelated_parent)
    await db.flush()
    child.download_job_id = unrelated_parent.id
    await db.commit()
    result = await invoke_cleanup(db, tmp_path / 'downloads')
    assert result['skipped_by_reason'].get('owner_not_complete') == 1 and metadata.exists()
    child.download_job_id = original_parent_id
    await db.commit()
    for owner, state in ((parent, 'failed'), (parent, 'stale'), (parent, 'cancelled'),
                         (parent, 'paused'), (child, 'failed'), (child, 'enqueued')):
        owner.status = state
        await db.commit()
        result = await invoke_cleanup(db, tmp_path / 'downloads')
        assert result['skipped_by_reason']['owner_not_complete'] == 1 and metadata.exists()
        owner.status = 'complete'
        await db.commit()
    projection = (await db.execute(select(ImportCurationOutbox))).scalar_one()
    projection.metadata_state = 'failed'
    await db.commit()
    result = await invoke_cleanup(db, tmp_path / 'downloads')
    assert result['skipped_by_reason']['projection_not_complete'] == 1
    projection.metadata_state = 'complete'
    await db.commit()
    media = (await db.execute(select(StorageArtifact).where(StorageArtifact.storage_root == 'downloads', StorageArtifact.artifact_type == 'image'))).scalar_one()
    original = media.file_name
    media.file_name = 'unassigned.jpg'
    await db.commit()
    result = await invoke_cleanup(db, tmp_path / 'downloads')
    assert result['removed'] == 0 and metadata.exists()
    media.file_name = original
    await db.commit()
    result = await invoke_cleanup(db, tmp_path / 'downloads')
    assert result['removed'] == 1


async def test_preservation_matrix_and_nonblocking_promotion(db, tmp_path, monkeypatch):
    import json
    import os
    import subprocess
    import sys
    from sqlalchemy import select
    from app.models import StorageArtifact
    from app.services import metadata_cleanup
    from app.services.download_staging import PROMOTION_LOCK_NAME
    metadata = await completed_sidecar(db, tmp_path, monkeypatch)
    artifact = (await db.execute(select(StorageArtifact).where(StorageArtifact.storage_root == 'downloads', StorageArtifact.artifact_type == 'metadata_json'))).scalar_one()
    proof = artifact.metadata_completion_proof
    for state in ('new', 'importing', 'failed'):
        artifact.state = state
        await db.commit()
        result = await invoke_cleanup(db, tmp_path / 'downloads')
        assert result['removed'] == 0 and metadata.exists()
    artifact.state = 'done'
    artifact.metadata_completion_proof = None
    await db.commit()
    assert (await invoke_cleanup(db, tmp_path / 'downloads'))['skipped_by_reason']['completion_evidence_missing_or_changed'] == 1
    artifact.metadata_completion_proof = proof
    await db.commit()
    monkeypatch.setattr(metadata_cleanup, 'staging_enabled', lambda: False)
    assert (await invoke_cleanup(db, tmp_path / 'downloads'))['skipped_by_reason']['unserialized_canonical_writers'] == 1
    monkeypatch.setattr(metadata_cleanup, 'staging_enabled', lambda: True)
    program = "import fcntl,sys; f=open(sys.argv[1],'w'); fcntl.flock(f,fcntl.LOCK_EX); print('locked',flush=True); sys.stdin.read()"
    holder = subprocess.Popen([sys.executable, '-c', program, str(tmp_path / 'downloads' / PROMOTION_LOCK_NAME)], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    try:
        assert holder.stdout.readline().strip() == b'locked'
        assert (await invoke_cleanup(db, tmp_path / 'downloads'))['skipped_by_reason']['canonical_writer_busy'] == 1
    finally:
        holder.communicate(input=b'', timeout=10)
    sentinel = tmp_path / 'outside.json'
    sentinel.write_text(json.dumps({'protected': True}))
    original = metadata.read_bytes()
    metadata.unlink()
    metadata.symlink_to(sentinel)
    result = await invoke_cleanup(db, tmp_path / 'downloads')
    assert result['removed'] == 0 and sentinel.exists() and metadata.is_symlink()
    metadata.unlink()
    metadata.write_bytes(original)
    assert sentinel.read_text() == json.dumps({'protected': True})


async def test_managed_keyset_continues_past_one_page(db, tmp_path, monkeypatch):
    from app.models import StorageArtifact
    from app.services import offline_restore
    metadata = await completed_sidecar(db, tmp_path, monkeypatch)
    root = tmp_path / 'downloads'
    monkeypatch.setattr(offline_restore, 'staging_root', lambda: root / 'pixiv/restore-stage')
    monkeypatch.setattr(offline_restore, 'receipts_root', lambda: root / 'pixiv/restore-receipts')
    protected_paths = ['manual/user/metadata.json', '.staging/manifest.json', '.backups/config.json',
                       'config/settings.json', 'pixiv/restore-stage/state.json', 'pixiv/restore-receipts/receipt.json']
    protected_paths += [f'.import-lists/{index}.json' for index in range(24)]
    for index, relative in enumerate(protected_paths):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'{"keep":true}')
        db.add(StorageArtifact(storage_root='downloads', artifact_type='metadata_json',
            file_path=relative, file_name=path.name, source='manual' if relative.startswith('manual/') else 'pixiv',
            creator_dir='protected', source_work_id=str(index), state='new'))
    await db.commit()
    result = await invoke_cleanup(db, tmp_path / 'downloads')
    assert result['scanned'] == 31 == result['removed'] + result['skipped'] + result['failed']
    assert result['removed'] == 1 and result['skipped'] == 30 and not metadata.exists()
    assert all((root / relative).read_bytes() == b'{"keep":true}' for relative in protected_paths)

async def test_compaction_proof_is_atomic_and_failed_owner_never_certified(db, tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone
    from sqlalchemy import event, select, update
    from app.database import engine, async_session
    from app.models import StorageArtifact, DownloadJob, ImportJob, TaskRun
    from app.services.operation_attention import compact_terminal_tasks
    metadata = await completed_sidecar(db, tmp_path, monkeypatch, compact=False)
    parent = (await db.execute(select(DownloadJob))).scalar_one()
    child = (await db.execute(select(ImportJob))).scalar_one()
    parent_id, child_id = parent.id, child.id
    def abort_delete(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith('DELETE FROM task_runs'):
            raise RuntimeError('fixture abort after proof capture')
    event.listen(engine.sync_engine, 'before_cursor_execute', abort_delete)
    try:
        with pytest.raises(RuntimeError, match='after proof capture'):
            await compact_terminal_tasks(db, dry_run=False)
    finally:
        event.remove(engine.sync_engine, 'before_cursor_execute', abort_delete)
        await db.rollback()
    artifacts = list((await db.execute(select(StorageArtifact).where(StorageArtifact.storage_root == 'downloads'))).scalars())
    assert all(row.metadata_completion_proof is None for row in artifacts)
    assert await db.get(DownloadJob, parent_id) and await db.get(ImportJob, child_id)
    parent = await db.get(DownloadJob, parent_id)
    child = await db.get(ImportJob, child_id)
    # Leave complete owners in this Session's identity map while another
    # transaction changes their terminal outcome before compaction locks them.
    async with async_session() as competitor:
        await competitor.execute(update(DownloadJob).where(DownloadJob.id == parent_id).values(status='failed'))
        await competitor.execute(update(ImportJob).where(ImportJob.id == child_id).values(status='failed'))
        await competitor.execute(update(TaskRun).values(status='failed', attention_state='acknowledged',
            compactable_at=datetime.now(timezone.utc) - timedelta(seconds=1)))
        await competitor.commit()
    report = await compact_terminal_tasks(db, dry_run=False)
    assert report['deleted_download_jobs'] == 1
    for row in artifacts:
        await db.refresh(row)
        assert row.metadata_completion_proof is None
    assert (await invoke_cleanup(db, tmp_path / 'downloads'))['removed'] == 0
    assert metadata.exists()


async def test_compaction_does_not_certify_explicit_completion_evidence_failure(db, tmp_path, monkeypatch):
    from sqlalchemy import select
    from app.models import StorageArtifact, TaskRun
    from app.services.operation_attention import compact_terminal_tasks
    metadata = await completed_sidecar(db, tmp_path, monkeypatch, compact=False)
    task = (await db.execute(select(TaskRun).where(TaskRun.subject_type == 'import_job'))).scalar_one()
    task.reason_code = 'import_completion_identity_mismatch'
    await db.commit()
    report = await compact_terminal_tasks(db, dry_run=False)
    assert report['deleted_download_jobs'] == 1
    artifact = (await db.execute(select(StorageArtifact).where(
        StorageArtifact.storage_root == 'downloads', StorageArtifact.artifact_type == 'metadata_json')
        .execution_options(populate_existing=True))).scalar_one()
    assert artifact.metadata_completion_proof is None
    assert (await invoke_cleanup(db, tmp_path / 'downloads'))['removed'] == 0
    assert metadata.exists()


async def test_ledger_mutations_clear_actual_completion_proof(db, tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone
    from uuid import uuid4
    from sqlalchemy import select
    from app.models import StorageArtifact
    from app.services.artifact_ledger import ArtifactLedger, artifact_row
    metadata = await completed_sidecar(db, tmp_path, monkeypatch)
    row = (await db.execute(select(StorageArtifact).where(StorageArtifact.storage_root == 'downloads', StorageArtifact.artifact_type == 'metadata_json'))).scalar_one()
    original_proof = row.metadata_completion_proof
    ledger = ArtifactLedger(db)
    # Use the genuine captured certificate as the sentinel; no fabricated
    # certificate is used to authorize a positive cleanup.
    for mutate in (
        lambda: ledger.mark_work(None, row.source_work_id, 'done'),
        lambda: ledger.mark_works(None, [row.source_work_id], 'failed'),
        lambda: ledger.mark_work_results(None, {row.source_work_id: ('done', None)}),
    ):
        row.metadata_completion_proof = original_proof
        await db.commit()
        await mutate()
        await db.commit()
        await db.refresh(row)
        assert row.metadata_completion_proof is None
    row.state = 'done'
    for field, value in (('file_size', row.file_size + 1), ('source_work_id', 'changed'), ('file_name', 'changed.json')):
        row.metadata_completion_proof = original_proof
        await db.commit()
        candidate = artifact_row(metadata, tmp_path / 'downloads')
        candidate[field] = value
        await ledger.upsert_many([candidate])
        await db.commit()
        await db.refresh(row)
        assert row.metadata_completion_proof is None

async def test_scoped_migration_leaves_legacy_rows_without_proof(db):
    import importlib.util
    from pathlib import Path
    from sqlalchemy import text
    from alembic.operations import Operations
    from alembic.migration import MigrationContext
    from app.models import StorageArtifact
    row = StorageArtifact(storage_root='downloads', artifact_type='metadata_json',
        file_path='pixiv/legacy.json', file_name='legacy.json', source='pixiv',
        creator_dir='legacy', source_work_id='legacy', state='done')
    db.add(row)
    await db.commit()
    identity = row.id
    spec = importlib.util.spec_from_file_location('proof_migration', Path(__file__).parents[1] / 'alembic/versions/fc24d6e8fa02_metadata_completion_proof.py')
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    connection = await db.connection()
    def run_scoped(sync_connection):
        context = MigrationContext.configure(sync_connection)
        with Operations.context(context):
            migration.downgrade()
            migration.upgrade()
    await connection.run_sync(run_scoped)
    assert (await db.execute(text('SELECT metadata_completion_proof IS NULL FROM storage_artifacts WHERE id=:id'), {'id': identity})).scalar_one()
    await db.rollback()  # PostgreSQL DDL is transactional; restore original schema.

async def test_multiwork_capture_is_batched_without_filesystem_reads(db, tmp_path, monkeypatch):
    import builtins
    import json
    import time
    from sqlalchemy import event, select
    from app.database import engine
    from app.models import StorageArtifact
    from app.services import metadata_cleanup_proof
    original = metadata_cleanup_proof.capture_compaction_proofs
    observed = {}
    async def measured(session, download_ids):
        statements = []
        def record(conn, cursor, statement, parameters, context, executemany):
            statements.append(statement)
        def forbidden(*args, **kwargs):
            raise AssertionError('compaction proof must not read files')
        event.listen(engine.sync_engine, 'before_cursor_execute', record)
        started = time.monotonic()
        try:
            with monkeypatch.context() as scope:
                scope.setattr(builtins, 'open', forbidden)
                await original(session, download_ids)
        finally:
            observed.update(seconds=time.monotonic() - started, statements=len(statements),
                            selects=sum(statement.startswith('SELECT') for statement in statements))
            event.remove(engine.sync_engine, 'before_cursor_execute', record)
    monkeypatch.setattr(metadata_cleanup_proof, 'capture_compaction_proofs', measured)
    await completed_sidecar(db, tmp_path, monkeypatch, number=26)
    rows = list((await db.execute(select(StorageArtifact).where(StorageArtifact.storage_root == 'downloads', StorageArtifact.artifact_type == 'metadata_json'))).scalars())
    assert len(rows) == 26 and all(row.metadata_completion_proof for row in rows)
    assert observed['selects'] <= 12, observed
    observed['maximum_proof_bytes'] = max(len(json.dumps(row.metadata_completion_proof)) for row in rows)
    assert observed['maximum_proof_bytes'] <= metadata_cleanup_proof.MAX_PROOF_BYTES
    print('COMPACTION_CAPTURE_METRICS', json.dumps(observed))
    from app.services import metadata_cleanup
    def deny_unlink(*args, **kwargs):
        raise PermissionError('fixture bounded failure sample')
    with monkeypatch.context() as scope:
        scope.setattr(metadata_cleanup.os, 'unlink', deny_unlink)
        partial = await invoke_cleanup(db, tmp_path / 'downloads')
    assert partial['failed'] == partial['scanned'] == 26
    assert len(partial['errors']) == 25 and partial['errors_truncated'] == 1
    result = await invoke_cleanup(db, tmp_path / 'downloads')
    assert result['removed'] == result['scanned'] == 26

async def test_cancellation_waits_for_unlink_thread_before_releasing_fence(db, tmp_path, monkeypatch):
    import asyncio
    import threading
    from sqlalchemy import select
    from sqlalchemy.exc import DBAPIError
    from app.database import async_session
    from app.models import TaskRun
    from app.services import metadata_cleanup
    metadata = await completed_sidecar(db, tmp_path, monkeypatch)
    entered, resume = threading.Event(), threading.Event()
    original = metadata_cleanup.os.unlink
    def blocking_unlink(*args, **kwargs):
        entered.set()
        assert resume.wait(timeout=30)
        return original(*args, **kwargs)
    monkeypatch.setattr(metadata_cleanup.os, 'unlink', blocking_unlink)
    running = asyncio.create_task(invoke_cleanup(db, tmp_path / 'downloads'))
    assert await asyncio.to_thread(entered.wait, 30)
    running.cancel()
    await asyncio.sleep(0)
    assert not running.done(), 'outer cancellation must wait for fenced thread'
    try:
        async with async_session() as competitor:
            with pytest.raises(DBAPIError) as error:
                await competitor.execute(select(TaskRun).where(TaskRun.kind == 'admin', TaskRun.status == 'running').with_for_update(nowait=True))
            assert error.value.orig.sqlstate == '55P03'
    finally:
        resume.set()
    with pytest.raises(asyncio.CancelledError):
        await running
    assert not metadata.exists()

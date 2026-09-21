"""R1 regressions for real ancestor swaps and durable-proof invalidation seams."""
import asyncio
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import select

from tests.test_metadata_cleanup import completed_sidecar, invoke_cleanup
from tests.test_import_finalization_recovery import db  # noqa: F401

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def proved_sidecar(db, tmp_path, monkeypatch):
    from app.models import StorageArtifact, SubscriptionSource
    metadata = await completed_sidecar(db, tmp_path, monkeypatch)
    row = (await db.execute(select(StorageArtifact).where(
        StorageArtifact.storage_root == 'downloads', StorageArtifact.artifact_type == 'metadata_json'))).scalar_one()
    repository = (await db.execute(select(SubscriptionSource))).scalar_one()
    assert row.metadata_completion_proof and row.download_job_id is row.import_job_id is None
    return metadata, row, repository


async def assert_done_without_certificate_cannot_unlink(db, tmp_path, metadata, row):
    """Restore an otherwise complete fixture without any proof-clearing helper."""
    from app.models import DownloadJob, ImportJob, StorageArtifact, WorkSource
    assert row.metadata_completion_proof is None
    for artifact in (await db.execute(select(StorageArtifact).where(StorageArtifact.storage_root == 'downloads'))).scalars():
        artifact.state = 'done'
        artifact.lease_token = artifact.lease_expires_at = artifact.last_error = None
    row.download_job_id = row.import_job_id = None
    for parent in (await db.execute(select(DownloadJob))).scalars():
        parent.status = 'complete'
    for child in (await db.execute(select(ImportJob))).scalars():
        child.status, child.execution_token = 'complete', None
    source = (await db.execute(select(WorkSource))).scalar_one()
    source.source_work_id = row.source_work_id
    await db.commit()
    result = await invoke_cleanup(db, tmp_path / 'downloads')
    assert result['removed'] == 0 and metadata.exists()
    assert result['skipped_by_reason'].get('completion_evidence_missing_or_changed') == 1


@pytest.mark.parametrize('reuse_work_directory', [False, True], ids=['replacement-file', 'same-inode-subtree'])
async def test_ancestor_swap_after_persisted_intent_preserves_replacement(db, tmp_path, monkeypatch, reuse_work_directory):
    from app.database import async_session
    from app.models import TaskRun
    from app.services import metadata_cleanup
    from app.services.operations import ADMIN_DISPATCH_META_KEY
    metadata, row, _ = await proved_sidecar(db, tmp_path, monkeypatch)
    original_bytes = metadata.read_bytes()
    original_visit = metadata_cleanup.visit_candidate
    entered, resume = asyncio.Event(), asyncio.Event()
    captured = {}

    async def after_intent(*args):
        async with async_session() as observer:
            task = (await observer.execute(select(TaskRun).where(
                TaskRun.kind == 'admin', TaskRun.status == 'running'))).scalar_one()
            saved = task.meta[ADMIN_DISPATCH_META_KEY]['checkpoints']['metadata_cleanup']
            assert saved['pending']['artifact_id'] == str(row.id)
            assert saved['pending']['identity'] == args[3]
            captured.update(args[3])
        entered.set()
        await resume.wait()
        return await original_visit(*args)

    monkeypatch.setattr(metadata_cleanup, 'visit_candidate', after_intent)
    running = asyncio.create_task(invoke_cleanup(db, tmp_path / 'downloads'))
    try:
        await asyncio.wait_for(entered.wait(), 15)
        ancestor = metadata.parent.parent
        displaced = tmp_path / 'displaced-creator'
        ancestor.rename(displaced)
        ancestor.mkdir()
        outside = displaced / 'outside-sentinel.json'
        outside.write_bytes(b'{"outside":"must survive"}')
        if reuse_work_directory:
            # Preserve the sidecar's exact inode/stat and work-directory inode:
            # only its higher ancestor chain now differs from the saved intent.
            (displaced / metadata.parent.name).rename(metadata.parent)
            current = metadata.stat()
            assert [current.st_dev, current.st_ino, current.st_size,
                    current.st_mtime_ns, current.st_ctime_ns] == captured['file']
        else:
            metadata.parent.mkdir()
            metadata.write_bytes(original_bytes)
        replacement_sentinel = ancestor / 'replacement-sentinel.json'
        replacement_sentinel.write_bytes(b'{"replacement":"must survive"}')
        before = {p: p.read_bytes() for p in (metadata, outside, replacement_sentinel)}
    finally:
        resume.set()
    result = await asyncio.wait_for(running, 15)
    assert result['removed'] == 0 and result['scanned'] == result['skipped'] == 1
    assert set(result['skipped_by_reason']) <= {'file_replaced_since_intent', 'ancestor_replaced', 'file_replaced'}
    assert before == {p: p.read_bytes() for p in before}
    if not reuse_work_directory:
        assert (displaced / metadata.parent.name / metadata.name).read_bytes() == original_bytes


@pytest.mark.parametrize('mutation', ['reset', 'claim', 'reclaim', 'renew', 'release', 'import_intent'])
async def test_owned_mutations_clear_naturally_captured_proof(db, tmp_path, monkeypatch, mutation):
    from app.models import DownloadJob, ImportJob
    from app.services.artifact_ledger import ArtifactLedger
    from app.jobs.download import _prepare_import_intent
    metadata, row, repository = await proved_sidecar(db, tmp_path, monkeypatch)
    certificate = row.metadata_completion_proof
    parent = DownloadJob(subscription_id=repository.subscription_id, source='pixiv',
                         source_url=repository.source_url, status='downloaded')
    db.add(parent)
    await db.flush()
    child = ImportJob(download_job_id=parent.id, status='running', execution_token=uuid4(), execution_attempt=1)
    db.add(child)
    await db.flush()
    row.download_job_id = parent.id
    row.import_job_id = None if mutation == 'import_intent' else child.id
    row.state = 'failed' if mutation == 'reset' else 'new'
    if mutation in {'reclaim', 'renew', 'release'}:
        row.state = 'importing'
        row.lease_token = child.execution_token
        row.lease_expires_at = datetime.now(timezone.utc) + timedelta(seconds=-60 if mutation == 'reclaim' else 900)
    # A genuine certificate is a negative sentinel on a deliberately re-owned
    # fixture. No synthetic certificate is ever used to authorize deletion.
    row.metadata_completion_proof = certificate
    await db.commit()
    ledger = ArtifactLedger(db)
    if mutation == 'reset':
        assert await ledger.reset_retry_assignment(parent.id, child.id) == 1
    elif mutation in {'claim', 'reclaim'}:
        claim = await ledger.claim_work_batch(parent.id, child.id, lease_token=child.execution_token, limit=1)
        assert claim.claimed == (row.source_work_id,)
    elif mutation == 'renew':
        assert await ledger.renew_work_leases(parent.id, [row.source_work_id],
            expected_import_job_id=child.id, expected_lease_token=child.execution_token) == {row.source_work_id}
    elif mutation == 'release':
        assert await ledger.release_owned_leases(child.id, child.execution_token) == {row.source_work_id}
    else:
        prepared = await _prepare_import_intent(db, parent.id, import_error=None,
            new_json_paths={str(metadata)}, require_assignment=True)
        assert prepared and row.import_job_id is not None and row.import_job_id != child.id
    await db.commit()
    await db.refresh(row)
    assert row.metadata_completion_proof is None, mutation
    assert metadata.exists()
    # Return just the candidate to a done/unowned shape without another
    # proof-clearing helper. Old authority must not reappear or permit unlink.
    row.state, row.download_job_id, row.import_job_id = 'done', None, None
    row.lease_token = row.lease_expires_at = row.last_error = None
    parent.status = 'complete'
    for current_child in (await db.execute(select(ImportJob))).scalars():
        current_child.status, current_child.execution_token = 'complete', None
    await db.commit()
    result = await invoke_cleanup(db, tmp_path / 'downloads')
    assert result['removed'] == 0 and metadata.exists()
    assert result['skipped_by_reason'].get('completion_evidence_missing_or_changed') == 1


@pytest.mark.parametrize('existing', [True, False], ids=['existing-content', 'recovered-assignment'])
async def test_repository_reconciliation_clears_actual_proof(db, tmp_path, monkeypatch, existing):
    from app.models import DownloadJob, WorkSource
    from app.services.repository_artifact_reconciliation import reconcile_repository_artifacts
    metadata, row, repository = await proved_sidecar(db, tmp_path, monkeypatch)
    parent = DownloadJob(subscription_id=repository.subscription_id, subscription_source_id=repository.id,
                         source='pixiv', source_url=repository.source_url, status='downloaded')
    db.add(parent)
    row.state = 'new'
    if not existing:
        source = (await db.execute(select(WorkSource))).scalar_one()
        source.source_work_id = 'different-current-content'
    await db.commit()
    assert row.metadata_completion_proof is not None
    outcome = await reconcile_repository_artifacts(db, parent)
    await db.commit()
    await db.refresh(row)
    assert row.metadata_completion_proof is None
    if existing:
        assert row.state == 'done' and outcome.pending_work_count == 0
        parent.status = 'complete'
        await db.commit()
        result = await invoke_cleanup(db, tmp_path / 'downloads')
        assert result['removed'] == 0 and result['skipped_by_reason'].get('completion_evidence_missing_or_changed') == 1
    else:
        assert row.state == 'new' and row.download_job_id == parent.id
        assert outcome.recovered_metadata_count == outcome.pending_work_count == 1
        await assert_done_without_certificate_cannot_unlink(db, tmp_path, metadata, row)
    assert metadata.exists()


@pytest.mark.parametrize('mutation', ['existing', 'missing', 'importable', 'reset'])
async def test_disk_import_mutations_clear_actual_proof(db, tmp_path, monkeypatch, mutation):
    from app.models import StorageArtifact, WorkSource
    from app.jobs import download
    from app.services.disk_import import reconcile_downloads_to_db
    metadata, row, repository = await proved_sidecar(db, tmp_path, monkeypatch)
    artifact_id = row.id
    original_bytes = metadata.read_bytes()
    if mutation != 'reset':
        row.state = 'new'
    if mutation in {'missing', 'importable'}:
        source = (await db.execute(select(WorkSource))).scalar_one()
        source.source_work_id = 'different-current-content'
    if mutation == 'missing':
        metadata.unlink()
    await db.commit()
    assert row.metadata_completion_proof is not None
    observations = []

    async def observe_before_publication(download_job_id, **kwargs):
        # Only downstream queue publication is replaced. Read the committed
        # real disk-import mutation before import-intent code could clear it.
        from app.database import async_session
        async with async_session() as observer:
            actual = await observer.get(StorageArtifact, artifact_id)
            assert actual.metadata_completion_proof is None
            assert actual.state == 'new' and str(actual.download_job_id) == download_job_id
            observations.append(actual.file_path)
        return None

    monkeypatch.setattr(download, '_enqueue_import', observe_before_publication)
    monkeypatch.setattr('app.services.disk_identity.danbooru_svc.search_and_extract', lambda **_: (None, []))
    result = await reconcile_downloads_to_db(db, {'source': 'pixiv',
        'repository_id': str(repository.id), 'reset_ledger': mutation == 'reset'})
    await db.refresh(row)
    assert row.metadata_completion_proof is None, (mutation, result)
    if mutation == 'existing':
        assert row.state == 'done' and result['existing'] == 1
        cleanup = await invoke_cleanup(db, tmp_path / 'downloads')
        assert cleanup['removed'] == 0 and cleanup['skipped_by_reason'].get('completion_evidence_missing_or_changed') == 1
    elif mutation == 'missing':
        assert row.state == 'failed' and result['failed'] == 1
        assert row.last_error == 'Metadata file is missing from DOWNLOAD_ROOT'
        metadata.write_bytes(original_bytes)
    else:
        assert result['jobs'] == 1 and observations == [row.file_path]
    assert metadata.read_bytes() == original_bytes
    if mutation != 'existing':
        await assert_done_without_certificate_cannot_unlink(db, tmp_path, metadata, row)

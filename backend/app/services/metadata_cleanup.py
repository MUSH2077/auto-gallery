"""Fenced removal of proved, redundant gallery-dl metadata sidecars."""
import asyncio
import errno
import fcntl
import hashlib
import json
import os
import stat
from pathlib import Path

from sqlalchemy import select, or_
from sqlalchemy.exc import DBAPIError
from app.database import async_session
from app.models import StorageArtifact, DownloadJob, ImportJob, WorkSource, Work, AssetSource, Asset, TaskRun, RepositorySyncReceipt
from app.models.pipeline_outbox import ImportCurationOutbox
from app.services.metadata_cleanup_proof import artifact_version, domain_snapshots, json_digest, MAX_METADATA_BYTES, index_checkpoint, assigned_checkpoint_matches, MAX_ASSETS
from app.services.operations import current_admin_operation_attempt, fence_current_admin_operation_transaction
from app.services.download_staging import PROMOTION_LOCK_NAME, staging_enabled


class SkipMetadata(Exception):
    pass


def path_parts(relative):
    path = Path(relative)
    if path.is_absolute() or not path.parts or any(p in {'', '.', '..'} or p.startswith('.') for p in str(relative).split('/')):
        raise SkipMetadata('protected_path')
    return path.parts


def open_managed(root, relative):
    """Caller owns returned parent/file descriptors. Never follows symlinks."""
    parts = path_parts(relative)
    parent = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for component in parts[:-1]:
            following = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            os.close(parent)
            parent = following
        file_fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        if not stat.S_ISREG(os.fstat(file_fd).st_mode):
            os.close(file_fd)
            raise SkipMetadata('not_regular')
        return parent, file_fd
    except BaseException as exc:
        os.close(parent)
        if isinstance(exc, OSError) and exc.errno in (errno.ELOOP, errno.ENOTDIR):
            raise SkipMetadata('unsafe_path') from None
        raise


def directory_identity(root, relative):
    current = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    chain = []
    try:
        for component in (*path_parts(relative)[:-1], None):
            info = os.fstat(current)
            chain.append([info.st_dev, info.st_ino])
            if component is not None:
                following = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current)
                os.close(current)
                current = following
        return chain
    finally:
        os.close(current)


def read_json_fd(fd):
    os.lseek(fd, 0, os.SEEK_SET)
    chunks, remaining = [], MAX_METADATA_BYTES + 1
    while remaining:
        chunk = os.read(fd, min(remaining, 1024 * 1024))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    data = b''.join(chunks)
    if len(data) > MAX_METADATA_BYTES:
        raise SkipMetadata('metadata_too_large')
    try:
        value = json.loads(data)
    except (ValueError, UnicodeError):
        raise SkipMetadata('invalid_metadata') from None
    if not isinstance(value, dict):
        raise SkipMetadata('invalid_metadata')
    return value, hashlib.sha256(data).hexdigest()


def identity(fd):
    value = os.fstat(fd)
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns


def finalize_file(root, row, proof, projection, library_root, expected_identity, projected_artifact):
    """Synchronous bounded I/O; caller retains transaction, task fence and flock."""
    parent, fd = open_managed(root, row.file_path)
    try:
        initial = identity(fd)
        if not expected_identity or list(initial) != expected_identity['file'] or directory_identity(root, row.file_path) != expected_identity['parents']:
            raise SkipMetadata('file_replaced_since_intent')
        if initial[2:4] != (row.file_size, row.mtime_ns):
            raise SkipMetadata('file_version_changed')
        raw, digest = read_json_fd(fd)
        if json_digest(raw) != proof['domain']['retained_raw_metadata_sha256']:
            raise SkipMetadata('metadata_content_changed')
        from app.providers import registry
        try:
            parsed = registry.get(row.source).parse_work_source(raw)
        except (KeyError, ValueError, TypeError, AttributeError):
            raise SkipMetadata('provider_metadata_invalid') from None
        if (parsed.get('source'), str(parsed.get('source_work_id')), str(parsed.get('source_creator_id'))) != (row.source, row.source_work_id, str(proof['domain']['work_source'][4])):
            raise SkipMetadata('provider_identity_changed')
        try:
            lp, lf = open_managed(library_root, projection.metadata_path)
        except FileNotFoundError:
            raise SkipMetadata('projection_missing') from None
        try:
            if identity(lf)[2:4] != (projected_artifact.file_size, projected_artifact.mtime_ns):
                raise SkipMetadata('projection_version_changed')
            retained, _ = read_json_fd(lf)
            expected = proof['domain']['work_source']
            if (retained.get('work_id'), retained.get('source'), str(retained.get('source_work_id'))) != (expected[1], row.source, row.source_work_id):
                raise SkipMetadata('projection_identity_changed')
            expected_files = sorted(link[8] for link in proof['domain']['asset_sources'])
            projected_assets = retained.get('assets')
            if (not isinstance(projected_assets, list) or any(not isinstance(item, dict) for item in projected_assets)
                    or sorted(str(item.get('file_name')) for item in projected_assets) != expected_files
                    or not retained.get('library_version')):
                raise SkipMetadata('projection_identity_changed')
        finally:
            os.close(lf)
            os.close(lp)
        # Reopen from the configured root: a renamed ancestor is not authority
        # to unlink through the old, still-open directory descriptor.
        check_parent, check_fd = open_managed(root, row.file_path)
        try:
            if identity(check_fd) != initial or read_json_fd(check_fd)[1] != digest:
                raise SkipMetadata('file_replaced')
            entry = os.stat(Path(row.file_path).name, dir_fd=check_parent, follow_symlinks=False)
            if (entry.st_dev, entry.st_ino, entry.st_size, entry.st_mtime_ns, entry.st_ctime_ns) != initial:
                raise SkipMetadata('file_replaced')
            if directory_identity(root, row.file_path) != expected_identity['parents']:
                raise SkipMetadata('ancestor_replaced')
            os.unlink(Path(row.file_path).name, dir_fd=check_parent)
        finally:
            os.close(check_fd)
            os.close(check_parent)
    finally:
        os.close(fd)
        os.close(parent)


async def visit_candidate(artifact_id, root, library_root, expected_identity):
    from app.providers import registry
    async with async_session() as db:
        row = (await db.execute(select(StorageArtifact).where(StorageArtifact.id == artifact_id)
                               .with_for_update(nowait=True))).scalar_one_or_none()
        if row is None:
            raise SkipMetadata('ledger_removed')
        parts = path_parts(row.file_path)
        if row.storage_root != 'downloads' or row.artifact_type != 'metadata_json' or parts[-1] != row.file_name:
            raise SkipMetadata('artifact_identity_changed')
        related_ids = list((await db.execute(select(StorageArtifact.id).where(
            StorageArtifact.storage_root == 'downloads', StorageArtifact.source == row.source,
            StorageArtifact.source_work_id == row.source_work_id).order_by(StorageArtifact.id).limit(MAX_ASSETS * 2 + 1).with_for_update(nowait=True))).scalars())
        if len(related_ids) > MAX_ASSETS * 2:
            raise SkipMetadata('work_artifact_limit')
        from app.services.offline_restore import staging_root, receipts_root
        target = Path(root).absolute() / row.file_path
        if any(target.is_relative_to(protected.absolute()) for protected in (staging_root(), receipts_root())):
            raise SkipMetadata('protected_restore_path')
        if row.source == 'manual' or row.source not in registry.list_downloadable() or parts[0] != row.source or not row.file_path.endswith('.json'):
            raise SkipMetadata('protected_source')
        if not staging_enabled():
            raise SkipMetadata('unserialized_canonical_writers')
        if row.state != 'done' or row.lease_token or row.lease_expires_at or row.last_error:
            raise SkipMetadata('artifact_not_complete')
        proof = row.metadata_completion_proof
        owners = list((await db.execute(select(DownloadJob).where(DownloadJob.id == row.download_job_id)
                                       .with_for_update(nowait=True))).scalars())
        children = list((await db.execute(select(ImportJob).where(or_(ImportJob.download_job_id == row.download_job_id, ImportJob.id == row.import_job_id))
                                         .order_by(ImportJob.id).with_for_update(nowait=True))).scalars())
        if (row.download_job_id and not owners) or any(owner.status != 'complete' or owner.source != row.source for owner in owners) or any(child.status != 'complete' or child.execution_token for child in children):
            raise SkipMetadata('owner_not_complete')
        conflict = (await db.execute(select(StorageArtifact.id).where(
            StorageArtifact.source == row.source, StorageArtifact.source_work_id == row.source_work_id,
            StorageArtifact.storage_root == 'downloads', or_(StorageArtifact.state != 'done', StorageArtifact.lease_token.is_not(None), StorageArtifact.lease_expires_at.is_not(None))).limit(1))).scalar_one_or_none()
        if conflict:
            raise SkipMetadata('conflicting_assignment')
        active = (await db.execute(select(DownloadJob.id).where(DownloadJob.source == row.source,
            DownloadJob.status.not_in(('complete', 'failed', 'stale', 'cancelled'))).limit(1))).scalar_one_or_none()
        if active:
            raise SkipMetadata('active_source_writer')
        source = (await db.execute(select(WorkSource).where(WorkSource.source == row.source,
            WorkSource.source_work_id == row.source_work_id).with_for_update(nowait=True))).scalar_one_or_none()
        if source is None:
            raise SkipMetadata('domain_missing')
        await db.execute(select(Work.id).where(Work.id == source.work_id).with_for_update(nowait=True))
        links = list((await db.execute(select(AssetSource).where(AssetSource.work_source_id == source.id)
                                      .order_by(AssetSource.id).limit(MAX_ASSETS + 1).with_for_update(nowait=True))).scalars())
        if len(links) > MAX_ASSETS:
            raise SkipMetadata('work_asset_limit')
        await db.execute(select(Asset.id).where(Asset.id.in_([link.asset_id for link in links])).order_by(Asset.id).with_for_update(nowait=True))
        current = await domain_snapshots(db, {(row.source, row.source_work_id)})
        domain = current.get((row.source, row.source_work_id))
        if owners:
            parent = owners[0]
            child = next((item for item in children if item.id == row.import_job_id), None)
            if row.import_job_id and (child is None or child.download_job_id != parent.id):
                raise SkipMetadata('owner_not_complete')
            from app.services.import_completion_checkpoint import CHECKPOINT_KEY
            task = (await db.execute(select(TaskRun).where(TaskRun.subject_type == 'import_job',
                TaskRun.subject_id == row.import_job_id))).scalar_one_or_none() if child else None
            checkpoint_value = (task.meta or {}).get(CHECKPOINT_KEY) if task else None
            if child and ((task and task.reason_code in ('import_completion_identity_mismatch', 'import_completion_evidence_missing')) or (checkpoint_value is not None and (not domain or not assigned_checkpoint_matches(row, index_checkpoint(checkpoint_value), domain)))):
                raise SkipMetadata('completion_checkpoint_changed')
            if not domain:
                raise SkipMetadata('domain_missing')
            proof = {'version': 1, 'kind': 'assigned_import_complete' if child else 'terminal_success_existing_content',
                     'download_job_id': str(parent.id), 'import_job_id': str(child.id) if child else None,
                     'download_status': 'complete', 'download_attempt': parent.retry_count,
                     'import_status': 'complete' if child else None,
                     'import_attempt': child.execution_attempt if child else None,
                     'artifact': artifact_version(row), 'domain': domain}
        if (not isinstance(proof, dict) or proof.get('version') != 1
                or proof.get('kind') not in ('assigned_import_complete', 'terminal_success_existing_content')
                or proof.get('download_status') != 'complete' or not proof.get('download_job_id')
                or not isinstance(proof.get('download_attempt'), int) or proof['download_attempt'] < 0
                or proof.get('artifact') != artifact_version(row)
                or (row.import_job_id and str(row.import_job_id) != proof.get('import_job_id'))
                or (proof.get('kind') == 'assigned_import_complete' and
                    (proof.get('import_status') != 'complete' or not proof.get('import_job_id')
                     or not isinstance(proof.get('import_attempt'), int) or proof['import_attempt'] < 1))):
            raise SkipMetadata('completion_evidence_missing_or_changed')
        from uuid import UUID
        try:
            original_parent = UUID(proof['download_job_id'])
            if proof.get('import_job_id'):
                UUID(proof['import_job_id'])
        except (ValueError, TypeError):
            raise SkipMetadata('completion_evidence_missing_or_changed') from None
        receipt = (await db.execute(select(RepositorySyncReceipt).where(
            RepositorySyncReceipt.source_download_job_id == original_parent))).scalar_one_or_none()
        if receipt and receipt.status != 'complete':
            raise SkipMetadata('completion_receipt_conflict')
        if domain != proof.get('domain'):
            raise SkipMetadata('domain_identity_changed')
        projection = (await db.execute(select(ImportCurationOutbox).where(ImportCurationOutbox.work_id == source.work_id)
                                      .with_for_update(nowait=True))).scalar_one_or_none()
        if projection is None or projection.metadata_state != 'complete' or not projection.metadata_path or projection.metadata_lease_token or projection.metadata_lease_expires_at or projection.metadata_last_error:
            raise SkipMetadata('projection_not_complete')
        projected_artifact = (await db.execute(select(StorageArtifact).where(
            StorageArtifact.storage_root == 'library', StorageArtifact.file_path == projection.metadata_path,
            StorageArtifact.artifact_type == 'metadata_json', StorageArtifact.source == row.source,
            StorageArtifact.source_work_id == row.source_work_id).with_for_update(nowait=True))).scalar_one_or_none()
        if projected_artifact is None or projected_artifact.state != 'done':
            raise SkipMetadata('projection_artifact_missing')
        lock_fd = os.open(Path(root) / PROMOTION_LOCK_NAME, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise SkipMetadata('canonical_writer_busy') from None
            task = await fence_current_admin_operation_transaction(db, allowed_statuses=('running',))
            if task.operation_type != 'admin-cleanup-metadata-jsons':
                raise RuntimeError('Metadata cleanup requires its registered operation')
            await asyncio.to_thread(finalize_file, root, row, proof, projection, library_root, expected_identity, projected_artifact)
            await db.commit()
        finally:
            os.close(lock_fd)


async def cleanup_metadata_jsons(download_root=None):
    if current_admin_operation_attempt() is None:
        raise RuntimeError('Metadata cleanup requires a current registered operation; submit a new cleanup task')
    from app.config import settings
    from app.jobs.admin_operations import _await_admin_file_finalizer
    result = {'removed': 0, 'scanned': 0, 'skipped': 0, 'failed': 0, 'skipped_by_reason': {},
              'failed_by_reason': {}, 'errors': [], 'errors_truncated': 0, 'scope': 'managed_download_metadata'}
    from uuid import UUID
    from app.services.operations import get_current_admin_operation_checkpoint, set_current_admin_operation_checkpoint
    async with async_session() as checkpoint_db:
        saved = await get_current_admin_operation_checkpoint(checkpoint_db, 'metadata_cleanup') or {}
    result = saved.get('result') or result
    cursor = UUID(saved['cursor']) if saved.get('cursor') else None
    pending = saved.get('pending')

    async def checkpoint(pending_value, cursor_value):
        async with async_session() as checkpoint_db:
            await fence_current_admin_operation_transaction(checkpoint_db, allowed_statuses=('running',))
            await set_current_admin_operation_checkpoint(checkpoint_db, 'metadata_cleanup',
                {'result': result, 'cursor': str(cursor_value) if cursor_value else None, 'pending': pending_value},
                progress={'phase': 'running', 'label': f"Metadata cleanup: {result['removed']} removed, {result['skipped']} skipped, {result['failed']} failed"})
            await checkpoint_db.commit()

    root = download_root or settings.download_root
    while True:
        async with async_session() as db:
            query = select(StorageArtifact.id, StorageArtifact.file_path).where(StorageArtifact.storage_root == 'downloads', StorageArtifact.artifact_type == 'metadata_json')
            if cursor is not None:
                query = query.where(StorageArtifact.id > cursor)
            ids = list((await db.execute(query.order_by(StorageArtifact.id).limit(25))).all())
        if not ids:
            break
        for artifact_id, file_path in ids:
            if not pending or pending.get('artifact_id') != str(artifact_id):
                expected_identity = None
                try:
                    parent_fd, file_fd = open_managed(root, file_path)
                    try:
                        expected_identity = {'file': list(identity(file_fd)), 'parents': directory_identity(root, file_path)}
                    finally:
                        os.close(file_fd)
                        os.close(parent_fd)
                except (OSError, SkipMetadata):
                    pass
                pending = {'artifact_id': str(artifact_id), 'identity': expected_identity}
                await checkpoint(pending, cursor)
            outcome, reason = 'removed', None
            try:
                await _await_admin_file_finalizer(visit_candidate(artifact_id, root, settings.library_root, pending.get('identity')))
            except SkipMetadata as exc:
                outcome, reason = 'skipped', str(exc)
            except FileNotFoundError:
                outcome, reason = 'skipped', 'already_absent'
            except DBAPIError as exc:
                if getattr(exc.orig, 'sqlstate', None) != '55P03':
                    raise
                outcome, reason = 'skipped', 'row_busy'
            except OSError as exc:
                outcome, reason = 'failed', type(exc).__name__
            result['scanned'] += 1
            result[outcome] += 1
            if reason:
                counts = result[outcome + '_by_reason']
                counts[reason] = counts.get(reason, 0) + 1
                if outcome == 'failed':
                    if len(result['errors']) < 25:
                        result['errors'].append({'artifact_id': str(artifact_id), 'reason': reason})
                    else:
                        result['errors_truncated'] += 1
            cursor = artifact_id
            pending = None
            await checkpoint(None, cursor)
        await asyncio.sleep(0)  # Cooperative yield only; no waiting worker.
    result['message'] = (f"Cleanup partial failure: {result['removed']} removed, {result['failed']} failed, {result['skipped']} skipped"
                         if result['failed'] else f"Removed {result['removed']} metadata files; skipped {result['skipped']}")
    from app.schemas.data_center import MetadataCleanupResult
    result['status'] = 'partial' if result['failed'] else 'complete'
    return MetadataCleanupResult.model_validate(result).model_dump()

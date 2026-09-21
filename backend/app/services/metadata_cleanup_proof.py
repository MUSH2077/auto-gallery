"""Versioned successful sidecar evidence retained across task compaction.

This is a database-content snapshot, never an invented historical file digest.
Compaction already holds artifacts -> parents -> children -> tasks. All extra
reads here are ordinary MVCC reads; no filesystem or new domain locks.
"""
import hashlib
import json
from collections import defaultdict
from pathlib import Path

from sqlalchemy import select, tuple_, func, Text, cast, update
from app.models import StorageArtifact, DownloadJob, ImportJob, TaskRun, WorkSource, Work, AssetSource, Asset
from app.services.import_completion_checkpoint import CHECKPOINT_KEY

BATCH_SIZE = 25
MAX_METADATA_BYTES = 8 * 1024 * 1024
MAX_PROOF_BYTES = 64 * 1024
MAX_ASSETS = 1000
VERSION_FIELDS = ('id', 'storage_root', 'file_path', 'source', 'creator_dir', 'source_work_id',
                  'file_name', 'artifact_type', 'file_size', 'mtime_ns', 'content_version', 'attempts')


def json_digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()).hexdigest()


def artifact_version(row):
    return [str(getattr(row, key)) if key == 'id' else getattr(row, key) for key in VERSION_FIELDS]


async def domain_snapshots(db, identities):
    """Bounded MVCC queries; raw metadata is never loaded for an entire download."""
    if not identities:
        return {}
    sources = list((await db.execute(select(WorkSource).join(Work).where(
        tuple_(WorkSource.source, WorkSource.source_work_id).in_(identities),
        func.octet_length(cast(WorkSource.raw_metadata, Text)) <= MAX_METADATA_BYTES,
    ).limit(BATCH_SIZE + 1).execution_options(populate_existing=True))).scalars())
    if len(sources) > BATCH_SIZE:
        return {}
    assets = (await db.execute(select(AssetSource, Asset).join(Asset, Asset.id == AssetSource.asset_id).where(
        AssetSource.work_source_id.in_([s.id for s in sources]),
    ).limit(BATCH_SIZE * MAX_ASSETS + 1).execution_options(populate_existing=True))).all()
    if len(assets) > BATCH_SIZE * MAX_ASSETS:
        return {}
    media = list((await db.execute(select(StorageArtifact).where(
        StorageArtifact.storage_root == 'downloads',
        StorageArtifact.artifact_type.in_(('image', 'video')),
        tuple_(StorageArtifact.source, StorageArtifact.source_work_id).in_(identities),
    ).limit(BATCH_SIZE * MAX_ASSETS + 1).execution_options(populate_existing=True))).scalars())
    if len(media) > BATCH_SIZE * MAX_ASSETS:
        return {}
    assets_by_source, media_by_source = defaultdict(list), defaultdict(list)
    for link, asset in assets:
        assets_by_source[link.work_source_id].append((link, asset))
    for item in media:
        media_by_source[(item.source, item.source_work_id)].append(item)
    result = {}
    for source in sources:
        links = assets_by_source[source.id]
        if not links or len(links) > MAX_ASSETS or not isinstance(source.raw_metadata, dict):
            continue
        assigned = []
        links_by_identity = defaultdict(list)
        for link, asset in links:
            links_by_identity[(link.source, link.source_asset_id)].append((link, asset))
        for item in media_by_source[(source.source, source.source_work_id)]:
            matches = links_by_identity[(item.source, Path(item.file_name).stem)]
            if len(matches) != 1 or item.state != 'done' or item.lease_token or item.lease_expires_at or item.last_error:
                break
            link, asset = matches[0]
            assigned.append({'artifact': artifact_version(item), 'association':
                             [str(item.id), str(source.id), str(source.work_id), str(link.id), str(asset.id)]})
        if not assigned:
            continue
        if len(assigned) != len(media_by_source[(source.source, source.source_work_id)]):
            continue
        snapshot = {
            'work_source': [str(source.id), str(source.work_id), source.source, source.source_work_id, source.source_creator_id],
            'asset_sources': sorted([str(link.id), str(link.work_source_id), link.source, link.source_asset_id,
                                      link.ordinal, link.role, str(asset.id), asset.file_path, asset.file_name]
                                     for link, asset in links),
            'retained_raw_metadata_sha256': json_digest(source.raw_metadata),
            'assigned_media': sorted(assigned, key=lambda value: value['artifact'][0]),
        }
        if len(json.dumps(snapshot)) <= MAX_PROOF_BYTES:
            result[(source.source, source.source_work_id)] = snapshot
    return result


def index_checkpoint(checkpoint):
    if not isinstance(checkpoint, dict) or checkpoint.get('version') != 2:
        return None
    signature = checkpoint.get('signature')
    if not isinstance(signature, dict):
        return None
    for key, length in (('artifacts', 11), ('work_sources', 5), ('asset_sources', 9), ('assigned_media', 5)):
        rows = signature.get(key)
        if not isinstance(rows, list) or not rows or any(not isinstance(row, list) or len(row) != length or not isinstance(row[0], str)
                or any(value is not None and not isinstance(value, (str, int)) for value in row) for row in rows):
            return None
    by_source = defaultdict(list)
    for link in signature['asset_sources']:
        by_source[link[1]].append(link)
    return {**checkpoint, 'indexed_artifacts': {r[0]: r for r in signature['artifacts']},
            'indexed_sources': {r[0]: r for r in signature['work_sources']},
            'indexed_assets': {key: sorted(value) for key, value in by_source.items()},
            'indexed_media': {r[0]: r for r in signature['assigned_media']}}


def assigned_checkpoint_matches(row, checkpoint, domain):
    if not isinstance(checkpoint, dict) or checkpoint.get('version') != 2:
        return False
    if (checkpoint.get('download_job_id') != str(row.download_job_id)
            or checkpoint.get('import_job_id') != str(row.import_job_id)):
        return False
    expected = [str(row.id), str(row.download_job_id), str(row.import_job_id), row.source,
                row.source_work_id, row.artifact_type, row.file_path, row.file_name,
                row.file_size, row.mtime_ns, row.content_version]
    for media in domain['assigned_media']:
        version = dict(zip(VERSION_FIELDS, media['artifact']))
        expected_media = [version['id'], str(row.download_job_id), str(row.import_job_id),
                          version['source'], version['source_work_id'], version['artifact_type'],
                          version['file_path'], version['file_name'], version['file_size'],
                          version['mtime_ns'], version['content_version']]
        if (checkpoint['indexed_artifacts'].get(version['id']) != expected_media
                or checkpoint['indexed_media'].get(version['id']) != media['association']):
            return False
    checkpoint_media = sorted(value for value in checkpoint['indexed_media'].values() if value[1] == domain['work_source'][0])
    if checkpoint_media != sorted(value['association'] for value in domain['assigned_media']):
        return False
    return (expected == checkpoint['indexed_artifacts'].get(str(row.id))
            and domain['work_source'] == checkpoint['indexed_sources'].get(domain['work_source'][0])
            and checkpoint['indexed_assets'].get(domain['work_source'][0]) == domain['asset_sources'])


async def capture_compaction_proofs(db, download_ids):
    """Only called under the compactor's existing row locks, before FK nulling."""
    if not download_ids:
        return
    parents = {r.id: r for r in (await db.execute(select(DownloadJob).where(
        DownloadJob.id.in_(download_ids)).execution_options(populate_existing=True))).scalars()}
    children = list((await db.execute(select(ImportJob).where(
        ImportJob.download_job_id.in_(download_ids)).execution_options(populate_existing=True))).scalars())
    by_child = {r.id: r for r in children}
    unsafe = {r.download_job_id for r in children if r.status != 'complete' or r.execution_token}
    checkpoint_cache = {}
    cursor = None
    while True:
        query = select(StorageArtifact).where(StorageArtifact.download_job_id.in_(download_ids),
            StorageArtifact.storage_root == 'downloads', StorageArtifact.artifact_type == 'metadata_json')
        if cursor is not None:
            query = query.where(StorageArtifact.id > cursor)
        rows = list((await db.execute(query.order_by(StorageArtifact.id).limit(BATCH_SIZE)
                                     .execution_options(populate_existing=True))).scalars())
        if not rows:
            break
        cursor = rows[-1].id
        domains = await domain_snapshots(db, {(r.source, r.source_work_id) for r in rows if r.source != 'manual'})
        missing = {r.import_job_id for r in rows if r.import_job_id and r.import_job_id not in checkpoint_cache}
        if missing:
            tasks = (await db.execute(select(TaskRun.subject_id, TaskRun.meta[CHECKPOINT_KEY], TaskRun.reason_code).where(
                TaskRun.subject_type == 'import_job', TaskRun.subject_id.in_(missing)))).all()
            checkpoint_cache.update({identity: None for identity in missing})
            checkpoint_cache.update({key: index_checkpoint(value) for key, value, reason in tasks
                                     if reason not in ('import_completion_identity_mismatch', 'import_completion_evidence_missing')})
        updates = []
        for row in rows:
            proof = None
            domain = domains.get((row.source, row.source_work_id))
            parent = parents.get(row.download_job_id)
            child = by_child.get(row.import_job_id)
            safe = (parent and parent.status == 'complete' and parent.source == row.source and parent.id not in unsafe and domain
                    and row.state == 'done' and not row.lease_token and not row.lease_expires_at
                    and not row.last_error and row.source != 'manual')
            if safe and row.import_job_id:
                safe = (child and child.download_job_id == parent.id and child.status == 'complete'
                        and assigned_checkpoint_matches(row, checkpoint_cache.get(child.id), domain))
            if safe:
                proof = {'version': 1, 'kind': 'assigned_import_complete' if child else 'terminal_success_existing_content',
                         'download_job_id': str(parent.id), 'import_job_id': str(child.id) if child else None,
                         'download_status': parent.status, 'download_attempt': parent.retry_count,
                         'import_status': child.status if child else None,
                         'import_attempt': child.execution_attempt if child else None,
                         'artifact': artifact_version(row), 'domain': domain}
                if len(json.dumps(proof)) > MAX_PROOF_BYTES:
                    proof = None
            # Explicit DML only touches rows already held by the compactor.
            updates.append({'id': row.id, 'metadata_completion_proof': proof})
        await db.execute(update(StorageArtifact), updates)

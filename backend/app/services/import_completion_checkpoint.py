"""Recover terminal import work from durable, assignment-scoped evidence."""
from pathlib import Path

from sqlalchemy import select, tuple_

from app.models import Asset, AssetSource, StorageArtifact, TaskRun, Work, WorkSource

CHECKPOINT_KEY = "import_completion_checkpoint"


class ImportCompletionUnresolved(RuntimeError):
    def __init__(self, reason_code):
        self.reason_code = reason_code
        super().__init__(f"{reason_code}: Completed import evidence needs reconciliation; committed content is retained")


async def _assigned_artifacts(db, child):
    return list((await db.execute(select(StorageArtifact).where(
        StorageArtifact.download_job_id == child.download_job_id,
        StorageArtifact.import_job_id == child.id,
        StorageArtifact.storage_root == "downloads",
    ).execution_options(populate_existing=True))).scalars())


async def _committed_content(db, rows):
    metadata = [row for row in rows if row.artifact_type == "metadata_json"]
    if not metadata or any(row.state != "done" for row in rows):
        return None
    identities = {(row.source, row.source_work_id) for row in metadata}
    sources = list((await db.execute(select(WorkSource).join(Work, Work.id == WorkSource.work_id).where(
        tuple_(WorkSource.source, WorkSource.source_work_id).in_(identities),
    ).execution_options(populate_existing=True))).scalars())
    by_source = {(row.source, row.source_work_id): row for row in sources}
    if set(by_source) != identities:
        return None
    assets = (await db.execute(select(AssetSource, Asset).join(
        Asset, Asset.id == AssetSource.asset_id).where(AssetSource.work_source_id.in_([row.id for row in sources]))
        .execution_options(populate_existing=True))).all()
    if {link.work_source_id for link, _ in assets} != {row.id for row in sources}:
        return None
    # Importer's source_asset_id is the input media filename stem. Bind each
    # available image/video assignment to that exact source-work/source-asset
    # relationship, rather than accepting any asset attached to the work.
    assigned_media = []
    for row in rows:
        if row.artifact_type not in {"image", "video"}:
            continue
        source = by_source.get((row.source, row.source_work_id))
        matches = [(link, asset) for link, asset in assets if source is not None
                   and link.work_source_id == source.id and link.source == row.source
                   and link.source_asset_id == Path(row.file_name).stem]
        if len(matches) != 1:
            return None
        link, asset = matches[0]
        assigned_media.append([str(row.id), str(source.id), str(source.work_id), str(link.id), str(asset.id)])
    signature = {
        "artifacts": sorted([
            str(row.id), str(row.download_job_id), str(row.import_job_id), row.source,
            row.source_work_id, row.artifact_type, row.file_path, row.file_name,
            row.file_size, row.mtime_ns, row.content_version,
        ] for row in rows),
        "work_sources": sorted([str(row.id), str(row.work_id), row.source, row.source_work_id, row.source_creator_id] for row in sources),
        "asset_sources": sorted([
            str(link.id), str(link.work_source_id), link.source, link.source_asset_id,
            link.ordinal, link.role, str(asset.id), asset.file_path, asset.file_name,
        ] for link, asset in assets),
        "assigned_media": sorted(assigned_media),
    }
    return signature, len(sources)


async def save_import_completion_checkpoint(db, child, *, stats, total_groups, message):
    """Persist exact successful statistics before the terminal transaction."""
    if stats.get("skipped") or total_groups <= 0:
        return
    content = await _committed_content(db, await _assigned_artifacts(db, child))
    if content is None:
        return
    signature, source_count = content
    if source_count != total_groups or stats["works"] + stats["existing"] != total_groups:
        return
    task = (await db.execute(select(TaskRun).where(
        TaskRun.subject_type == "import_job", TaskRun.subject_id == child.id,
    ).with_for_update())).scalar_one_or_none()
    if task is not None:
        task.meta = {**(task.meta or {}), CHECKPOINT_KEY: {
            "version": 2, "download_job_id": str(child.download_job_id), "import_job_id": str(child.id),
            "stats": dict(stats), "total_groups": total_groups, "message": message,
            "signature": signature,
        }}


async def load_import_completion_checkpoint(db, child):
    if (child.execution_attempt or 0) < 2:
        return None
    rows = await _assigned_artifacts(db, child)
    task = (await db.execute(select(TaskRun).where(
        TaskRun.subject_type == "import_job", TaskRun.subject_id == child.id,
    ).execution_options(populate_existing=True))).scalar_one_or_none()
    checkpoint = (task.meta or {}).get(CHECKPOINT_KEY) if task else None
    if checkpoint:
        # Version 1 contains only independent ID sets and cannot prove the
        # original Work or media associations. Do not upgrade it by inference.
        if checkpoint.get("version") != 2:
            raise ImportCompletionUnresolved("import_completion_evidence_missing")
        content = await _committed_content(db, rows)
        if (content is None or checkpoint.get("download_job_id") != str(child.download_job_id)
                or checkpoint.get("import_job_id") != str(child.id)
                or checkpoint.get("signature") != content[0]):
            raise ImportCompletionUnresolved("import_completion_identity_mismatch")
        return checkpoint
    metadata = [row for row in rows if row.artifact_type == "metadata_json"]
    if metadata and all(row.state == "done" for row in metadata):
        # Chronology cannot attribute domain creation or recover exact stats.
        # This is known completed input with missing evidence, not empty JSON.
        raise ImportCompletionUnresolved("import_completion_evidence_missing")
    return None

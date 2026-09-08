"""Recover terminal import work from durable, assignment-scoped evidence."""
from collections import Counter
from uuid import UUID

from sqlalchemy import select

from app.models import Asset, AssetSource, ImportJob, StorageArtifact, TaskRun, Work, WorkSource

CHECKPOINT_KEY = "import_completion_checkpoint"


async def _committed_content(db, child):
    rows = list((await db.execute(select(StorageArtifact).where(
        StorageArtifact.download_job_id == child.download_job_id,
        StorageArtifact.import_job_id == child.id,
        StorageArtifact.storage_root == "downloads",
    ))).scalars())
    metadata = [row for row in rows if row.artifact_type == "metadata_json"]
    # Empty/missing/failed inputs are not completion evidence. A DONE ledger
    # alone is also insufficient: every work must still have committed domain
    # and media identities, not merely a historical filename.
    if not metadata or any(row.state != "done" for row in rows):
        return None
    identities = {(row.source, row.source_work_id) for row in metadata}
    from sqlalchemy import tuple_
    sources = list((await db.execute(select(WorkSource).join(Work, Work.id == WorkSource.work_id).where(
        tuple_(WorkSource.source, WorkSource.source_work_id).in_(identities),
    ))).scalars())
    if {(row.source, row.source_work_id) for row in sources} != identities:
        return None
    assets = (await db.execute(select(AssetSource.work_source_id, Asset.id, Asset.created_at).join(
        Asset, Asset.id == AssetSource.asset_id).where(AssetSource.work_source_id.in_([row.id for row in sources])))).all()
    media_counts = Counter(row.work_source_id for row in assets)
    if any(not media_counts[row.id] for row in sources):
        return None
    signature = {
        "artifact_ids": sorted(str(row.id) for row in rows),
        "work_source_ids": sorted(str(row.id) for row in sources),
        "asset_ids": sorted({str(row.id) for row in assets}),
    }
    return signature, sources, assets, media_counts


async def save_import_completion_checkpoint(db, child, *, stats, total_groups, message):
    """Persist exact successful statistics before the terminal transaction."""
    if stats.get("skipped") or total_groups <= 0:
        return
    content = await _committed_content(db, child)
    if content is None:
        return
    signature, sources, _, _ = content
    if len(sources) != total_groups or stats["works"] + stats["existing"] != total_groups:
        return
    task = (await db.execute(select(TaskRun).where(
        TaskRun.subject_type == "import_job", TaskRun.subject_id == child.id,
    ).with_for_update())).scalar_one_or_none()
    if task is not None:
        task.meta = {**(task.meta or {}), CHECKPOINT_KEY: {
            "version": 1, "download_job_id": str(child.download_job_id),
            "stats": dict(stats), "total_groups": total_groups, "message": message,
            "signature": signature,
        }}


async def load_import_completion_checkpoint(db, child):
    if (child.execution_attempt or 0) < 2:
        return None
    content = await _committed_content(db, child)
    if content is None:
        return None
    signature, sources, assets, media_counts = content
    task = (await db.execute(select(TaskRun).where(
        TaskRun.subject_type == "import_job", TaskRun.subject_id == child.id,
    ))).scalar_one_or_none()
    checkpoint = (task.meta or {}).get(CHECKPOINT_KEY) if task else None
    if checkpoint:
        if (checkpoint.get("version") == 1
                and UUID(checkpoint["download_job_id"]) == child.download_job_id
                and checkpoint.get("signature") == signature):
            return checkpoint
        return None
    # Compatibility for an already committed import from before checkpoints:
    # new domain identities created within this import establish exact stats.
    # An older/updated existing work is ambiguous without its saved statistics;
    # do not turn such an unknown case into a guessed success.
    if any(row.created_at < child.created_at for row in sources) or any(row.created_at < child.created_at for row in assets):
        return None
    return {
        "stats": {"works": len(sources), "assets": len(signature["asset_ids"]),
                  "existing": 0, "skipped": 0, "multi_page": sum(count > 1 for count in media_counts.values())},
        "total_groups": len(sources), "message": "Recovered committed import completion",
    }

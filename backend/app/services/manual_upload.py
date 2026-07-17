"""Manual upload service.

Accepts already-received files (streamed to temp paths under DOWNLOAD_ROOT by
the API layer -- see app/api/upload.py) and feeds them through the SAME
download_job -> artifact_ledger -> import_job pipeline used by gallery-dl
downloads and disk-import recovery (app/services/disk_import.py).

DownloadJob.subscription_id/subscription_source_id are NOT NULL (see
app/models/download_job.py), so -- exactly like disk_import.py's
reconcile_downloads_to_db() -- a manual upload must provision a
Subscription/SubscriptionSource/SourceCreator identity chain via
app/services/disk_identity.py before it can create the DownloadJob row.
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.creator import Creator
from app.models.download_job import DownloadJob
from app.models.source_creator import SourceCreator
from app.models.user import User
from app.services.artifact_ledger import ArtifactLedger, artifact_row
from app.services.disk_identity import MetadataIdentity, provision_identity_for_disk_import

logger = logging.getLogger(__name__)

ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".mp4", ".webm"}
MAX_FILE_SIZE_BYTES = 500 * 1024 * 1024  # 500MB per file


class QuotaExceededError(Exception):
    """Raised when an upload would push the user past upload_quota_bytes."""


@dataclass(slots=True)
class UploadFileInput:
    """One already-received file.

    Bytes are on disk at temp_path (the API layer must place this under
    DOWNLOAD_ROOT so the service's os.replace() into final storage stays on
    one filesystem). original_name is the client-supplied filename --
    untrusted, used only inside metadata.json, never as a path component.
    """
    temp_path: Path
    original_name: str


@dataclass(slots=True)
class UploadResult:
    work_id: str
    download_job_id: str
    import_job_id: str | None
    used_bytes: int
    quota_bytes: int | None


def _sniff_magic(header: bytes, ext: str) -> bool:
    """Validate file content matches its extension via magic bytes.

    An extension allowlist alone is not enough -- a renamed executable with a
    .jpg extension would otherwise sail through. Table per task-8-brief.md.
    """
    if ext in (".jpg", ".jpeg"):
        return header[:2] == b"\xff\xd8"
    if ext == ".png":
        return header[:4] == b"\x89PNG"
    if ext == ".gif":
        return header[:4] == b"GIF8"
    if ext == ".webp":
        return header[:4] == b"RIFF" and header[8:12] == b"WEBP"
    if ext == ".mp4":
        return header[4:8] == b"ftyp"
    if ext == ".webm":
        return header[:4] == b"\x1a\x45\xdf\xa3"
    return False


def _assert_contained(path: Path, root: Path) -> None:
    """Path.relative_to containment check -- never string-prefix matching."""
    try:
        path.resolve().relative_to(root)
    except ValueError:
        raise ValueError("Resolved upload path escapes DOWNLOAD_ROOT")


class ManualUploadService:
    async def save_upload(
        self,
        db: AsyncSession,
        user: User,
        files: list[UploadFileInput],
        meta: dict,
    ) -> UploadResult:
        if not files:
            raise ValueError("At least one file is required")

        creator_id = meta.get("creator_id")
        title = meta.get("title")
        tags_raw = meta.get("tags") or ""
        is_nsfw = bool(meta.get("is_nsfw"))

        # ---- Permission check first: zero I/O, fail fast ----
        if creator_id and not (user.is_admin or "curation" in (user.permissions or [])):
            raise PermissionError("curation permission required to target an existing creator")

        # ---- Per-file validation: extension, magic bytes, size ----
        validated: list[tuple[UploadFileInput, str, int]] = []
        total_size = 0
        for f in files:
            ext = Path(f.original_name).suffix.lower()
            if ext not in ALLOWED_EXTENSIONS:
                raise ValueError(f"Unsupported file extension for {f.original_name!r}")
            size = f.temp_path.stat().st_size
            if size > MAX_FILE_SIZE_BYTES:
                raise ValueError(f"{f.original_name} exceeds the 500MB per-file limit")
            with open(f.temp_path, "rb") as fh:
                header = fh.read(16)
            if not _sniff_magic(header, ext):
                raise ValueError(f"{f.original_name} failed content validation (extension/content mismatch)")
            validated.append((f, ext, size))
            total_size += size

        # ---- Quota check (before any writes) ----
        if user.upload_quota_bytes is not None and user.upload_used_bytes + total_size > user.upload_quota_bytes:
            raise QuotaExceededError(
                f"Upload of {total_size} bytes would exceed quota "
                f"({user.upload_used_bytes}/{user.upload_quota_bytes} bytes used)"
            )

        # ---- Creator identity resolution ----
        source_creator_id, source_url, display_name, target_creator_id = await self._resolve_identity(
            db, user, creator_id
        )

        # ---- Storage: DOWNLOAD_ROOT/manual/user_{username}/{work_uuid}/ ----
        work_uuid = str(uuid.uuid4())
        root = Path(settings.download_root).resolve()
        work_dir = root / "manual" / f"user_{user.username}" / work_uuid
        _assert_contained(work_dir, root)
        work_dir.mkdir(parents=True, exist_ok=True)

        file_records = []
        saved_paths: list[Path] = []
        for f, ext, size in validated:
            dest_name = f"{uuid.uuid4().hex}{ext}"
            dest_path = work_dir / dest_name
            _assert_contained(dest_path, root)
            os.replace(f.temp_path, dest_path)
            saved_paths.append(dest_path)
            file_records.append({"name": dest_name, "original_name": f.original_name, "size": size})

        tag_list = [t.strip() for t in tags_raw.split(",") if t.strip()]
        metadata = {
            "category": "manual",
            "id": work_uuid,
            "title": title,
            "tags": tag_list,
            "is_nsfw": is_nsfw,
            "uploaded_by": user.username,
            "target_creator_id": target_creator_id,
            "files": file_records,
            "date": datetime.now(timezone.utc).isoformat(),
        }
        json_path = work_dir / "metadata.json"
        with open(json_path, "w", encoding="utf-8") as jf:
            json.dump(metadata, jf, ensure_ascii=False)

        # ---- DownloadJob.subscription_id is NOT NULL: provision the
        # identity chain via disk_identity.py exactly as disk_import.py's
        # reconcile_downloads_to_db() does ----
        identity = MetadataIdentity(
            source="manual",
            source_creator_id=source_creator_id,
            source_url=source_url,
            display_name=display_name,
            raw_metadata={},
            creator_dir=f"user_{user.username}",
        )
        provisioned = await provision_identity_for_disk_import(db, identity, prefer_danbooru=False)
        # Manual uploads are user-intentional. Unlike gallery-dl-discovered
        # subscription sources, which default to is_enabled=False pending
        # admin review (CLAUDE.md: "Only Pixiv defaults to is_enabled=True on
        # import; others default disabled"), this is a documented exception --
        # see ManualProvider.parse_work_source().
        provisioned.subscription_source.is_enabled = True

        # subscription_source_id is deliberately left unset: download_jobs has
        # a partial unique index (uq_download_jobs_active_source, migration
        # a6c8e0f2b4d6) allowing at most one "active" (enqueued/downloading/
        # downloaded/importing) DownloadJob per subscription_source_id -- a
        # guard against concurrent gallery-dl re-syncs of the same creator.
        # Every manual upload's identity chain reuses the SAME
        # SubscriptionSource (one per user/target-creator), so setting it
        # here would make the second concurrent/back-to-back upload for that
        # same identity violate the constraint. subscription_id (the actual
        # NOT NULL FK) is still satisfied via provisioned.subscription.id.
        job = DownloadJob(
            source="manual",
            source_url=f"manual://{work_uuid}",
            status="downloaded",
            subscription_id=provisioned.subscription.id,
            subscription_source_id=None,
        )
        db.add(job)
        await db.flush()

        rows = []
        seen: set[str] = set()
        for p in [*saved_paths, json_path]:
            ar = artifact_row(p, root, job.id)
            if ar and ar["file_path"] not in seen:
                seen.add(ar["file_path"])
                rows.append(ar)
        await ArtifactLedger(db).upsert_many(rows)

        db_user = await db.get(User, user.id)
        db_user.upload_used_bytes = (db_user.upload_used_bytes or 0) + total_size

        await db.commit()

        from app.jobs.download import _enqueue_import
        import_job_id = await _enqueue_import(str(job.id), new_json_paths={str(json_path)})

        return UploadResult(
            work_id=work_uuid,
            download_job_id=str(job.id),
            import_job_id=import_job_id,
            used_bytes=db_user.upload_used_bytes,
            quota_bytes=db_user.upload_quota_bytes,
        )

    async def _resolve_identity(
        self, db: AsyncSession, user: User, creator_id: str | None
    ) -> tuple[str, str, str, str | None]:
        """Return (source_creator_id, source_url, display_name, target_creator_id)."""
        if creator_id:
            try:
                creator_uuid = uuid.UUID(str(creator_id))
            except (ValueError, AttributeError, TypeError):
                raise ValueError(f"Invalid creator_id: {creator_id!r}")
            creator = await db.get(Creator, creator_uuid)
            if not creator:
                raise ValueError(f"Creator {creator_id} not found")
            source_creator_id = f"creator:{creator.id}"
            source_url = f"manual://{source_creator_id}"
            display_name = creator.display_name or creator.name
            # Pin the manual identity to this specific creator BEFORE calling
            # provision_identity_for_disk_import(): that helper resolves an
            # existing creator purely by (source, source_creator_id) identity
            # match and otherwise CREATES a new creator -- it has no
            # parameter for "attach to this already-known creator".
            # Pre-creating the SourceCreator row here makes its identity
            # lookup resolve to the curator-chosen creator instead of
            # spawning a duplicate.
            existing_sc = (await db.execute(select(SourceCreator).where(
                SourceCreator.source == "manual",
                SourceCreator.source_creator_id == source_creator_id,
            ))).scalar_one_or_none()
            if not existing_sc:
                db.add(SourceCreator(
                    creator_id=creator.id,
                    source="manual",
                    source_creator_id=source_creator_id,
                    source_url=source_url,
                    display_name=display_name,
                ))
                await db.flush()
            return source_creator_id, source_url, display_name, str(creator.id)

        # Default: get-or-create the uploading user's personal space.
        source_creator_id = f"user:{user.username}"
        source_url = f"manual://{source_creator_id}"
        display_name = user.display_name or user.username
        return source_creator_id, source_url, display_name, None

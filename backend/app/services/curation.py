from __future__ import annotations

from datetime import date, datetime, time, timezone
import hashlib
import json
from pathlib import Path
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import func, or_, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import (
    Asset,
    AssetSource,
    AssetStorageState,
    Creator,
    CreatorCurationState,
    CurationChange,
    CurationCommit,
    SourceCreator,
    SubscriptionSource,
    VisualAssetGroup,
    VisualAssetMember,
    Work,
    WorkCurationState,
    WorkSource,
    WorkTag,
    Tag,
)
from app.schemas.curation import CurationChangeRead, CurationCommitRead
from app.services.search_projection_outbox import request_search_projection


VISIBLE = "visible"
TRASHED = "trashed"
PURGED = "purged"
ARCHIVED = "archived"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _json_value(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_value(item) for item in value]
    return value


def _safe_path(root: Path, relative: str | None) -> Path | None:
    if not relative:
        return None
    full = (root / relative).resolve()
    try:
        full.relative_to(root.resolve())
    except ValueError:
        return None
    return full


def _parse_datetime(value) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, date):
        return datetime.combine(value, time.min, tzinfo=timezone.utc)
    text = str(value).strip()
    if not text:
        return None
    candidates = [
        text,
        text.replace("Z", "+00:00"),
        text.replace(" ", "T"),
    ]
    for candidate in candidates:
        try:
            parsed = datetime.fromisoformat(candidate)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text[:len(fmt)], fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return None


def _day_key(value: datetime) -> str:
    return value.date().isoformat()


class CurationService:
    COMMIT_WORK_LIMIT = 25

    def __init__(self, db: AsyncSession):
        self.db = db

    async def _latest_commit_id(self) -> UUID | None:
        result = await self.db.execute(
            select(CurationCommit.id)
            .order_by(CurationCommit.created_at.desc(), CurationCommit.id.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def _create_commit(
        self,
        *,
        message: str,
        trigger: str,
        actor_type: str = "admin",
        actor_id: str | None = None,
        reverts_commit_id: UUID | None = None,
        metadata: dict | None = None,
        occurred_at: datetime | None = None,
        status: str = "active",
        dedupe_key: str | None = None,
    ) -> CurationCommit:
        # Gitllery applies after_state snapshots, so commit creation order is a
        # correctness boundary rather than merely presentation metadata.  The
        # transaction-scoped advisory lock closes an invisible-predecessor
        # hole: a later transaction cannot commit/project before the commit it
        # names as parent.  It is cheap because imports already coalesce up to
        # 25 work changes into one CurationCommit.
        await self.db.execute(
            text(
                "SELECT pg_advisory_xact_lock("
                "hashtextextended(:lock_name, 0))"
            ),
            {"lock_name": "auto-gallery:curation-commit-order"},
        )
        if dedupe_key:
            existing = await self.db.execute(select(CurationCommit).where(CurationCommit.dedupe_key == dedupe_key))
            existing_commit = existing.scalar_one_or_none()
            if existing_commit:
                # A post-migration commit already has a trigger-created outbox
                # row, including failed/retry state.  Creating a new intent for
                # a pre-migration historical dedupe hit could replay old
                # after_state over the current projection.
                return existing_commit
        created_at = _now()
        commit = CurationCommit(
            parent_commit_id=await self._latest_commit_id(),
            actor_type=actor_type,
            actor_id=actor_id,
            message=message,
            trigger=trigger,
            dedupe_key=dedupe_key,
            reverts_commit_id=reverts_commit_id,
            status=status,
            stats={},
            extra_metadata=metadata or {},
            created_at=created_at,
            updated_at=created_at,
        )
        if occurred_at:
            commit.occurred_at = occurred_at
        self.db.add(commit)
        await self.db.flush()
        # Persist the projection intent in the same transaction as every
        # curation commit.  Filesystem projection may lag, but a process crash
        # can no longer strand a committed audit record before enqueue.
        from app.services.gitllery_outbox import request_gitllery_projection

        await request_gitllery_projection(self.db, commit.id)
        return commit

    async def _add_change(
        self,
        commit: CurationCommit,
        *,
        subject_type: str,
        subject_id: str,
        action: str,
        sequence: int | None = None,
        before_state: dict | None,
        after_state: dict | None,
        impact: dict | None = None,
    ) -> CurationChange:
        before_state = _json_value(before_state) if before_state is not None else None
        after_state = _json_value(after_state) if after_state is not None else None
        impact = _json_value(impact) if impact is not None else {}
        diff = self._diff(before_state or {}, after_state or {})
        change = CurationChange(
            commit_id=commit.id,
            subject_type=subject_type,
            subject_id=subject_id,
            action=action,
            sequence=sequence,
            before_state=before_state,
            after_state=after_state,
            diff=diff,
            impact=impact,
        )
        self.db.add(change)
        await self.db.flush()
        return change

    async def execute_gitllery_command(
        self,
        command,
        *,
        actor_id: str | None,
        dry_run: bool = False,
    ) -> dict:
        """Apply one bounded Gitllery command as one authoritative DB commit.

        The advisory transaction lock protects both expected-head comparison
        and idempotency. No filesystem work occurs in this transaction.
        """

        dedupe_key = f"cli:{command.idempotency_key}"
        command_hash = hashlib.sha256(
            json.dumps(
                command.model_dump(mode="json"),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        await self.db.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:name, 0))"),
            {"name": "auto-gallery:curation-commit-order"},
        )
        existing = (
            await self.db.execute(
                select(CurationCommit).where(CurationCommit.dedupe_key == dedupe_key)
            )
        ).scalar_one_or_none()
        if existing is not None:
            if (existing.extra_metadata or {}).get("command_hash") != command_hash:
                raise HTTPException(
                    status_code=409,
                    detail={"code": "idempotency_key_reused"},
                )
            return {
                "status": "noop" if not (existing.stats or {}).get("work_count") else "committed",
                "commit_id": existing.id,
                "parent_commit_id": existing.parent_commit_id,
                "changed": int((existing.stats or {}).get("work_count") or 0),
                "skipped": int((existing.stats or {}).get("skipped") or 0),
                "outbox_state": "pending",
                "projected_lag_seconds": None,
            }

        current_head = await self._latest_commit_id()
        if (
            command.expected_parent_commit_id is not None
            and command.expected_parent_commit_id != current_head
        ):
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "gitllery_head_changed",
                    "expected": str(command.expected_parent_commit_id),
                    "actual": str(current_head) if current_head else None,
                },
            )

        work_ids = list(dict.fromkeys(operation.work_id for operation in command.operations))
        works = list(
            (
                await self.db.execute(
                    select(Work).where(Work.id.in_(work_ids)).order_by(Work.id)
                )
            ).scalars()
        )
        works_by_id = {work.id: work for work in works}
        missing = [str(work_id) for work_id in work_ids if work_id not in works_by_id]
        if missing:
            raise HTTPException(
                status_code=404,
                detail={"code": "works_not_found", "work_ids": missing},
            )
        states = {
            state.work_id: state
            for state in (
                await self.db.execute(
                    select(WorkCurationState).where(
                        WorkCurationState.work_id.in_(work_ids)
                    )
                )
            ).scalars()
        }
        tag_rows = (
            await self.db.execute(
                select(WorkTag.work_id, Tag.id, Tag.normalized_name)
                .join(Tag, Tag.id == WorkTag.tag_id)
                .where(WorkTag.work_id.in_(work_ids))
            )
        ).all()
        tags_by_work: dict[UUID, dict[str, UUID]] = {work_id: {} for work_id in work_ids}
        for work_id, tag_id, name in tag_rows:
            tags_by_work[work_id][name] = tag_id
        persisted_tag_ids = {
            work_id: dict(tag_ids) for work_id, tag_ids in tags_by_work.items()
        }

        plans: list[
            tuple[object, Work, WorkCurationState | None, dict, dict, bool]
        ] = []
        skipped = 0
        for operation in command.operations:
            work = works_by_id[operation.work_id]
            state = states.get(work.id)
            before = self._work_snapshot(work, state)
            before["tags"] = sorted(tags_by_work[work.id])
            after = dict(before)
            action = operation.action
            if action == "trash":
                if (state.visibility if state else VISIBLE) in {TRASHED, PURGED}:
                    skipped += 1
                    plans.append((operation, work, state, before, before, False))
                    continue
                after.update(visibility=TRASHED, reason=operation.reason or command.reason)
            elif action == "restore":
                if (state.visibility if state else VISIBLE) != TRASHED:
                    skipped += 1
                    plans.append((operation, work, state, before, before, False))
                    continue
                after.update(visibility=VISIBLE, reason=operation.reason or command.reason)
            elif action == "favorite":
                if work.is_favorite == bool(operation.value):
                    skipped += 1
                    plans.append((operation, work, state, before, before, False))
                    continue
                after["is_favorite"] = bool(operation.value)
            else:
                normalized = (operation.tag or "").strip().casefold()
                tag_exists = normalized in tags_by_work[work.id]
                if (action == "tag-add" and tag_exists) or (
                    action == "tag-remove" and not tag_exists
                ):
                    skipped += 1
                    plans.append((operation, work, state, before, before, False))
                    continue
                updated_tags = set(tags_by_work[work.id])
                if action == "tag-add":
                    updated_tags.add(normalized)
                    tags_by_work[work.id][normalized] = UUID(int=0)
                else:
                    updated_tags.remove(normalized)
                    tags_by_work[work.id].pop(normalized, None)
                after["tags"] = sorted(updated_tags)
            plans.append((operation, work, state, before, after, True))

        if dry_run:
            await self.db.rollback()
            return {
                "status": "noop" if len(plans) == skipped else "committed",
                "commit_id": None,
                "parent_commit_id": current_head,
                "changed": len(plans) - skipped,
                "skipped": skipped,
                "outbox_state": "preview",
                "projected_lag_seconds": None,
            }

        commit = await self._create_commit(
            message=command.message,
            trigger="gitllery_cli",
            actor_type="user",
            actor_id=actor_id,
            metadata={
                "reason": command.reason,
                "command_version": command.version,
                "command_hash": command_hash,
            },
            dedupe_key=dedupe_key,
        )
        for sequence, (
            operation,
            work,
            state,
            before,
            after,
            will_change,
        ) in enumerate(plans):
            action = operation.action
            if will_change and action in {"trash", "restore"}:
                if state is None:
                    state = WorkCurationState(work_id=work.id, visibility=VISIBLE)
                    self.db.add(state)
                    states[work.id] = state
                state.visibility = after["visibility"]
                state.reason = after.get("reason")
                if action == "trash":
                    state.trashed_at = _now()
                    state.trashed_by_commit_id = commit.id
                else:
                    state.restored_by_commit_id = commit.id
            elif will_change and action == "favorite":
                work.is_favorite = bool(operation.value)
            elif will_change and action == "tag-add":
                normalized = (operation.tag or "").strip().casefold()
                tag_id = (
                    await self.db.execute(
                        pg_insert(Tag)
                        .values(normalized_name=normalized)
                        .on_conflict_do_update(
                            index_elements=[Tag.normalized_name],
                            set_={"normalized_name": normalized},
                        )
                        .returning(Tag.id)
                    )
                ).scalar_one()
                await self.db.execute(
                    pg_insert(WorkTag)
                    .values(work_id=work.id, tag_id=tag_id, source="gitllery-cli")
                    .on_conflict_do_nothing(
                        index_elements=[WorkTag.work_id, WorkTag.tag_id]
                    )
                )
            elif will_change:
                normalized = (operation.tag or "").strip().casefold()
                tag_id = persisted_tag_ids[work.id].get(normalized)
                if tag_id:
                    await self.db.execute(
                        WorkTag.__table__.delete().where(
                            WorkTag.work_id == work.id,
                            WorkTag.tag_id == tag_id,
                        )
                    )
            await self._add_change(
                commit,
                subject_type="work",
                subject_id=str(work.id),
                action=(
                    {
                        "trash": "work_trashed",
                        "restore": "work_restored",
                        "favorite": "work_favorited",
                        "tag-add": "work_tag_added",
                        "tag-remove": "work_tag_removed",
                    }[action]
                    if will_change
                    else "command_noop"
                ),
                sequence=sequence,
                before_state=before,
                after_state=after,
                impact={
                    "source": "gitllery-cli",
                    "requested_action": action,
                    "changed": will_change,
                },
            )
        changed = len(plans) - skipped
        commit.stats = {"work_count": changed, "skipped": skipped}
        await request_search_projection(self.db, work_ids)
        await self.db.commit()
        return {
            "status": "committed" if changed else "noop",
            "commit_id": commit.id,
            "parent_commit_id": commit.parent_commit_id,
            "changed": changed,
            "skipped": skipped,
            "outbox_state": "pending",
            "projected_lag_seconds": 0.0,
        }

    async def record_asset_dedup_decision(
        self,
        *,
        case_id: UUID,
        left_asset_id: UUID,
        right_asset_id: UUID,
        action: str,
        actor_type: str,
        actor_id: str | None,
        evidence: dict,
        previous_status: str,
        representative_asset_id: UUID | None = None,
        group_id: UUID | None = None,
        reason: str | None = None,
        dedupe_key: str,
    ) -> CurationCommit:
        """Record one asset-level reconciliation decision in the curation ledger."""
        commit = await self._create_commit(
            message=f"Asset dedup decision: {action}",
            trigger="asset_dedup",
            actor_type=actor_type,
            actor_id=actor_id,
            metadata={"reason": reason, "case_id": str(case_id)},
            dedupe_key=dedupe_key,
        )
        await self._add_change(
            commit,
            subject_type="asset_dedup_case",
            subject_id=str(case_id),
            action=f"asset_dedup_{action}",
            before_state={"status": previous_status},
            after_state={
                "status": "merged" if action == "merge" else action,
                "representative_asset_id": (
                    str(representative_asset_id) if representative_asset_id else None
                ),
                "group_id": str(group_id) if group_id else None,
            },
            impact={
                "left_asset_id": str(left_asset_id),
                "right_asset_id": str(right_asset_id),
                "evidence": evidence,
            },
        )
        commit.stats = {
            "asset_count": 2,
            "group_count": 1 if group_id else 0,
        }
        return commit

    @staticmethod
    def _diff(before: dict, after: dict) -> dict:
        keys = sorted(set(before) | set(after))
        return {
            key: {"before": before.get(key), "after": after.get(key)}
            for key in keys
            if before.get(key) != after.get(key)
        }

    async def work_state(self, work_id: UUID) -> WorkCurationState | None:
        result = await self.db.execute(select(WorkCurationState).where(WorkCurationState.work_id == work_id))
        return result.scalar_one_or_none()

    async def creator_state(self, creator_id: UUID) -> CreatorCurationState | None:
        result = await self.db.execute(select(CreatorCurationState).where(CreatorCurationState.creator_id == creator_id))
        return result.scalar_one_or_none()

    def work_state_payload(self, state: WorkCurationState | None) -> dict:
        if not state:
            return {"visibility": VISIBLE}
        return {
            "visibility": state.visibility,
            "reason": state.reason,
            "trashed_at": _json_value(state.trashed_at),
            "purged_at": _json_value(state.purged_at),
        }

    def creator_state_payload(self, state: CreatorCurationState | None) -> dict:
        if not state:
            return {"visibility": VISIBLE}
        return {
            "visibility": state.visibility,
            "reason": state.reason,
            "archived_at": _json_value(state.archived_at),
        }

    async def _ensure_work_state(self, work_id: UUID) -> WorkCurationState:
        state = await self.work_state(work_id)
        if not state:
            state = WorkCurationState(work_id=work_id, visibility=VISIBLE)
            self.db.add(state)
            await self.db.flush()
        return state

    async def _ensure_creator_state(self, creator_id: UUID) -> CreatorCurationState:
        state = await self.creator_state(creator_id)
        if not state:
            state = CreatorCurationState(creator_id=creator_id, visibility=VISIBLE)
            self.db.add(state)
            await self.db.flush()
        return state

    def _work_snapshot(self, work: Work, state: WorkCurationState | None) -> dict:
        return {
            "id": str(work.id),
            "title": work.title,
            "description": work.description,
            "posted_at": _json_value(work.posted_at),
            "is_nsfw": work.is_nsfw,
            "is_ai_generated": work.is_ai_generated,
            "is_favorite": work.is_favorite,
            "thumbnail_asset_id": str(work.thumbnail_asset_id) if work.thumbnail_asset_id else None,
            "visibility": state.visibility if state else VISIBLE,
            "reason": state.reason if state else None,
        }

    def _creator_snapshot(self, creator: Creator, state: CreatorCurationState | None) -> dict:
        return {
            "id": str(creator.id),
            "name": creator.name,
            "display_name": creator.display_name,
            "thumbnail_url": creator.thumbnail_url,
            "is_active": creator.is_active,
            "is_favorite": creator.is_favorite,
            "visibility": state.visibility if state else VISIBLE,
            "reason": state.reason if state else None,
        }

    async def trash_works(self, work_ids: list[UUID], *, reason: str | None = None, message: str | None = None, trigger: str = "work_trash") -> CurationCommit:
        if not work_ids:
            raise HTTPException(status_code=400, detail="ids list is required")
        if len(work_ids) > self.COMMIT_WORK_LIMIT:
            raise HTTPException(
                status_code=400,
                detail=f"at most {self.COMMIT_WORK_LIMIT} works per curation commit",
            )
        rows = await self.db.execute(select(Work).where(Work.id.in_(work_ids)))
        works = list(rows.scalars().all())
        if not works:
            raise HTTPException(status_code=404, detail="No works found")

        commit = await self._create_commit(
            message=message or (f"Move {len(works)} work{'s' if len(works) != 1 else ''} to trash"),
            trigger=trigger,
            metadata={"reason": reason},
        )
        changed = 0
        for work in works:
            state = await self._ensure_work_state(work.id)
            if state.visibility in {TRASHED, PURGED}:
                continue
            before = self._work_snapshot(work, state)
            state.visibility = TRASHED
            state.trashed_at = _now()
            state.trashed_by_commit_id = commit.id
            state.reason = reason
            after = self._work_snapshot(work, state)
            await self._add_change(
                commit,
                subject_type="work",
                subject_id=str(work.id),
                action="work_trashed",
                before_state=before,
                after_state=after,
                impact={"storage_reclaimable": False},
            )
            changed += 1
        commit.stats = {"work_count": changed}
        await request_search_projection(self.db, [work.id for work in works])
        await self.db.commit()
        await self.db.refresh(commit)
        return commit

    async def restore_works(self, work_ids: list[UUID], *, reason: str | None = None, message: str | None = None, trigger: str = "work_restore") -> CurationCommit:
        if not work_ids:
            raise HTTPException(status_code=400, detail="ids list is required")
        if len(work_ids) > self.COMMIT_WORK_LIMIT:
            raise HTTPException(
                status_code=400,
                detail=f"at most {self.COMMIT_WORK_LIMIT} works per curation commit",
            )
        rows = await self.db.execute(select(Work).where(Work.id.in_(work_ids)))
        works = list(rows.scalars().all())
        if not works:
            raise HTTPException(status_code=404, detail="No works found")
        commit = await self._create_commit(
            message=message or (f"Restore {len(works)} work{'s' if len(works) != 1 else ''}"),
            trigger=trigger,
            metadata={"reason": reason},
        )
        changed = 0
        for work in works:
            state = await self._ensure_work_state(work.id)
            if state.visibility != TRASHED:
                continue
            before = self._work_snapshot(work, state)
            state.visibility = VISIBLE
            state.restored_by_commit_id = commit.id
            state.reason = reason
            after = self._work_snapshot(work, state)
            await self._add_change(
                commit,
                subject_type="work",
                subject_id=str(work.id),
                action="work_restored",
                before_state=before,
                after_state=after,
            )
            changed += 1
        commit.stats = {"work_count": changed}
        await request_search_projection(self.db, [work.id for work in works])
        await self.db.commit()
        await self.db.refresh(commit)
        return commit

    async def record_work_favorite(self, work: Work, before_favorite: bool, after_favorite: bool) -> CurationCommit:
        state = await self._ensure_work_state(work.id)
        before = self._work_snapshot(work, state)
        before["is_favorite"] = before_favorite
        after = dict(before)
        after["is_favorite"] = after_favorite
        commit = await self._create_commit(
            message=("Favorite work" if after_favorite else "Unfavorite work"),
            trigger="work_favorite",
        )
        await self._add_change(
            commit,
            subject_type="work",
            subject_id=str(work.id),
            action="work_favorited",
            before_state=before,
            after_state=after,
        )
        commit.stats = {"work_count": 1}
        return commit

    async def record_imported_work(
        self,
        work: Work,
        *,
        creator_id: UUID | None = None,
        repository_id: UUID | None = None,
        source: str | None = None,
        source_work_id: str | None = None,
    ) -> tuple[CurationCommit, str]:
        # Absence means visible. Do not create a 1:1 "visible" row for every
        # ordinary import; the sparse exception table makes list anti-joins and
        # exact counts scale with curated exceptions, not library size.
        state = await self.work_state(work.id)
        archived_creator = False
        if creator_id:
            creator_state = await self.creator_state(creator_id)
            archived_creator = bool(creator_state and creator_state.visibility == ARCHIVED)
        before = self._work_snapshot(work, state)
        if archived_creator:
            if state is None:
                state = WorkCurationState(work_id=work.id, visibility=VISIBLE)
                self.db.add(state)
                await self.db.flush()
            state.visibility = TRASHED
            state.trashed_at = _now()
            state.reason = "creator archived"
        after = self._work_snapshot(work, state)
        commit = await self._create_commit(
            message=f"Import work {work.title or work.id}",
            trigger="source_synced",
            actor_type="system",
            metadata={
                "source": source,
                "source_work_id": source_work_id,
                "creator_id": str(creator_id) if creator_id else None,
                "repository_id": str(repository_id) if repository_id else None,
            },
        )
        if archived_creator:
            state.trashed_by_commit_id = commit.id
        await self._add_change(
            commit,
            subject_type="work",
            subject_id=str(work.id),
            action="work_added" if not archived_creator else "work_trashed",
            before_state=before if archived_creator else None,
            after_state=after,
            impact={"source": source, "source_work_id": source_work_id, "archived_creator": archived_creator},
        )
        if repository_id:
            await self._add_change(
                commit,
                subject_type="repository",
                subject_id=str(repository_id),
                action="source_synced",
                before_state=None,
                after_state={
                    "repository_id": str(repository_id),
                    "work_id": str(work.id),
                    "source": source,
                    "source_work_id": source_work_id,
                },
                impact={"work_id": str(work.id), "archived_creator": archived_creator},
            )
        commit.stats = {"work_count": 1, "archived_creator": archived_creator}
        return commit, state.visibility if state else VISIBLE

    async def record_imported_works(
        self,
        records: list[dict],
        *,
        dedupe_key: str,
    ) -> tuple[CurationCommit, dict[UUID, str]]:
        """Record up to one importer micro-batch in a single commit.

        Every work keeps its own CurationChange, while the repository receives
        one aggregate source_synced change.  Ordinary visible imports do not
        create WorkCurationState rows.  The Gitllery outbox is written in this
        same transaction, so disk projection can lag safely without ever being
        lost.
        """
        if not records:
            raise ValueError("records must not be empty")
        if len(records) > 25:
            raise ValueError("an import curation batch may contain at most 25 works")

        commit = await self._create_commit(
            message=f"Import {len(records)} works",
            trigger="source_synced",
            actor_type="system",
            dedupe_key=dedupe_key,
            metadata={
                "batch_size": len(records),
                "sources": sorted(
                    {str(record.get("source")) for record in records if record.get("source")}
                ),
            },
        )
        existing_change = (
            await self.db.execute(
                select(CurationChange.id)
                .where(CurationChange.commit_id == commit.id)
                .limit(1)
            )
        ).scalar_one_or_none()
        if existing_change is not None:
            return commit, {
                record["work"].id: VISIBLE for record in records
            }

        creator_ids = {
            record.get("creator_id")
            for record in records
            if record.get("creator_id") is not None
        }
        archived_ids: set[UUID] = set()
        if creator_ids:
            archived_ids = set(
                (
                    await self.db.execute(
                        select(CreatorCurationState.creator_id).where(
                            CreatorCurationState.creator_id.in_(creator_ids),
                            CreatorCurationState.visibility == ARCHIVED,
                        )
                    )
                ).scalars()
            )

        work_ids = [record["work"].id for record in records]
        existing_states = {
            state.work_id: state
            for state in (
                await self.db.execute(
                    select(WorkCurationState).where(
                        WorkCurationState.work_id.in_(work_ids)
                    )
                )
            ).scalars()
        }

        now = _now()
        visibilities: dict[UUID, str] = {}
        changes: list[CurationChange] = []
        repository_works: dict[str, list[dict]] = {}
        archived_count = 0
        for record in records:
            work: Work = record["work"]
            archived = record.get("creator_id") in archived_ids
            state = existing_states.get(work.id)
            before = self._work_snapshot(work, None) if archived else None
            if archived:
                if state is None:
                    state = WorkCurationState(
                        work_id=work.id,
                        visibility=TRASHED,
                        trashed_at=now,
                        reason="creator archived",
                    )
                    self.db.add(state)
                state.visibility = TRASHED
                state.trashed_at = state.trashed_at or now
                state.trashed_by_commit_id = commit.id
                state.reason = state.reason or "creator archived"
                archived_count += 1
            after = self._work_snapshot(work, state)
            action = "work_trashed" if archived else "work_added"
            impact = {
                "source": record.get("source"),
                "source_work_id": record.get("source_work_id"),
                "archived_creator": archived,
            }
            changes.append(
                CurationChange(
                    commit_id=commit.id,
                    subject_type="work",
                    subject_id=str(work.id),
                    action=action,
                    before_state=_json_value(before) if before else None,
                    after_state=_json_value(after),
                    diff=self._diff(before or {}, after),
                    impact=impact,
                )
            )
            visibilities[work.id] = TRASHED if archived else VISIBLE
            repository_id = record.get("repository_id")
            if repository_id:
                repository_works.setdefault(str(repository_id), []).append(
                    {
                        "work_id": str(work.id),
                        "source": record.get("source"),
                        "source_work_id": record.get("source_work_id"),
                        "archived_creator": archived,
                    }
                )

        for repository_id, works in repository_works.items():
            after = {"repository_id": repository_id, "works": works}
            changes.append(
                CurationChange(
                    commit_id=commit.id,
                    subject_type="repository",
                    subject_id=repository_id,
                    action="source_synced",
                    before_state=None,
                    after_state=after,
                    diff=self._diff({}, after),
                    impact={"work_ids": [item["work_id"] for item in works]},
                )
            )
        self.db.add_all(changes)
        commit.stats = {
            "work_count": len(records),
            "archived_creator": archived_count,
            "repository_count": len(repository_works),
        }
        await self.db.flush()
        return commit, visibilities

    async def curate_creator(
        self,
        creator_id: UUID,
        *,
        action: str,
        reason: str | None = None,
        message: str | None = None,
        work_ids: list[UUID] | None = None,
    ) -> CurationCommit:
        creator = await self.db.get(Creator, creator_id)
        if not creator:
            raise HTTPException(status_code=404, detail="Creator not found")
        if action not in {"archive", "restore"}:
            raise HTTPException(status_code=400, detail="action must be archive or restore")

        state = await self._ensure_creator_state(creator_id)
        commit = await self._create_commit(
            message=message or (f"Archive creator {creator.display_name or creator.name}" if action == "archive" else f"Restore creator {creator.display_name or creator.name}"),
            trigger=f"creator_{action}",
            metadata={"reason": reason},
        )
        before = self._creator_snapshot(creator, state)
        if action == "archive":
            creator.is_active = False
            state.visibility = ARCHIVED
            state.archived_at = _now()
            state.archived_by_commit_id = commit.id
            state.reason = reason
        else:
            creator.is_active = True
            state.visibility = VISIBLE
            state.restored_by_commit_id = commit.id
            state.reason = reason
        after = self._creator_snapshot(creator, state)
        await self._add_change(
            commit,
            subject_type="creator",
            subject_id=str(creator.id),
            action="creator_archived" if action == "archive" else "creator_restored",
            before_state=before,
            after_state=after,
        )

        work_count = 0
        commit.stats = {"creator_count": 1, "work_count": 0}
        await request_search_projection(self.db, creator_ids=[creator.id])
        # The creator mutation is durable before bounded work commits. A retry
        # can safely continue with whichever works remain visible.
        await self.db.commit()
        await self.db.refresh(commit)
        if action == "archive":
            batches = (
                self._work_batches_for_creator(creator_id)
                if work_ids is None
                else self._work_batches_for_ids(work_ids)
            )
            async for works in batches:
                work_commit = await self._create_commit(
                    message=(
                        f"Archive works for creator "
                        f"{creator.display_name or creator.name}"
                    ),
                    trigger="creator_archive_works",
                    metadata={
                        "creator_id": str(creator_id),
                        "reason": reason,
                    },
                )
                changed_ids: list[UUID] = []
                for work in works:
                    w_state = await self._ensure_work_state(work.id)
                    if w_state.visibility != VISIBLE:
                        continue
                    before_work = self._work_snapshot(work, w_state)
                    w_state.visibility = TRASHED
                    w_state.trashed_at = _now()
                    w_state.trashed_by_commit_id = work_commit.id
                    w_state.reason = reason or "creator archived"
                    after_work = self._work_snapshot(work, w_state)
                    await self._add_change(
                        work_commit,
                        subject_type="work",
                        subject_id=str(work.id),
                        action="work_trashed",
                        before_state=before_work,
                        after_state=after_work,
                        impact={"creator_id": str(creator_id)},
                    )
                    changed_ids.append(work.id)
                work_commit.stats = {
                    "creator_count": 1,
                    "work_count": len(changed_ids),
                    "creator_id": str(creator_id),
                }
                work_count += len(changed_ids)
                if changed_ids:
                    await request_search_projection(self.db, changed_ids)
                await self.db.commit()
        commit.stats = {"creator_count": 1, "work_count": work_count}
        await self.db.commit()
        await self.db.refresh(commit)
        return commit

    async def _work_batches_for_creator(self, creator_id: UUID):
        """Yield stable creator work batches without materializing the library."""

        last_id: UUID | None = None
        while True:
            stmt = (
                select(Work)
                .join(WorkSource, WorkSource.work_id == Work.id)
                .join(
                    SourceCreator,
                    (SourceCreator.source == WorkSource.source)
                    & (
                        SourceCreator.source_creator_id
                        == WorkSource.source_creator_id
                    ),
                )
                .outerjoin(
                    WorkCurationState,
                    WorkCurationState.work_id == Work.id,
                )
                .where(
                    SourceCreator.creator_id == creator_id,
                    or_(
                        WorkCurationState.id.is_(None),
                        WorkCurationState.visibility == VISIBLE,
                    ),
                )
                .distinct()
                .order_by(Work.id)
                .limit(self.COMMIT_WORK_LIMIT)
            )
            if last_id is not None:
                stmt = stmt.where(Work.id > last_id)
            works = list((await self.db.execute(stmt)).scalars().all())
            if not works:
                return
            yield works
            last_id = works[-1].id

    async def _work_batches_for_ids(self, work_ids: list[UUID]):
        """Yield caller-scoped visible works in bounded, stable batches."""

        ordered_ids = sorted(set(work_ids), key=str)
        for index in range(0, len(ordered_ids), self.COMMIT_WORK_LIMIT):
            chunk = ordered_ids[index:index + self.COMMIT_WORK_LIMIT]
            works = list((await self.db.execute(
                select(Work)
                .outerjoin(WorkCurationState, WorkCurationState.work_id == Work.id)
                .where(
                    Work.id.in_(chunk),
                    or_(
                        WorkCurationState.id.is_(None),
                        WorkCurationState.visibility == VISIBLE,
                    ),
                )
                .order_by(Work.id)
            )).scalars().all())
            if works:
                yield works

    async def backfill_status(self) -> dict:
        # count_only: we only need the number of groups here, not their works.
        expected_groups = await self._baseline_work_groups(count_only=True)
        expected = {
            "creators": await self._count_rows(Creator),
            "repositories": await self._count_rows(SubscriptionSource),
            "work_groups": len(expected_groups),
        }
        existing = {
            "creators": await self._count_commits_like("creator:%:added"),
            "repositories": await self._count_commits_like("repository:%:added"),
            "work_groups": await self._count_commits_like("%:works:%"),
        }
        missing = {key: max(expected[key] - existing.get(key, 0), 0) for key in expected}
        return {
            "is_complete": all(value == 0 for value in missing.values()),
            "expected": expected,
            "existing": existing,
            "missing": missing,
        }

    async def run_backfill(
        self,
        *,
        resource_owner: str | None = None,
    ) -> dict:
        from app.services.heavy_io import adaptive_resource_slice

        created = {"creators": 0, "repositories": 0, "work_groups": 0}
        skipped = {"creators": 0, "repositories": 0, "work_groups": 0}

        creator_rows = await self.db.execute(select(Creator).order_by(Creator.created_at))
        for creator in creator_rows.scalars().all():
            dedupe_key = f"creator:{creator.id}:added"
            if await self._commit_exists(dedupe_key):
                skipped["creators"] += 1
                continue
            commit = await self._create_commit(
                message=f"Baseline: add creator {creator.display_name or creator.name}",
                trigger="baseline_backfill",
                actor_type="system",
                status="baseline",
                dedupe_key=dedupe_key,
                occurred_at=creator.created_at,
                metadata={"baseline": True, "entity": "creator"},
            )
            await self._add_change(
                commit,
                subject_type="creator",
                subject_id=str(creator.id),
                action="creator_added",
                before_state=None,
                after_state=self._creator_snapshot(creator, None),
            )
            commit.stats = {"creator_count": 1}
            created["creators"] += 1
            if (created["creators"] + skipped["creators"]) % 25 == 0:
                await self.db.commit()

        repo_rows = await self.db.execute(select(SubscriptionSource).order_by(SubscriptionSource.created_at))
        for repo in repo_rows.scalars().all():
            dedupe_key = f"repository:{repo.id}:added"
            if await self._commit_exists(dedupe_key):
                skipped["repositories"] += 1
                continue
            commit = await self._create_commit(
                message=f"Baseline: add repository {repo.source}/{repo.source_creator_id or repo.id}",
                trigger="baseline_backfill",
                actor_type="system",
                status="baseline",
                dedupe_key=dedupe_key,
                occurred_at=repo.created_at,
                metadata={"baseline": True, "entity": "repository"},
            )
            await self._add_change(
                commit,
                subject_type="repository",
                subject_id=str(repo.id),
                action="repository_added",
                before_state=None,
                after_state={
                    "id": str(repo.id),
                    "subscription_id": str(repo.subscription_id),
                    "source": repo.source,
                    "source_creator_id": repo.source_creator_id,
                    "source_url": repo.source_url,
                    "is_enabled": repo.is_enabled,
                },
            )
            commit.stats = {"repository_count": 1}
            created["repositories"] += 1
            if (created["repositories"] + skipped["repositories"]) % 25 == 0:
                await self.db.commit()

        legacy_complete_groups: set[str] = set()
        async for group in self._iter_baseline_work_groups():
            dedupe_key = group["dedupe_key"]
            base_dedupe_key = group["base_dedupe_key"]
            if base_dedupe_key in legacy_complete_groups:
                skipped["work_groups"] += 1
                continue
            existing_commit = await self._commit_for_key(dedupe_key)
            if existing_commit is not None:
                if (
                    group["chunk_index"] == 0
                    and not (existing_commit.extra_metadata or {}).get(
                        "baseline_chunked"
                    )
                ):
                    legacy_complete_groups.add(base_dedupe_key)
                skipped["work_groups"] += 1
                continue
            works = group["works"]
            async with adaptive_resource_slice(
                "import_db",
                resource_owner or "curation-baseline-backfill",
                max_work_units=len(works),
                max_slice_seconds=20.0,
            ):
                commit = await self._create_commit(
                    message=f"Baseline: add {len(works)} works on {group['day']}",
                    trigger="baseline_backfill",
                    actor_type="system",
                    status="baseline",
                    dedupe_key=dedupe_key,
                    occurred_at=group["occurred_at"],
                    metadata={
                        "baseline": True,
                        "entity": "work_group",
                        "repository_id": str(group["repository_id"]) if group["repository_id"] else None,
                        "source": group["source"],
                        "source_creator_id": group["source_creator_id"],
                        "day": group["day"],
                        "baseline_chunked": True,
                        "chunk_index": group["chunk_index"],
                    },
                )
                for item in works:
                    await self._add_change(
                        commit,
                        subject_type="work",
                        subject_id=item["work_id"],
                        action="work_added",
                        before_state=None,
                        after_state={
                            **item["snapshot"],
                            "source": item["source"],
                            "source_work_id": item["source_work_id"],
                            "source_creator_id": item["source_creator_id"],
                        },
                        impact={
                            "repository_id": str(group["repository_id"]) if group["repository_id"] else None,
                            "source": item["source"],
                            "source_work_id": item["source_work_id"],
                        },
                    )
                if group["repository_id"]:
                    await self._add_change(
                        commit,
                        subject_type="repository",
                        subject_id=str(group["repository_id"]),
                        action="source_synced",
                        before_state=None,
                        after_state={
                            "repository_id": str(group["repository_id"]),
                            "work_count": len(works),
                            "day": group["day"],
                        },
                        impact={"work_count": len(works), "baseline": True},
                    )
                commit.stats = {"work_count": len(works), "baseline": True}
                created["work_groups"] += 1
                await self.db.commit()

        await self.db.commit()
        return {"status": "ok", "created": created, "skipped": skipped, "expected": (await self.backfill_status())["expected"]}

    async def _count_rows(self, model) -> int:
        result = await self.db.execute(select(func.count(model.id)))
        return result.scalar() or 0

    async def _commit_exists(self, dedupe_key: str) -> bool:
        result = await self.db.execute(select(CurationCommit.id).where(CurationCommit.dedupe_key == dedupe_key).limit(1))
        return result.scalar_one_or_none() is not None

    async def _commit_for_key(self, dedupe_key: str) -> CurationCommit | None:
        result = await self.db.execute(
            select(CurationCommit)
            .where(CurationCommit.dedupe_key == dedupe_key)
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def _count_commits_like(self, pattern: str) -> int:
        result = await self.db.execute(
            select(func.count(CurationCommit.id)).where(CurationCommit.dedupe_key.like(pattern))
        )
        return result.scalar() or 0

    async def _repository_lookup(self) -> dict[tuple[str, str], SubscriptionSource]:
        rows = await self.db.execute(
            select(SubscriptionSource)
            .where(SubscriptionSource.source_creator_id.isnot(None))
            .order_by(SubscriptionSource.created_at)
        )
        lookup: dict[tuple[str, str], SubscriptionSource] = {}
        for repo in rows.scalars().all():
            if repo.source_creator_id:
                lookup.setdefault((repo.source, repo.source_creator_id), repo)
        return lookup

    async def _baseline_work_groups(self, count_only: bool = False) -> list[dict]:
        # Streamed with a server-side cursor and, for count_only callers
        # (backfill_status), the per-group work lists are not retained — the
        # old .all() materialized every Work+WorkSource ORM object at once,
        # O(library) resident memory on the event loop.
        repo_lookup = await self._repository_lookup()
        result = await self.db.stream(
            select(Work, WorkSource)
            .join(WorkSource, WorkSource.work_id == Work.id)
            .order_by(Work.created_at, WorkSource.created_at)
        )
        groups: dict[str, dict] = {}
        seen_works: set[UUID] = set()
        async for work, work_source in result:
            if work.id in seen_works:
                continue
            seen_works.add(work.id)
            occurred_at = _parse_datetime(work_source.posted_at) or _parse_datetime(work.posted_at) or work.created_at
            if occurred_at and occurred_at.tzinfo is None:
                occurred_at = occurred_at.replace(tzinfo=timezone.utc)
            repo = repo_lookup.get((work_source.source, work_source.source_creator_id or ""))
            day = _day_key(occurred_at)
            if repo:
                dedupe_key = f"repository:{repo.id}:works:{day}"
                group_key = dedupe_key
                repository_id = repo.id
            else:
                source_creator_id = work_source.source_creator_id or "unknown"
                dedupe_key = f"source:{work_source.source}:{source_creator_id}:works:{day}"
                group_key = dedupe_key
                repository_id = None
            group = groups.setdefault(group_key, {
                "dedupe_key": dedupe_key,
                "repository_id": repository_id,
                "source": work_source.source,
                "source_creator_id": work_source.source_creator_id,
                "day": day,
                "occurred_at": occurred_at,
                "works": [],
            })
            if occurred_at < group["occurred_at"]:
                group["occurred_at"] = occurred_at
            if not count_only:
                # Lightweight dict, not ORM objects: holding every Work +
                # WorkSource instance made the group list O(library) resident
                # memory. The snapshot is computed here while the row is in hand.
                group["works"].append({
                    "work_id": str(work.id),
                    "snapshot": self._work_snapshot(work, None),
                    "source": work_source.source,
                    "source_work_id": work_source.source_work_id,
                    "source_creator_id": work_source.source_creator_id,
                })
        return sorted(groups.values(), key=lambda g: (g["occurred_at"], g["dedupe_key"]))

    async def _iter_baseline_work_groups(self):
        """Stream deterministic repository/day chunks of at most 25 works."""

        from app.database import async_session

        repo_lookup = await self._repository_lookup()
        await self.db.commit()
        primary_source = (
            select(WorkSource.id.label("work_source_id"))
            .distinct(WorkSource.work_id)
            .order_by(
                WorkSource.work_id,
                WorkSource.created_at,
                WorkSource.id,
            )
            .subquery()
        )
        day_expr = func.date(
            func.coalesce(
                WorkSource.posted_at,
                Work.posted_at,
                Work.created_at,
            )
        )
        stmt = (
            select(Work, WorkSource)
            .select_from(WorkSource)
            .join(
                primary_source,
                primary_source.c.work_source_id == WorkSource.id,
            )
            .join(Work, Work.id == WorkSource.work_id)
            .order_by(
                WorkSource.source,
                func.coalesce(WorkSource.source_creator_id, ""),
                day_expr,
                Work.id,
            )
            .execution_options(yield_per=100)
        )

        current_base: str | None = None
        current: dict | None = None
        chunk_index = 0

        async def emit_current():
            nonlocal current
            if current is None or not current["works"]:
                return None
            payload = current
            current = None
            return payload

        async with async_session() as read_db:
            rows = await read_db.stream(stmt)
            async for work, work_source in rows:
                occurred_at = (
                    work_source.posted_at
                    or work.posted_at
                    or work.created_at
                )
                if occurred_at.tzinfo is None:
                    occurred_at = occurred_at.replace(tzinfo=timezone.utc)
                repo = repo_lookup.get(
                    (
                        work_source.source,
                        work_source.source_creator_id or "",
                    )
                )
                day = _day_key(occurred_at)
                if repo:
                    base_key = f"repository:{repo.id}:works:{day}"
                    repository_id = repo.id
                else:
                    source_creator_id = (
                        work_source.source_creator_id or "unknown"
                    )
                    base_key = (
                        f"source:{work_source.source}:"
                        f"{source_creator_id}:works:{day}"
                    )
                    repository_id = None

                if current_base != base_key:
                    payload = await emit_current()
                    if payload is not None:
                        yield payload
                    current_base = base_key
                    chunk_index = 0
                if current is None:
                    dedupe_key = (
                        base_key
                        if chunk_index == 0
                        else f"{base_key}:chunk:{chunk_index}"
                    )
                    current = {
                        "base_dedupe_key": base_key,
                        "dedupe_key": dedupe_key,
                        "chunk_index": chunk_index,
                        "repository_id": repository_id,
                        "source": work_source.source,
                        "source_creator_id": work_source.source_creator_id,
                        "day": day,
                        "occurred_at": occurred_at,
                        "works": [],
                    }
                current["occurred_at"] = min(
                    current["occurred_at"],
                    occurred_at,
                )
                current["works"].append(
                    {
                        "work_id": str(work.id),
                        "snapshot": self._work_snapshot(work, None),
                        "source": work_source.source,
                        "source_work_id": work_source.source_work_id,
                        "source_creator_id": work_source.source_creator_id,
                    }
                )
                if len(current["works"]) >= self.COMMIT_WORK_LIMIT:
                    payload = await emit_current()
                    if payload is not None:
                        yield payload
                    chunk_index += 1

            payload = await emit_current()
            if payload is not None:
                yield payload

    async def purge_preview(self, work_ids: list[UUID] | None = None) -> dict:
        works = await self._purge_candidate_works(work_ids)
        assets = await self._purge_candidate_assets([w.id for w in works])
        return {
            "work_count": len(works),
            "asset_count": len(assets),
            "bytes_reclaimable": await self._purge_reclaimable_bytes(assets),
            "works": [
                {
                    "id": str(w.id),
                    "title": w.title,
                    "thumbnail_asset_id": str(w.thumbnail_asset_id) if w.thumbnail_asset_id else None,
                }
                for w in works
            ],
            "assets": [{"id": str(a.id), "file_name": a.file_name, "file_size": a.file_size or 0} for a in assets],
        }

    async def purge(
        self,
        work_ids: list[UUID] | None = None,
        *,
        message: str | None = None,
        strict_file_cleanup: bool = False,
        asset_scope_work_ids: list[UUID] | None = None,
    ) -> CurationCommit:
        if not work_ids:
            raise HTTPException(
                status_code=400,
                detail="purge requires an explicit list of 1 to 25 work ids",
            )
        if len(work_ids) > self.COMMIT_WORK_LIMIT:
            raise HTTPException(
                status_code=400,
                detail=f"at most {self.COMMIT_WORK_LIMIT} works per purge commit",
            )
        works = await self._purge_candidate_works(work_ids)
        if not works:
            raise HTTPException(status_code=400, detail="No trashed works are eligible for purge")
        assets = await self._purge_candidate_assets(
            [w.id for w in works],
            ownership_work_ids=asset_scope_work_ids,
        )
        commit = await self._create_commit(
            message=message or f"Purge {len(works)} trashed work{'s' if len(works) != 1 else ''}",
            trigger="work_purge",
        )
        bytes_reclaimed = 0
        for work in works:
            state = await self._ensure_work_state(work.id)
            before = self._work_snapshot(work, state)
            state.visibility = PURGED
            state.purged_at = _now()
            state.purged_by_commit_id = commit.id
            after = self._work_snapshot(work, state)
            await self._add_change(
                commit,
                subject_type="work",
                subject_id=str(work.id),
                action="work_purged",
                before_state=before,
                after_state=after,
            )
        for asset in assets:
            before = {"id": str(asset.id), "file_name": asset.file_name, "storage_state": "available"}
            storage_state = await self._ensure_asset_state(asset.id)
            reclaimed, missing = await self._delete_asset_files(
                asset,
                storage_state,
                strict=strict_file_cleanup,
            )
            bytes_reclaimed += reclaimed
            storage_state.storage_state = PURGED
            storage_state.purged_at = _now()
            storage_state.purged_by_commit_id = commit.id
            storage_state.bytes_reclaimed = max(
                storage_state.bytes_reclaimed,
                reclaimed,
            )
            storage_state.missing_files = missing
            storage_state.quarantine_path = None
            storage_state.quarantine_sha256 = None
            after = {"id": str(asset.id), "file_name": asset.file_name, "storage_state": PURGED, "bytes_reclaimed": reclaimed}
            await self._add_change(
                commit,
                subject_type="asset",
                subject_id=str(asset.id),
                action="asset_purged",
                before_state=before,
                after_state=after,
                impact={"bytes_reclaimed": reclaimed, "missing_files": missing},
            )
        commit.stats = {"work_count": len(works), "asset_count": len(assets), "bytes_reclaimed": bytes_reclaimed}
        # Purged works remain database rows with a non-visible projection; an
        # upsert keeps identity audits exact while visibility filters hide them.
        await request_search_projection(self.db, [work.id for work in works])
        await self.db.commit()
        await self.db.refresh(commit)
        return commit

    async def _purge_candidate_works(self, work_ids: list[UUID] | None) -> list[Work]:
        stmt = select(Work).join(WorkCurationState, WorkCurationState.work_id == Work.id).where(WorkCurationState.visibility == TRASHED)
        if work_ids:
            stmt = stmt.where(Work.id.in_(work_ids))
        else:
            # The management page polls this endpoint. Keep the preview and
            # subsequent purge on the same bounded 25-work unit instead of
            # scanning every trashed work every 30 seconds.
            stmt = stmt.limit(self.COMMIT_WORK_LIMIT)
        rows = await self.db.execute(stmt.order_by(Work.updated_at.desc()))
        return list(rows.scalars().all())

    async def _purge_candidate_assets(
        self,
        work_ids: list[UUID],
        *,
        ownership_work_ids: list[UUID] | None = None,
    ) -> list[Asset]:
        if not work_ids:
            return []
        ownership_ids = set(ownership_work_ids or work_ids)
        candidate_ids = set((await self.db.execute(
            select(AssetSource.asset_id)
            .join(WorkSource, WorkSource.id == AssetSource.work_source_id)
            .where(WorkSource.work_id.in_(work_ids))
            .distinct()
        )).scalars().all())
        candidate_ids.update((await self.db.execute(
            select(Work.thumbnail_asset_id).where(
                Work.id.in_(work_ids),
                Work.thumbnail_asset_id.is_not(None),
            )
        )).scalars().all())
        if not candidate_ids:
            return []
        purged_asset_ids = set((await self.db.execute(
            select(AssetStorageState.asset_id).where(
                AssetStorageState.asset_id.in_(candidate_ids),
                AssetStorageState.storage_state == PURGED,
            )
        )).scalars().all())
        candidate_ids.difference_update(purged_asset_ids)
        if not candidate_ids:
            return []
        candidates = list((await self.db.execute(
            select(Asset).where(Asset.id.in_(candidate_ids))
        )).scalars().all())
        eligible: list[Asset] = []
        for asset in candidates:
            outside_refs = await self.db.execute(
                select(func.count(WorkSource.work_id))
                .select_from(AssetSource)
                .join(WorkSource, WorkSource.id == AssetSource.work_source_id)
                .where(AssetSource.asset_id == asset.id)
                .where(~WorkSource.work_id.in_(ownership_ids))
            )
            outside_thumbnail_refs = await self.db.execute(
                select(func.count(Work.id)).where(
                    Work.thumbnail_asset_id == asset.id,
                    ~Work.id.in_(ownership_ids),
                )
            )
            if (
                (outside_refs.scalar() or 0) == 0
                and (outside_thumbnail_refs.scalar() or 0) == 0
            ):
                representative_group_id = (
                    await self.db.execute(
                        select(VisualAssetGroup.id).where(
                            VisualAssetGroup.representative_asset_id == asset.id
                        )
                    )
                ).scalar_one_or_none()
                if representative_group_id:
                    group_outside_refs = (
                        await self.db.execute(
                            select(func.count(func.distinct(WorkSource.work_id)))
                            .select_from(VisualAssetMember)
                            .join(
                                AssetSource,
                                AssetSource.asset_id
                                == VisualAssetMember.asset_id,
                            )
                            .join(
                                WorkSource,
                                WorkSource.id == AssetSource.work_source_id,
                            )
                            .where(
                                VisualAssetMember.group_id
                                == representative_group_id,
                                ~WorkSource.work_id.in_(ownership_ids),
                            )
                        )
                    ).scalar_one()
                    if group_outside_refs:
                        continue
                eligible.append(asset)
        return eligible

    async def _ensure_asset_state(self, asset_id: UUID) -> AssetStorageState:
        result = await self.db.execute(select(AssetStorageState).where(AssetStorageState.asset_id == asset_id))
        state = result.scalar_one_or_none()
        if not state:
            state = AssetStorageState(asset_id=asset_id, storage_state="available", bytes_reclaimed=0)
            self.db.add(state)
            await self.db.flush()
        return state

    @staticmethod
    def _asset_file_roots(
        asset: Asset,
        storage_state: AssetStorageState | None,
    ) -> list[tuple[Path, str | None]]:
        roots = [
            (Path(settings.download_root), asset.file_path),
            (Path(settings.library_root), asset.thumb_sm_path),
            (Path(settings.library_root), asset.thumb_md_path),
            (Path(settings.library_root), asset.thumb_lg_path),
        ]
        if storage_state and storage_state.quarantine_path:
            roots.append(
                (Path(settings.download_root), storage_state.quarantine_path)
            )
        return roots

    async def _purge_reclaimable_bytes(self, assets: list[Asset]) -> int:
        if not assets:
            return 0
        states = {
            state.asset_id: state
            for state in (
                (
                    await self.db.execute(
                        select(AssetStorageState).where(
                            AssetStorageState.asset_id.in_(
                                [asset.id for asset in assets]
                            )
                        )
                    )
                ).scalars()
            )
        }
        inodes: dict[tuple[int, int], dict] = {}
        for asset in assets:
            for root, relative in self._asset_file_roots(
                asset,
                states.get(asset.id),
            ):
                path = _safe_path(root, relative)
                if not path or not path.exists():
                    continue
                try:
                    stat = path.stat()
                except OSError:
                    continue
                entry = inodes.setdefault(
                    (stat.st_dev, stat.st_ino),
                    {
                        "size": stat.st_size,
                        "link_count": stat.st_nlink,
                        "paths": set(),
                    },
                )
                entry["paths"].add(str(path))
        return sum(
            entry["size"]
            for entry in inodes.values()
            if len(entry["paths"]) >= entry["link_count"]
        )

    async def _delete_asset_files(
        self,
        asset: Asset,
        storage_state: AssetStorageState | None = None,
        *,
        strict: bool = False,
    ) -> tuple[int, list[str]]:
        reclaimed = 0
        missing: list[str] = []
        seen_paths: set[Path] = set()
        for root, relative in self._asset_file_roots(asset, storage_state):
            path = _safe_path(root, relative)
            if not path:
                if relative:
                    missing.append(relative)
                    if strict:
                        raise RuntimeError(
                            f"Refusing to delete asset path outside managed storage: {relative}"
                        )
                continue
            if not path.exists():
                missing.append(relative or str(path))
                continue
            if path in seen_paths:
                continue
            seen_paths.add(path)
            try:
                stat = path.stat()
                if stat.st_nlink <= 1:
                    reclaimed += stat.st_size
                path.unlink()
            except OSError as exc:
                missing.append(relative or str(path))
                if strict:
                    raise RuntimeError(
                        f"Unable to delete managed asset file: {relative or path}"
                    ) from exc
        return reclaimed, missing

    async def revert_commit(self, commit_id: UUID) -> dict:
        target = await self.db.get(CurationCommit, commit_id)
        if not target:
            raise HTTPException(status_code=404, detail="Commit not found")
        if self._is_baseline_commit(target):
            raise HTTPException(status_code=409, detail="Baseline commits cannot be reverted")
        existing = await self.db.execute(
            select(CurationCommit).where(CurationCommit.reverts_commit_id == commit_id, CurationCommit.status == "active")
        )
        if existing.scalar_one_or_none():
            raise HTTPException(status_code=409, detail="Commit has already been reverted")
        change_count = (
            await self.db.execute(
                select(func.count(CurationChange.id)).where(
                    CurationChange.commit_id == commit_id
                )
            )
        ).scalar_one()
        if int(change_count or 0) > self.COMMIT_WORK_LIMIT:
            raise HTTPException(
                status_code=409,
                detail=(
                    "This historical commit is too large for an online revert; "
                    "use the staged maintenance workflow"
                ),
            )
        changes = await self._changes_for_commit(commit_id)
        revert = await self._create_commit(
            message=f"Revert: {target.message}",
            trigger="commit_revert",
            reverts_commit_id=target.id,
            metadata={"target_commit_id": str(target.id)},
        )
        reverted = 0
        skipped = 0
        conflicts: list[dict] = []
        for change in reversed(changes):
            result = await self._revert_change(revert, change)
            if result == "reverted":
                reverted += 1
            elif result == "conflict":
                skipped += 1
                conflicts.append({"change_id": str(change.id), "action": change.action, "reason": "not safely revertible"})
            else:
                skipped += 1
        target.status = "reverted" if skipped == 0 else "partial_reverted"
        revert.stats = {"reverted": reverted, "skipped": skipped}
        reverted_work_ids = [
            UUID(change.subject_id)
            for change in changes
            if change.subject_type == "work"
        ]
        if reverted_work_ids:
            await request_search_projection(self.db, reverted_work_ids)
        await self.db.commit()
        await self.db.refresh(revert)
        return {"status": "ok" if skipped == 0 else "partial", "commit": await self.commit_payload(revert.id), "reverted": reverted, "skipped": skipped, "conflicts": conflicts}

    async def _revert_change(self, revert_commit: CurationCommit, change: CurationChange) -> str:
        before = change.before_state or {}
        if change.action == "work_trashed":
            work = await self.db.get(Work, UUID(change.subject_id))
            if not work:
                return "skipped"
            state = await self._ensure_work_state(work.id)
            if state.visibility == PURGED:
                return "conflict"
            current = self._work_snapshot(work, state)
            state.visibility = VISIBLE
            state.restored_by_commit_id = revert_commit.id
            state.reason = "reverted"
            await self._add_change(revert_commit, subject_type="work", subject_id=str(work.id), action="work_restored", before_state=current, after_state=self._work_snapshot(work, state))
            return "reverted"
        if change.action == "work_restored":
            work = await self.db.get(Work, UUID(change.subject_id))
            if not work:
                return "skipped"
            state = await self._ensure_work_state(work.id)
            if state.visibility == PURGED:
                return "conflict"
            current = self._work_snapshot(work, state)
            state.visibility = TRASHED
            state.trashed_at = _now()
            state.trashed_by_commit_id = revert_commit.id
            state.reason = "reverted"
            await self._add_change(revert_commit, subject_type="work", subject_id=str(work.id), action="work_trashed", before_state=current, after_state=self._work_snapshot(work, state))
            return "reverted"
        if change.action == "work_favorited":
            work = await self.db.get(Work, UUID(change.subject_id))
            if not work:
                return "skipped"
            current = {"id": str(work.id), "is_favorite": work.is_favorite}
            work.is_favorite = bool(before.get("is_favorite"))
            await self._add_change(revert_commit, subject_type="work", subject_id=str(work.id), action="work_favorited", before_state=current, after_state={"id": str(work.id), "is_favorite": work.is_favorite})
            return "reverted"
        if change.action == "creator_archived":
            creator = await self.db.get(Creator, UUID(change.subject_id))
            if not creator:
                return "skipped"
            state = await self._ensure_creator_state(creator.id)
            current = self._creator_snapshot(creator, state)
            creator.is_active = True
            state.visibility = VISIBLE
            state.restored_by_commit_id = revert_commit.id
            state.reason = "reverted"
            await self._add_change(revert_commit, subject_type="creator", subject_id=str(creator.id), action="creator_restored", before_state=current, after_state=self._creator_snapshot(creator, state))
            return "reverted"
        if change.action in {"work_purged", "asset_purged"}:
            return "conflict"
        return "skipped"

    def _is_baseline_commit(self, commit: CurationCommit) -> bool:
        return commit.status == "baseline" or bool((commit.extra_metadata or {}).get("baseline"))

    async def repository_graph(
        self,
        repository_id: UUID,
        *,
        offset: int = 0,
        limit: int = 100,
        trigger: str | None = None,
        include_baseline: bool = True,
    ) -> dict:
        filters = [
            CurationChange.subject_type == "repository",
            CurationChange.subject_id == str(repository_id),
        ]
        if trigger:
            filters.append(CurationCommit.trigger == trigger)
        if not include_baseline:
            filters.append(CurationCommit.status != "baseline")

        count_result = await self.db.execute(
            select(func.count(func.distinct(CurationCommit.id)))
            .join(CurationChange, CurationChange.commit_id == CurationCommit.id)
            .where(*filters)
        )
        total = count_result.scalar() or 0
        rows = await self.db.execute(
            select(CurationCommit)
            .join(CurationChange, CurationChange.commit_id == CurationCommit.id)
            .where(*filters)
            .order_by(CurationCommit.occurred_at.desc(), CurationCommit.created_at.desc())
            .offset(offset)
            .limit(limit)
            .distinct()
        )
        commits = list(rows.scalars().all())
        commits.sort(key=lambda c: (c.occurred_at, c.created_at))
        nodes = []
        for commit in commits:
            changes = await self._changes_for_commit(commit.id)
            thumbnails = []
            summary: dict[str, int] = {}
            for change in changes:
                summary[change.action] = summary.get(change.action, 0) + 1
                after = change.after_state or {}
                thumb = after.get("thumbnail_asset_id")
                if thumb and thumb not in thumbnails:
                    thumbnails.append(str(thumb))
            nodes.append({
                "id": commit.id,
                "message": commit.message,
                "trigger": commit.trigger,
                "occurred_at": commit.occurred_at,
                "status": commit.status,
                "is_baseline": self._is_baseline_commit(commit),
                "stats": commit.stats,
                "thumbnails": thumbnails[:8],
                "changes_summary": [{"action": action, "count": count} for action, count in sorted(summary.items())],
            })
        edges = [
            {"from_id": nodes[idx]["id"], "to_id": nodes[idx + 1]["id"]}
            for idx in range(max(len(nodes) - 1, 0))
        ]
        return {"repository_id": repository_id, "nodes": nodes, "edges": edges, "total": total, "offset": offset, "limit": limit}

    async def list_commits(
        self,
        *,
        offset: int = 0,
        limit: int = 50,
        subject_type: str | None = None,
        subject_id: str | None = None,
        trigger: str | None = None,
        include_baseline: bool = True,
    ) -> dict:
        stmt = select(CurationCommit)
        count_stmt = select(func.count(func.distinct(CurationCommit.id)))
        if subject_type or subject_id:
            stmt = stmt.join(CurationChange, CurationChange.commit_id == CurationCommit.id)
            count_stmt = count_stmt.join(CurationChange, CurationChange.commit_id == CurationCommit.id)
            if subject_type:
                stmt = stmt.where(CurationChange.subject_type == subject_type)
                count_stmt = count_stmt.where(CurationChange.subject_type == subject_type)
            if subject_id:
                stmt = stmt.where(CurationChange.subject_id == subject_id)
                count_stmt = count_stmt.where(CurationChange.subject_id == subject_id)
        if trigger:
            stmt = stmt.where(CurationCommit.trigger == trigger)
            count_stmt = count_stmt.where(CurationCommit.trigger == trigger)
        if not include_baseline:
            stmt = stmt.where(CurationCommit.status != "baseline")
            count_stmt = count_stmt.where(CurationCommit.status != "baseline")
        stmt = stmt.order_by(CurationCommit.occurred_at.desc(), CurationCommit.created_at.desc()).offset(offset).limit(limit).distinct()
        rows = await self.db.execute(stmt)
        commits = list(rows.scalars().all())
        total_rows = await self.db.execute(count_stmt)
        return {"items": [await self.commit_payload(c.id) for c in commits], "total": total_rows.scalar() or 0}

    async def commit_payload(self, commit_id: UUID) -> CurationCommitRead:
        commit = await self.db.get(CurationCommit, commit_id)
        if not commit:
            raise HTTPException(status_code=404, detail="Commit not found")
        changes = await self._changes_for_commit(commit_id)
        return CurationCommitRead(
            id=commit.id,
            parent_commit_id=commit.parent_commit_id,
            actor_type=commit.actor_type,
            actor_id=commit.actor_id,
            message=commit.message,
            trigger=commit.trigger,
            dedupe_key=commit.dedupe_key,
            occurred_at=commit.occurred_at,
            reverts_commit_id=commit.reverts_commit_id,
            status=commit.status,
            stats=commit.stats,
            metadata=commit.extra_metadata,
            is_baseline=self._is_baseline_commit(commit),
            revertible=(not self._is_baseline_commit(commit)) and commit.status == "active" and commit.reverts_commit_id is None,
            created_at=commit.created_at,
            updated_at=commit.updated_at,
            changes=[CurationChangeRead.model_validate(c, from_attributes=True) for c in changes],
        )

    async def _changes_for_commit(self, commit_id: UUID) -> list[CurationChange]:
        rows = await self.db.execute(
            select(CurationChange)
            .where(CurationChange.commit_id == commit_id)
            .order_by(
                CurationChange.sequence.asc().nulls_last(),
                CurationChange.created_at,
                CurationChange.id,
            )
        )
        return list(rows.scalars().all())

    async def rule_suggestions(self) -> list[dict]:
        rows = await self.db.execute(
            select(func.count(CurationChange.id)).where(CurationChange.action == "work_trashed")
        )
        trashed = rows.scalar() or 0
        if trashed < 5:
            return []
        return [{
            "id": "trash-patterns",
            "title": "Review repeated trash patterns",
            "description": f"{trashed} works have been moved to trash. Review history before converting this into an automatic rule.",
            "confidence": 0.35,
            "impact": {"trashed_work_count": trashed},
        }]

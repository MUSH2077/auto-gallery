"""Asset-level visual reconciliation.

Works and WorkSources remain independent.  This module only groups downloaded
Asset variants that represent the same visual image, preserves every
AssetSource occurrence, and selects one representative for media delivery.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import and_, delete, exists, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.config import settings
from app.models import (
    Asset,
    AssetDedupCase,
    AssetDedupDecision,
    AssetDedupEvidence,
    AssetDedupOutbox,
    AssetSource,
    AssetStorageState,
    SourceCreator,
    VisualAssetGroup,
    VisualAssetMember,
    Work,
    WorkSource,
)
from app.services.curation import CurationService
from app.services.asset_dedup_scope import (
    CrossSourceDedupScope,
    canonical_source,
    scope_error_message,
)
from app.services.media_signing import signed_media_url
from app.services.settings import extractor_key_for_source


ALGORITHM_VERSION = "asset-visual-v2"
QUALITY_POLICY_VERSION = "asset-quality-v2"
PHASH_MAX_DISTANCE = 4
ASPECT_RATIO_MAX_DELTA = 0.01
SSIM_MIN_SCORE = 0.98
AUTO_MERGE_SCORE = 95.0
REVIEW_MIN_SCORE = 70.0
QUARANTINE_DAYS = 30
MAX_CANDIDATES_PER_ASSET = 250

IMAGE_MIME_TYPES = {
    "image/jpeg",
    "image/png",
    "image/gif",
    "image/webp",
    "image/bmp",
}


class StaleDedupCase(ValueError):
    pass


class DedupIdempotencyConflict(ValueError):
    pass


@dataclass(frozen=True)
class EvaluationResult:
    evidence_id: UUID
    case_id: UUID
    status: str
    case_created: bool
    auto_merged: bool
    storage_actions: int
    bytes_reclaimable: int


@dataclass(frozen=True)
class DedupPolicy:
    auto_group_enabled: bool = True
    phash_threshold: int = PHASH_MAX_DISTANCE
    ssim_threshold: float = SSIM_MIN_SCORE
    aspect_ratio_tolerance: float = ASPECT_RATIO_MAX_DELTA
    auto_group_score: float = AUTO_MERGE_SCORE
    review_score: float = REVIEW_MIN_SCORE
    quarantine_days: int = QUARANTINE_DAYS


def _ordered_pair(left: UUID, right: UUID) -> tuple[UUID, UUID]:
    return (left, right) if left.int < right.int else (right, left)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _advisory_lock_key(namespace: str, value: str) -> int:
    digest = hashlib.sha256(f"{namespace}:{value}".encode()).digest()
    return int.from_bytes(digest[:8], byteorder="big", signed=True)


def _safe_download_path(relative: str) -> Path:
    root = Path(settings.download_root).resolve()
    full = (root / relative).resolve()
    try:
        full.relative_to(root)
    except ValueError as exc:
        raise ValueError("Asset path escapes DOWNLOAD_ROOT") from exc
    return full


def phash_distance(left: str | None, right: str | None) -> int | None:
    if not left or not right:
        return None
    try:
        return (int(left, 16) ^ int(right, 16)).bit_count()
    except ValueError:
        return None


def versioned_phash_distance(
    left: str | None,
    right: str | None,
    left_version: str | None,
    right_version: str | None,
) -> int | None:
    """Compare pHashes only when both were produced by the same algorithm.

    ``None`` is the legacy algorithm version.  Two legacy hashes remain
    comparable, while a legacy hash is never compared with a versioned one.
    """

    if left_version != right_version:
        return None
    return phash_distance(left, right)


def aspect_ratio_delta(left: Asset, right: Asset) -> float | None:
    if not left.width or not left.height or not right.width or not right.height:
        return None
    left_ratio = left.width / left.height
    right_ratio = right.width / right.height
    return abs(left_ratio - right_ratio) / max(left_ratio, right_ratio)


def metadata_time_bonus(min_delta_hours: float | None) -> float:
    if min_delta_hours is None:
        return 0.0
    if min_delta_hours <= 24:
        return 4.0
    if min_delta_hours <= 24 * 7:
        return 2.0
    if min_delta_hours <= 24 * 30:
        return 1.0
    return 0.0


def _normalized_pixels(path: Path):
    import numpy as np
    from PIL import Image, ImageOps

    with Image.open(path) as opened:
        image = ImageOps.exif_transpose(opened).convert("RGB")
        image = image.resize((256, 256), Image.Resampling.LANCZOS)
        return np.asarray(image, dtype=np.float64) / 255.0


def structural_similarity(left_path: Path, right_path: Path) -> float:
    """Return a deterministic global RGB SSIM score in [-1, 1].

    Candidate blocking already enforces a close aspect ratio and pHash.  The
    SSIM pass is a conservative second signal for resize/compression variants,
    not a general semantic-image matcher.
    """
    import numpy as np

    left = _normalized_pixels(left_path)
    right = _normalized_pixels(right_path)
    c1 = 0.01**2
    c2 = 0.03**2
    scores: list[float] = []
    for channel in range(3):
        x = left[:, :, channel]
        y = right[:, :, channel]
        mean_x = float(x.mean())
        mean_y = float(y.mean())
        var_x = float(x.var())
        var_y = float(y.var())
        covariance = float(((x - mean_x) * (y - mean_y)).mean())
        numerator = (2 * mean_x * mean_y + c1) * (2 * covariance + c2)
        denominator = (mean_x**2 + mean_y**2 + c1) * (var_x + var_y + c2)
        scores.append(numerator / denominator if denominator else 1.0)
    return max(-1.0, min(1.0, float(np.mean(scores))))


def quality_facts(asset: Asset) -> dict[str, Any]:
    name = asset.file_name.lower()
    is_preview = any(marker in name for marker in ("thumb", "preview", "sample"))
    pixel_area = int(asset.width or 0) * int(asset.height or 0)
    lossless = asset.mime_type in {"image/png", "image/bmp"}
    return {
        "source_original": not is_preview,
        "pixel_area": pixel_area,
        "lossless": lossless,
        "file_size": int(asset.file_size or 0),
        "width": asset.width,
        "height": asset.height,
        "mime_type": asset.mime_type,
    }


def quality_score(asset: Asset) -> float:
    facts = quality_facts(asset)
    return float(
        facts["pixel_area"]
        + (0.1 if facts["source_original"] else 0)
        + (0.01 if facts["lossless"] else 0)
        + min(facts["file_size"] / 1_000_000_000_000, 0.009)
    )


def _quality_key(asset: Asset) -> tuple[int, int, int, int, int]:
    facts = quality_facts(asset)
    return (
        facts["pixel_area"],
        1 if facts["source_original"] else 0,
        1 if facts["lossless"] else 0,
        facts["file_size"],
        -asset.id.int,
    )


class AssetReconciliation:
    """Deep application module for asset evidence, cases, and decisions."""

    def __init__(self, db: AsyncSession):
        self.db = db
        self.scope = CrossSourceDedupScope(db)
        self._policy_value: DedupPolicy | None = None

    async def policy(self) -> DedupPolicy:
        if self._policy_value is None:
            from app.services.settings import get_system_setting

            raw = await get_system_setting(self.db, "dedup")
            self._policy_value = DedupPolicy(
                auto_group_enabled=bool(
                    raw.get(
                        "auto_group_enabled",
                        raw.get("auto_merge", True),
                    )
                ),
                phash_threshold=max(0, min(4, int(raw.get("phash_threshold", 4)))),
                ssim_threshold=max(0.9, min(1.0, float(raw.get("ssim_threshold", 0.98)))),
                aspect_ratio_tolerance=max(
                    0.0,
                    min(0.05, float(raw.get("aspect_ratio_tolerance", 0.01))),
                ),
                auto_group_score=max(
                    70.0,
                    min(100.0, float(raw.get("auto_group_score", 95))),
                ),
                review_score=max(
                    0.0,
                    min(100.0, float(raw.get("review_score", 70))),
                ),
                quarantine_days=max(
                    1,
                    min(365, int(raw.get("quarantine_days", 30))),
                ),
            )
        return self._policy_value

    async def observe(self, asset_ids: list[UUID], *, auto_apply: bool = True) -> dict[str, int]:
        totals = {
            "assets_scanned": 0,
            "candidates_evaluated": 0,
            "cases_created": 0,
            "assets_grouped": 0,
            "storage_actions": 0,
            "bytes_reclaimable": 0,
        }
        for asset_id in asset_ids:
            results = await self.evaluate_asset(asset_id, auto_apply=auto_apply)
            totals["assets_scanned"] += 1
            totals["candidates_evaluated"] += len(results)
            totals["cases_created"] += sum(1 for item in results if item.case_created)
            totals["assets_grouped"] += sum(1 for item in results if item.auto_merged)
            totals["storage_actions"] += sum(item.storage_actions for item in results)
            totals["bytes_reclaimable"] += sum(
                item.bytes_reclaimable for item in results
            )
        return totals

    async def evaluate_asset(
        self,
        asset_id: UUID,
        *,
        auto_apply: bool = True,
    ) -> list[EvaluationResult]:
        asset = await self.db.get(Asset, asset_id)
        if not asset or asset.mime_type not in IMAGE_MIME_TYPES:
            return []
        if not asset.sha256 and not asset.phash:
            return []

        candidates = await self._candidate_assets(asset)
        policy = await self.policy()
        results: list[EvaluationResult] = []
        for candidate in candidates:
            scope = await self.scope.pair(asset.id, candidate.id)
            if not scope.eligible:
                continue
            # Candidate and scope reads start an implicit PostgreSQL
            # transaction.  Release it before SSIM opens and decodes files;
            # the pair is locked and its scope is checked again below before
            # any evidence/case mutation is committed.
            await self.db.commit()
            evidence_facts = await self._evaluate_pair(asset, candidate)
            if not evidence_facts["sha256_equal"] and not evidence_facts["hard_gate_passed"]:
                continue
            if evidence_facts["total_score"] < policy.review_score:
                continue

            await self._lock_pair(asset.id, candidate.id)
            scope = await self.scope.pair(asset.id, candidate.id)
            if not scope.eligible:
                continue
            evidence_facts["facts"]["scope"] = scope.facts()
            evidence_facts["input_digest"] = hashlib.sha256(
                json.dumps(
                    {
                        "visual_input_digest": evidence_facts["input_digest"],
                        "scope": scope.facts(),
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
            evidence = await self._persist_evidence(asset, candidate, evidence_facts)
            case, case_created = await self._upsert_case(asset, candidate, evidence)
            auto_merged = False
            storage_actions = 0
            bytes_reclaimable = 0
            should_auto = (
                evidence.sha256_equal
                or (
                    evidence.hard_gate_passed
                    and evidence.total_score >= policy.auto_group_score
                )
            )
            if (
                auto_apply
                and policy.auto_group_enabled
                and should_auto
                and case.status == "pending"
            ):
                can_auto = await self._can_auto_merge_groups(asset.id, candidate.id, evidence)
                if can_auto:
                    receipt = await self.decide(
                        case.id,
                        expected_revision=case.revision,
                        action="merge",
                        representative_asset_id=None,
                        actor_type="system",
                        actor_id="asset-reconciliation",
                        reason="Automatic asset-level reconciliation",
                        idempotency_key=(
                            f"asset-dedup:auto:{case.id}:{case.revision}:{evidence.input_digest}"
                        ),
                    )
                    auto_merged = True
                    storage_actions = int(receipt["storage_actions"])
                    bytes_reclaimable = int(receipt["bytes_reclaimable"])

            results.append(
                EvaluationResult(
                    evidence_id=evidence.id,
                    case_id=case.id,
                    status=case.status,
                    case_created=case_created,
                    auto_merged=auto_merged,
                    storage_actions=storage_actions,
                    bytes_reclaimable=bytes_reclaimable,
                )
            )
        return results

    async def list_cases(
        self,
        *,
        status: str = "pending",
        offset: int = 0,
        limit: int = 50,
    ) -> dict[str, Any]:
        filters = [AssetDedupCase.status == status] if status != "all" else []
        total = (
            await self.db.execute(
                select(func.count(AssetDedupCase.id)).where(*filters)
            )
        ).scalar_one()
        rows = await self.db.execute(
            select(AssetDedupCase)
            .where(*filters)
            .order_by(AssetDedupCase.created_at.desc(), AssetDedupCase.id.desc())
            .offset(offset)
            .limit(limit)
        )
        cases = list(rows.scalars().all())
        return {
            "items": [await self.case_payload(case) for case in cases],
            "total": total,
            "offset": offset,
            "limit": limit,
        }

    async def get_case(self, case_id: UUID) -> dict[str, Any] | None:
        case = await self.db.get(AssetDedupCase, case_id)
        return await self.case_payload(case) if case else None

    async def case_payload(self, case: AssetDedupCase) -> dict[str, Any]:
        evidence = await self.db.get(AssetDedupEvidence, case.evidence_id)
        return {
            "id": case.id,
            "status": case.status,
            "revision": case.revision,
            "left": await self._asset_payload(case.left_asset_id),
            "right": await self._asset_payload(case.right_asset_id),
            "evidence": {
                "id": evidence.id,
                "algorithm_version": evidence.algorithm_version,
                "sha256_equal": evidence.sha256_equal,
                "phash_distance": evidence.phash_distance,
                "ssim_score": evidence.ssim_score,
                "aspect_ratio_delta": evidence.aspect_ratio_delta,
                "visual_score": evidence.visual_score,
                "metadata_score": evidence.metadata_score,
                "total_score": evidence.total_score,
                "hard_gate_passed": evidence.hard_gate_passed,
                "facts": evidence.facts or {},
            },
            "suggested_representative_asset_id": case.suggested_representative_asset_id,
            "created_at": case.created_at,
            "decided_at": case.decided_at,
            "decided_by": case.decided_by,
            "decision_reason": case.decision_reason,
        }

    async def decide(
        self,
        case_id: UUID,
        *,
        expected_revision: int,
        action: str,
        representative_asset_id: UUID | None,
        actor_type: str,
        actor_id: str | None,
        reason: str | None,
        idempotency_key: str,
    ) -> dict[str, Any]:
        if action not in {"merge", "separate", "defer"}:
            raise ValueError("Unsupported dedup action")
        request_payload = {
            "case_id": str(case_id),
            "expected_revision": expected_revision,
            "action": action,
            "representative_asset_id": (
                str(representative_asset_id) if representative_asset_id else None
            ),
            "reason": reason,
        }
        request_digest = hashlib.sha256(
            json.dumps(request_payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        await self.db.execute(
            text("SELECT pg_advisory_xact_lock(:lock_key)"),
            {
                "lock_key": _advisory_lock_key(
                    "asset-dedup-decision",
                    idempotency_key,
                )
            },
        )
        existing = (
            await self.db.execute(
                select(AssetDedupDecision).where(
                    AssetDedupDecision.idempotency_key == idempotency_key
                )
            )
        ).scalar_one_or_none()
        if existing:
            if existing.request_digest != request_digest:
                raise DedupIdempotencyConflict(
                    "Idempotency key was already used with a different decision"
                )
            return dict(existing.result or {})

        case = (
            await self.db.execute(
                select(AssetDedupCase)
                .where(AssetDedupCase.id == case_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if not case:
            raise KeyError("Dedup case not found")
        if case.revision != expected_revision or case.status not in {"pending", "deferred"}:
            raise StaleDedupCase("Dedup case changed; refresh before deciding")
        evidence = await self.db.get(AssetDedupEvidence, case.evidence_id)
        previous_status = case.status

        group_id: UUID | None = None
        storage_actions = 0
        bytes_reclaimable = 0
        if action == "merge":
            requested_representative = representative_asset_id
            if requested_representative is None and actor_type != "system":
                requested_representative = case.suggested_representative_asset_id
            (
                group_id,
                representative_asset_id,
                storage_actions,
                bytes_reclaimable,
            ) = await self._merge_assets(
                case.left_asset_id,
                case.right_asset_id,
                representative_asset_id=requested_representative,
                evidence=evidence,
            )
            case.status = "merged"
        elif action == "separate":
            case.status = "separate"
        else:
            case.status = "deferred"
        case.revision += 1
        case.decided_by = actor_id
        case.decided_at = _utcnow()
        case.decision_reason = reason

        curation = CurationService(self.db)
        commit = await curation.record_asset_dedup_decision(
            case_id=case.id,
            left_asset_id=case.left_asset_id,
            right_asset_id=case.right_asset_id,
            action=action,
            actor_type=actor_type,
            actor_id=actor_id,
            representative_asset_id=representative_asset_id,
            group_id=group_id,
            reason=reason,
            evidence=self._evidence_summary(evidence),
            previous_status=previous_status,
            dedupe_key=f"asset-dedup:{idempotency_key}",
        )
        result = {
            "case_id": str(case.id),
            "action": action,
            "status": case.status,
            "revision": case.revision,
            "representative_asset_id": (
                str(representative_asset_id) if representative_asset_id else None
            ),
            "group_id": str(group_id) if group_id else None,
            "storage_actions": storage_actions,
            "bytes_reclaimable": bytes_reclaimable,
            "curation_commit_id": str(commit.id),
        }
        decision = AssetDedupDecision(
            case_id=case.id,
            idempotency_key=idempotency_key,
            request_digest=request_digest,
            action=action,
            actor_type=actor_type,
            actor_id=actor_id,
            representative_asset_id=representative_asset_id,
            result=result,
            curation_commit_id=commit.id,
        )
        self.db.add(decision)
        await self.db.flush()
        result["decision_id"] = str(decision.id)
        decision.result = result
        return result

    async def _lock_pair(self, left_id: UUID, right_id: UUID) -> None:
        ordered = _ordered_pair(left_id, right_id)
        await self.db.execute(
            text("SELECT pg_advisory_xact_lock(:lock_key)"),
            {
                "lock_key": _advisory_lock_key(
                    "asset-dedup-pair",
                    f"{ordered[0]}:{ordered[1]}",
                )
            },
        )

    async def _candidate_assets(self, asset: Asset) -> list[Asset]:
        anchor = (await self.scope.contexts((asset.id,)))[asset.id]
        if not anchor.complete:
            return []
        conditions = []
        if asset.sha256:
            conditions.append(Asset.sha256 == asset.sha256)
        if asset.phash:
            phash_version_matches = (
                Asset.phash_version.is_(None)
                if asset.phash_version is None
                else Asset.phash_version == asset.phash_version
            )
            for start, length in ((1, 3), (4, 3), (7, 3), (10, 3), (13, 4)):
                conditions.append(
                    and_(
                        phash_version_matches,
                        func.substr(Asset.phash, start, length)
                        == asset.phash[start - 1 : start - 1 + length],
                    )
                )
        if not conditions:
            return []

        candidate_asset_source = aliased(AssetSource)
        candidate_work_source = aliased(WorkSource)
        bound_candidate = exists(
            select(1)
            .select_from(candidate_asset_source)
            .join(
                candidate_work_source,
                candidate_work_source.id
                == candidate_asset_source.work_source_id,
            )
            .where(candidate_asset_source.asset_id == Asset.id)
        )
        shared_work = exists(
            select(1)
            .select_from(candidate_asset_source)
            .join(
                candidate_work_source,
                candidate_work_source.id
                == candidate_asset_source.work_source_id,
            )
            .where(
                candidate_asset_source.asset_id == Asset.id,
                candidate_work_source.work_id.in_(anchor.work_ids),
            )
        )
        blocked_source_keys = set(anchor.raw_sources)
        blocked_source_keys.update(anchor.sources)
        blocked_source_keys.update(
            extractor_key_for_source(source) for source in anchor.sources
        )
        shared_source = exists(
            select(1)
            .select_from(candidate_asset_source)
            .join(
                candidate_work_source,
                candidate_work_source.id
                == candidate_asset_source.work_source_id,
            )
            .where(
                candidate_asset_source.asset_id == Asset.id,
                func.lower(candidate_work_source.source).in_(
                    sorted(blocked_source_keys)
                ),
            )
        )
        rows = await self.db.execute(
            select(Asset)
            .where(
                Asset.id != asset.id,
                Asset.mime_type.in_(IMAGE_MIME_TYPES),
                or_(*conditions),
                bound_candidate,
                ~shared_work,
                ~shared_source,
            )
            .order_by(
                (Asset.sha256 == asset.sha256).desc() if asset.sha256 else Asset.created_at,
                Asset.created_at.desc(),
            )
            .limit(MAX_CANDIDATES_PER_ASSET)
        )
        return list(rows.scalars().all())

    async def _evaluate_pair(self, left: Asset, right: Asset) -> dict[str, Any]:
        policy = await self.policy()
        left_id, right_id = _ordered_pair(left.id, right.id)
        if left.id != left_id:
            left, right = right, left

        sha_equal = bool(left.sha256 and left.sha256 == right.sha256)
        distance = versioned_phash_distance(
            left.phash,
            right.phash,
            left.phash_version,
            right.phash_version,
        )
        ratio_delta = aspect_ratio_delta(left, right)
        hard_compatible = (
            distance is not None
            and distance <= policy.phash_threshold
            and ratio_delta is not None
            and ratio_delta <= policy.aspect_ratio_tolerance
        )
        ssim: float | None = None
        if not sha_equal and hard_compatible:
            left_path = _safe_download_path(left.file_path)
            right_path = _safe_download_path(right.file_path)
            if left_path.exists() and right_path.exists():
                try:
                    ssim = await asyncio.to_thread(
                        structural_similarity, left_path, right_path
                    )
                except Exception:
                    ssim = None

        hard_gate_passed = sha_equal or (
            hard_compatible and ssim is not None and ssim >= policy.ssim_threshold
        )
        if sha_equal:
            visual_score = 100.0
        elif hard_gate_passed:
            phash_divisor = max(1, policy.phash_threshold)
            phash_quality = max(0.0, 1.0 - float(distance or 0) / phash_divisor)
            ssim_divisor = max(0.000001, 1 - policy.ssim_threshold)
            ssim_quality = max(0.0, min(1.0, ((ssim or 0) - policy.ssim_threshold) / ssim_divisor))
            ratio_quality = max(
                0.0,
                1.0 - float(ratio_delta or 0) / max(0.000001, policy.aspect_ratio_tolerance),
            )
            visual_score = 70.0 + 10.0 * phash_quality + 15.0 * ssim_quality + 5.0 * ratio_quality
        else:
            visual_score = 0.0

        metadata = await self._metadata_evidence(left.id, right.id)
        metadata_score = (
            float(metadata["score"])
            if hard_gate_passed and not sha_equal
            else 0.0
        )
        total_score = 100.0 if sha_equal else min(100.0, visual_score + metadata_score)
        thresholds = {
            "phash_max_distance": policy.phash_threshold,
            "aspect_ratio_max_delta": policy.aspect_ratio_tolerance,
            "ssim_min_score": policy.ssim_threshold,
            "auto_merge_score": policy.auto_group_score,
            "review_min_score": policy.review_score,
            "quarantine_days": policy.quarantine_days,
        }
        input_payload = {
            "algorithm": ALGORITHM_VERSION,
            "left": self._asset_fingerprint(left),
            "right": self._asset_fingerprint(right),
            "metadata": metadata,
            "thresholds": thresholds,
        }
        input_digest = hashlib.sha256(
            json.dumps(input_payload, sort_keys=True, separators=(",", ":"), default=str).encode()
        ).hexdigest()
        return {
            "input_digest": input_digest,
            "sha256_equal": sha_equal,
            "phash_distance": distance,
            "ssim_score": ssim,
            "aspect_ratio_delta": ratio_delta,
            "visual_score": round(visual_score, 4),
            "metadata_score": round(metadata_score, 4),
            "total_score": round(total_score, 4),
            "hard_gate_passed": hard_gate_passed,
            "facts": {
                "metadata": metadata,
                "thresholds": thresholds,
            },
        }

    async def _metadata_evidence(self, left_id: UUID, right_id: UUID) -> dict[str, Any]:
        left = await self._source_contexts(left_id)
        right = await self._source_contexts(right_id)
        left_creators = {item["creator_id"] for item in left if item["creator_id"]}
        right_creators = {item["creator_id"] for item in right if item["creator_id"]}
        same_creator = bool(left_creators & right_creators)

        deltas: list[float] = []
        for left_item in left:
            for right_item in right:
                if left_item["posted_at"] and right_item["posted_at"]:
                    deltas.append(
                        abs(
                            (
                                left_item["posted_at"] - right_item["posted_at"]
                            ).total_seconds()
                        )
                        / 3600
                    )
        min_delta_hours = min(deltas) if deltas else None
        creator_bonus = 6.0 if same_creator else 0.0
        time_bonus = metadata_time_bonus(min_delta_hours)
        score = creator_bonus + time_bonus
        return {
            "same_canonical_creator": same_creator,
            "min_posted_delta_hours": (
                round(min_delta_hours, 3) if min_delta_hours is not None else None
            ),
            "score": min(10.0, score),
            "creator_bonus": creator_bonus,
            "time_bonus": time_bonus,
            "left_sources": sorted({item["source"] for item in left}),
            "right_sources": sorted({item["source"] for item in right}),
        }

    async def _source_contexts(self, asset_id: UUID) -> list[dict[str, Any]]:
        rows = await self.db.execute(
            select(
                WorkSource.source,
                WorkSource.posted_at,
                SourceCreator.creator_id,
            )
            .select_from(AssetSource)
            .join(WorkSource, WorkSource.id == AssetSource.work_source_id)
            .outerjoin(
                SourceCreator,
                (SourceCreator.source == WorkSource.source)
                & (
                    SourceCreator.source_creator_id
                    == WorkSource.source_creator_id
                ),
            )
            .where(AssetSource.asset_id == asset_id)
        )
        return [
            {
                "source": canonical_source(row.source),
                "posted_at": row.posted_at,
                "creator_id": str(row.creator_id) if row.creator_id else None,
            }
            for row in rows
        ]

    async def _persist_evidence(
        self,
        left: Asset,
        right: Asset,
        facts: dict[str, Any],
    ) -> AssetDedupEvidence:
        left_id, right_id = _ordered_pair(left.id, right.id)
        existing = (
            await self.db.execute(
                select(AssetDedupEvidence).where(
                    AssetDedupEvidence.left_asset_id == left_id,
                    AssetDedupEvidence.right_asset_id == right_id,
                    AssetDedupEvidence.algorithm_version == ALGORITHM_VERSION,
                    AssetDedupEvidence.input_digest == facts["input_digest"],
                )
            )
        ).scalar_one_or_none()
        if existing:
            return existing
        evidence = AssetDedupEvidence(
            left_asset_id=left_id,
            right_asset_id=right_id,
            algorithm_version=ALGORITHM_VERSION,
            **facts,
        )
        self.db.add(evidence)
        await self.db.flush()
        return evidence

    async def _upsert_case(
        self,
        left: Asset,
        right: Asset,
        evidence: AssetDedupEvidence,
    ) -> tuple[AssetDedupCase, bool]:
        left_id, right_id = _ordered_pair(left.id, right.id)
        case = (
            await self.db.execute(
                select(AssetDedupCase)
                .where(
                    AssetDedupCase.left_asset_id == left_id,
                    AssetDedupCase.right_asset_id == right_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        suggested = max((left, right), key=_quality_key).id
        if not case:
            case = AssetDedupCase(
                left_asset_id=left_id,
                right_asset_id=right_id,
                evidence_id=evidence.id,
                status="pending",
                revision=1,
                suggested_representative_asset_id=suggested,
            )
            self.db.add(case)
            await self.db.flush()
            return case, True
        if case.evidence_id != evidence.id and case.status != "merged":
            case.evidence_id = evidence.id
            case.revision += 1
            case.suggested_representative_asset_id = suggested
            if case.status in {"pending", "deferred"}:
                case.status = "pending"
                case.decided_at = None
                case.decided_by = None
                case.decision_reason = None
            # A human “not the same image” decision is a durable negative
            # constraint. New thresholds/evidence may be recorded, but must
            # never silently reopen it for automatic grouping.
            return case, False
        return case, False

    async def _can_auto_merge_groups(
        self,
        left_id: UUID,
        right_id: UUID,
        evidence: AssetDedupEvidence,
    ) -> bool:
        pair_scope = await self.scope.pair(left_id, right_id)
        if not pair_scope.eligible:
            return False
        group_rows = await self.db.execute(
            select(VisualAssetMember.asset_id, VisualAssetMember.group_id).where(
                VisualAssetMember.asset_id.in_([left_id, right_id])
            )
        )
        groups = {row.group_id for row in group_rows}
        if groups:
            # Group expansion is deliberately review-only. The manual merge
            # path validates the full prospective group under a row lock.
            return False
        return (
            await self.scope.group((left_id, right_id))
        ).eligible

    async def _merge_assets(
        self,
        left_id: UUID,
        right_id: UUID,
        *,
        representative_asset_id: UUID | None,
        evidence: AssetDedupEvidence,
    ) -> tuple[UUID, UUID, int, int]:
        pair_scope = await self.scope.pair(left_id, right_id)
        if not pair_scope.eligible:
            raise ValueError(scope_error_message(pair_scope.reason))
        assets = list(
            (
                await self.db.execute(
                    select(Asset)
                    .where(Asset.id.in_([left_id, right_id]))
                    .order_by(Asset.id)
                    .with_for_update()
                )
            ).scalars()
        )
        if len(assets) != 2:
            raise KeyError("One or more assets no longer exist")

        member_rows = list(
            (
                await self.db.execute(
                    select(VisualAssetMember)
                    .where(VisualAssetMember.asset_id.in_([left_id, right_id]))
                    .with_for_update()
                )
            ).scalars()
        )
        group_ids = {member.group_id for member in member_rows}
        if len(group_ids) > 1:
            raise ValueError(
                "Existing visual groups cannot be merged transitively"
            )
        all_asset_ids = {left_id, right_id}
        if group_ids:
            all_asset_ids.update(
                (
                    await self.db.execute(
                        select(VisualAssetMember.asset_id).where(
                            VisualAssetMember.group_id.in_(group_ids)
                        )
                    )
                ).scalars()
            )
        group_scope = await self.scope.group(all_asset_ids)
        if not group_scope.eligible:
            raise ValueError(scope_error_message(group_scope.reason))
        all_assets = list(
            (
                await self.db.execute(
                    select(Asset).where(Asset.id.in_(all_asset_ids))
                )
            ).scalars()
        )
        blocked_pair = (
            await self.db.execute(
                select(AssetDedupCase.id)
                .where(
                    AssetDedupCase.status == "separate",
                    AssetDedupCase.left_asset_id.in_(all_asset_ids),
                    AssetDedupCase.right_asset_id.in_(all_asset_ids),
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if blocked_pair:
            raise ValueError(
                "This merge conflicts with an existing ‘not the same image’ decision"
            )
        states = {
            state.asset_id: state
            for state in (
                (
                    await self.db.execute(
                        select(AssetStorageState).where(
                            AssetStorageState.asset_id.in_(all_asset_ids)
                        )
                    )
                ).scalars()
            )
        }
        available_assets = [
            asset
            for asset in all_assets
            if states.get(asset.id) is None
            or states[asset.id].storage_state not in {"quarantined", "purged"}
        ]
        existing_group = None
        if group_ids:
            existing_group = await self.db.get(
                VisualAssetGroup, next(iter(group_ids))
            )
            if not existing_group:
                raise ValueError("Existing visual group no longer exists")
            if (
                representative_asset_id is not None
                and representative_asset_id
                != existing_group.representative_asset_id
            ):
                raise ValueError(
                    "An existing visual group's representative cannot change "
                    "during expansion"
                )
            representative = next(
                (
                    asset
                    for asset in available_assets
                    if asset.id == existing_group.representative_asset_id
                ),
                None,
            )
            if not representative:
                raise ValueError(
                    "Existing visual group representative is unavailable"
                )
            current_member_ids = {
                member.asset_id for member in member_rows
            }
            current_member_ids.update(
                (
                    await self.db.execute(
                        select(VisualAssetMember.asset_id).where(
                            VisualAssetMember.group_id
                            == existing_group.id
                        )
                    )
                ).scalars()
            )
            new_asset_ids = all_asset_ids - current_member_ids
            for new_asset_id in new_asset_ids:
                new_asset = next(
                    asset for asset in all_assets if asset.id == new_asset_id
                )
                evidence_pair = {
                    evidence.left_asset_id,
                    evidence.right_asset_id,
                }
                expected_pair = {representative.id, new_asset.id}
                if evidence_pair == expected_pair:
                    validation = {
                        "hard_gate_passed": evidence.hard_gate_passed,
                        "total_score": evidence.total_score,
                    }
                else:
                    validation = await self._evaluate_pair(
                        representative, new_asset
                    )
                policy = await self.policy()
                if (
                    not validation["hard_gate_passed"]
                    or validation["total_score"] < policy.review_score
                ):
                    raise ValueError(
                        "New group member does not match the current "
                        "representative"
                    )
        elif representative_asset_id:
            representative = next(
                (asset for asset in available_assets if asset.id == representative_asset_id),
                None,
            )
            if not representative:
                raise ValueError("Representative must be an available member asset")
        else:
            representative = max(available_assets, key=_quality_key)

        if group_ids:
            target_group_id = next(iter(group_ids))
            group = existing_group
        else:
            group = VisualAssetGroup(
                representative_asset_id=representative.id,
                policy_version=QUALITY_POLICY_VERSION,
            )
            self.db.add(group)
            await self.db.flush()
            target_group_id = group.id

        existing_members = {
            member.asset_id: member
            for member in (
                (
                    await self.db.execute(
                        select(VisualAssetMember).where(
                            VisualAssetMember.group_id.in_(group_ids)
                            if group_ids
                            else VisualAssetMember.group_id == target_group_id
                        )
                    )
                ).scalars()
            )
        }
        for asset in all_assets:
            member = existing_members.get(asset.id)
            if member:
                member.group_id = target_group_id
                if asset.id in {left_id, right_id}:
                    member.evidence_id = evidence.id
                member.quality_score = quality_score(asset)
                member.quality_facts = quality_facts(asset)
            else:
                self.db.add(
                    VisualAssetMember(
                        group_id=target_group_id,
                        asset_id=asset.id,
                        evidence_id=evidence.id
                        if asset.id in {left_id, right_id}
                        else None,
                        quality_score=quality_score(asset),
                        quality_facts=quality_facts(asset),
                    )
                )
        for old_group_id in group_ids - {target_group_id}:
            await self.db.execute(
                delete(VisualAssetGroup).where(VisualAssetGroup.id == old_group_id)
            )
        group.representative_asset_id = representative.id
        group.policy_version = QUALITY_POLICY_VERSION

        actions = 0
        bytes_reclaimable = 0
        for asset in all_assets:
            state = states.get(asset.id)
            if not state:
                state = AssetStorageState(
                    asset_id=asset.id,
                    storage_state="available",
                    bytes_reclaimed=0,
                )
                self.db.add(state)
                states[asset.id] = state
            if asset.id == representative.id:
                state.served_by_asset_id = None
                if state.storage_state not in {"quarantined", "purged"}:
                    state.storage_state = "available"
                continue
            state.served_by_asset_id = representative.id
            if state.storage_state in {"quarantined", "purged"}:
                continue
            exact = bool(asset.sha256 and asset.sha256 == representative.sha256)
            state.dedup_kind = "exact_sha256" if exact else "visual"
            event_type = "hardlink" if exact else "quarantine"
            event_key = (
                f"asset-dedup:{group.id}:{asset.id}:{event_type}:{representative.id}"
            )
            exists = (
                await self.db.execute(
                    select(AssetDedupOutbox.id).where(
                        AssetDedupOutbox.idempotency_key == event_key
                    )
                )
            ).scalar_one_or_none()
            if not exists:
                self.db.add(
                    AssetDedupOutbox(
                        idempotency_key=event_key,
                        event_type=event_type,
                        payload={
                            "group_id": str(group.id),
                            "asset_id": str(asset.id),
                            "representative_asset_id": str(representative.id),
                            "evidence_id": str(evidence.id),
                        },
                        state="pending",
                        attempts=0,
                        available_at=_utcnow(),
                    )
                )
                actions += 1
                bytes_reclaimable += int(asset.file_size or 0)
        await self.db.flush()
        return group.id, representative.id, actions, bytes_reclaimable

    async def _asset_payload(self, asset_id: UUID) -> dict[str, Any]:
        asset = await self.db.get(Asset, asset_id)
        row = (
            await self.db.execute(
                select(
                    WorkSource.source,
                    WorkSource.source_work_id,
                    WorkSource.source_url,
                    WorkSource.posted_at,
                    Work.id.label("work_id"),
                    Work.title.label("work_title"),
                    SourceCreator.creator_id,
                    SourceCreator.display_name,
                )
                .select_from(AssetSource)
                .join(WorkSource, WorkSource.id == AssetSource.work_source_id)
                .join(Work, Work.id == WorkSource.work_id)
                .outerjoin(
                    SourceCreator,
                    (SourceCreator.source == WorkSource.source)
                    & (
                        SourceCreator.source_creator_id
                        == WorkSource.source_creator_id
                    ),
                )
                .where(AssetSource.asset_id == asset_id)
                .limit(1)
            )
        ).one_or_none()
        member = (
            await self.db.execute(
                select(VisualAssetMember, VisualAssetGroup.representative_asset_id)
                .join(
                    VisualAssetGroup,
                    VisualAssetGroup.id == VisualAssetMember.group_id,
                )
                .where(VisualAssetMember.asset_id == asset_id)
            )
        ).one_or_none()
        return {
            "id": asset.id,
            "file_name": asset.file_name,
            "file_size": asset.file_size,
            "mime_type": asset.mime_type,
            "width": asset.width,
            "height": asset.height,
            "sha256": asset.sha256,
            "phash": asset.phash,
            "phash_version": asset.phash_version,
            "source": row.source if row else None,
            "source_work_id": row.source_work_id if row else None,
            "source_url": row.source_url if row else None,
            "work_id": row.work_id if row else None,
            "work_title": row.work_title if row else None,
            "creator_id": row.creator_id if row else None,
            "creator_name": row.display_name if row else None,
            "posted_at": row.posted_at if row else None,
            "thumb_url": f"/media/thumb/{asset.id}",
            "preview_url": signed_media_url(str(asset.id), "preview"),
            "group_id": member[0].group_id if member else None,
            "is_representative": bool(
                member and member[1] == asset.id
            ),
        }

    @staticmethod
    def _asset_fingerprint(asset: Asset) -> dict[str, Any]:
        return {
            "id": str(asset.id),
            "sha256": asset.sha256,
            "phash": asset.phash,
            "file_size": asset.file_size,
            "width": asset.width,
            "height": asset.height,
            "mime_type": asset.mime_type,
        }

    @staticmethod
    def _evidence_summary(evidence: AssetDedupEvidence) -> dict[str, Any]:
        return {
            "evidence_id": str(evidence.id),
            "algorithm_version": evidence.algorithm_version,
            "sha256_equal": evidence.sha256_equal,
            "phash_distance": evidence.phash_distance,
            "ssim_score": evidence.ssim_score,
            "aspect_ratio_delta": evidence.aspect_ratio_delta,
            "visual_score": evidence.visual_score,
            "metadata_score": evidence.metadata_score,
            "total_score": evidence.total_score,
            "hard_gate_passed": evidence.hard_gate_passed,
            "facts": evidence.facts or {},
        }

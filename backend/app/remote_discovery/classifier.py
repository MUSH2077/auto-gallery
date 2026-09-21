"""Explainable, conservative local-identity confidence classification."""

from dataclasses import dataclass
from typing import Literal


Confidence = Literal["high", "medium", "low"]


@dataclass(frozen=True)
class IdentityMatchSignals:
    source: Literal["pixiv", "x", "bilibili"]
    local_identity_match_count: int = 0
    danbooru_verified_link: bool = False
    verified_cross_site_link: bool = False
    pixiv_illustration_preview: bool = False
    art_focused_bio: bool = False
    recent_visual_post: bool = False
    supported_site_link: bool = False

    def __post_init__(self) -> None:
        if self.source not in {"pixiv", "x", "bilibili"}:
            raise ValueError("unsupported discovery source")
        if self.local_identity_match_count < 0:
            raise ValueError("local identity match count cannot be negative")


@dataclass(frozen=True)
class IdentityClassification:
    confidence: Confidence
    reasons: tuple[str, ...]
    identity_conflict: bool
    auto_importable: bool


def classify_identity(signals: IdentityMatchSignals) -> IdentityClassification:
    """Apply the approved deterministic tiers; conflicts always require review."""

    reasons: list[str] = []
    conflict = signals.local_identity_match_count > 1
    if conflict:
        reasons.append("multiple_local_identity_matches")

    if signals.local_identity_match_count == 1:
        confidence: Confidence = "high"
        reasons.append("unique_local_identity")
    elif signals.danbooru_verified_link:
        confidence = "high"
        reasons.append("danbooru_verified_link")
    elif signals.verified_cross_site_link:
        confidence = "high"
        reasons.append("verified_cross_site_link")
    elif signals.source == "pixiv" and signals.pixiv_illustration_preview:
        confidence = "high"
        reasons.append("pixiv_illustration_preview")
    else:
        evidence = tuple(
            reason
            for enabled, reason in (
                (signals.art_focused_bio, "art_focused_bio"),
                (signals.recent_visual_post, "recent_visual_post"),
                (signals.supported_site_link, "supported_site_link"),
            )
            if enabled
        )
        if signals.source in {"x", "bilibili"} and len(evidence) >= 2:
            confidence = "high"
            reasons.extend(("multiple_creator_evidence", *evidence))
        elif len(evidence) == 1:
            confidence = "medium"
            reasons.extend(("single_creator_evidence", *evidence))
        else:
            confidence = "low"
            reasons.append("no_creator_evidence")

    return IdentityClassification(
        confidence=confidence,
        reasons=tuple(reasons),
        identity_conflict=conflict,
        auto_importable=not conflict,
    )

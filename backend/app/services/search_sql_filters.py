"""SQL work-projection query rules used by SearchService."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import and_, func, literal_column, not_, or_, select

from app.models import (
    Asset, AssetSource, SourceCreator, SubscriptionSource, Tag, Work,
    WorkCurationState, WorkSource, WorkSourceTag, WorkTag,
)
from app.services.search_filters import (
    _date_bounds, _grouped_qualifiers, _resolved_source_url, _resolved_value,
)
from app.services.search_language import SearchQuery
from app.services.source_search_identity import parse_source_identity


def _sql_date_expression(column, raw: str):
    operator, start, end = _date_bounds(raw)
    start_dt = datetime.fromtimestamp(start, tz=timezone.utc)
    end_dt = datetime.fromtimestamp(end, tz=timezone.utc) if end else None
    if operator == "=" and end_dt:
        return and_(column >= start_dt, column <= end_dt)
    if operator == ">":
        return column > start_dt
    if operator == ">=":
        return column >= start_dt
    if operator == "<":
        return column < start_dt
    if operator == "<=":
        return column <= start_dt
    return column == start_dt



def _works_db_compatible(query: SearchQuery) -> bool:
    """Whether a work query can use the real-time list projection."""
    supported = {
        "type",
        "source",
        "creator",
        "repo",
        "tag",
        "is",
        "has",
        "posted",
        "created",
        "updated",
        "sort",
        "uid",
        "pid",
        "url",
    }
    visibility_values = {
        token.value for token in query.qualifiers
        if token.key == "is" and not token.negated and token.value in {"visible", "trashed"}
    }
    return len(visibility_values) <= 1 and all(
        token.key in supported
        and (
            token.key != "has"
            or token.value
            in {
                "tags",
                "description",
                "multiple-assets",
                "image",
                "animation",
                "video",
            }
        )
        for token in query.qualifiers
    )



def _work_visibility_expressions():
    non_visible = select(WorkCurationState.id).where(
        WorkCurationState.work_id == Work.id,
        WorkCurationState.visibility != literal_column("'visible'"),
    ).exists()
    trashed = select(WorkCurationState.id).where(
        WorkCurationState.work_id == Work.id,
        WorkCurationState.visibility == literal_column("'trashed'"),
    ).exists()
    return not_(non_visible), trashed



def _work_has_tags_expression():
    direct = select(WorkTag.id).where(WorkTag.work_id == Work.id).exists()
    sourced = (
        select(WorkSourceTag.id)
        .join(WorkSource, WorkSource.id == WorkSourceTag.work_source_id)
        .where(WorkSource.work_id == Work.id)
        .exists()
    )
    return or_(direct, sourced)



def _work_filter_conditions(
    query: SearchQuery,
    resolved: dict[tuple[str, str], Any],
    *,
    force_sfw: bool,
) -> tuple[list[Any], set[str], Any]:
    """Compile the SQL projection with the same qualifier semantics as Meili."""

    visible_expression, trashed_expression = _work_visibility_expressions()
    has_tags_expression = _work_has_tags_expression()
    conditions: list[Any] = []
    requested_visibility = {
        token.value for token in query.qualifiers
        if token.key == "is" and not token.negated and token.value in {"visible", "trashed"}
    }
    conditions.append(
        trashed_expression if requested_visibility == {"trashed"} else visible_expression
    )
    if force_sfw:
        conditions.append(Work.is_nsfw.is_(False))

    multi_asset_ids = (
        select(WorkSource.work_id)
        .join(AssetSource, AssetSource.work_source_id == WorkSource.id)
        .group_by(WorkSource.work_id)
        .having(func.count(func.distinct(AssetSource.asset_id)) > 1)
    )

    def media_exists(kind: str):
        lowered_mime = func.lower(func.coalesce(Asset.mime_type, ""))
        lowered_name = func.lower(func.coalesce(Asset.file_name, ""))
        lowered_role = func.lower(func.coalesce(AssetSource.role, ""))
        media_condition = {
            "image": lowered_mime.like("image/%"),
            "video": or_(
                lowered_role == "video",
                lowered_mime.like("video/%"),
                lowered_name.like("%.mp4"),
                lowered_name.like("%.webm"),
            ),
            "animation": or_(
                lowered_role.in_(("animation", "archive")),
                lowered_mime.in_(("image/gif", "image/apng")),
                lowered_name.like("%.gif"),
                lowered_name.like("%.zip"),
            ),
        }[kind]
        return (
            select(AssetSource.id)
            .join(WorkSource, WorkSource.id == AssetSource.work_source_id)
            .join(Asset, Asset.id == AssetSource.asset_id)
            .where(WorkSource.work_id == Work.id, media_condition)
            .exists()
        )

    for (key, negated), tokens in _grouped_qualifiers(query, "works").items():
        expressions = []
        for token in tokens:
            value = _resolved_value(token, resolved)
            expression = None
            if key == "source":
                expression = Work.id.in_(
                    select(WorkSource.work_id).where(WorkSource.source == value)
                )
            elif key in {"uid", "pid"}:
                source, identity = parse_source_identity(token.value)
                identity_column = (
                    WorkSource.source_creator_id
                    if key == "uid" else WorkSource.source_work_id
                )
                expression = Work.id.in_(
                    select(WorkSource.work_id).where(
                        WorkSource.source == source,
                        identity_column == identity,
                    )
                )
            elif key == "url":
                source_url = _resolved_source_url(token, resolved)
                identities = source_url.work_ids if source_url else ()
                expression = Work.id.in_([UUID(identity) for identity in identities])
            elif key == "creator":
                expression = Work.id.in_(
                    select(WorkSource.work_id)
                    .join(
                        SourceCreator,
                        and_(
                            SourceCreator.source == WorkSource.source,
                            SourceCreator.source_creator_id == WorkSource.source_creator_id,
                        ),
                    )
                    .where(SourceCreator.creator_id == UUID(value))
                )
            elif key == "repo":
                repository_id = UUID(value)
                expression = (
                    select(WorkSource.id)
                    .join(
                        SubscriptionSource,
                        or_(
                            and_(
                                WorkSource.source_creator_id.is_not(None),
                                SubscriptionSource.source_creator_id.is_not(None),
                                SubscriptionSource.source == WorkSource.source,
                                SubscriptionSource.source_creator_id
                                == WorkSource.source_creator_id,
                            ),
                            and_(
                                WorkSource.source_url.is_not(None),
                                SubscriptionSource.source_url.is_not(None),
                                func.lower(
                                    func.rtrim(
                                        func.btrim(SubscriptionSource.source_url),
                                        "/",
                                    )
                                )
                                == func.lower(
                                    func.rtrim(
                                        func.btrim(WorkSource.source_url),
                                        "/",
                                    )
                                ),
                            ),
                        ),
                    )
                    .where(
                        WorkSource.work_id == Work.id,
                        SubscriptionSource.id == repository_id,
                    )
                    .exists()
                )
            elif key == "tag":
                direct_tag_ids = (
                    select(WorkTag.work_id)
                    .join(Tag, Tag.id == WorkTag.tag_id)
                    .where(Tag.normalized_name == value)
                )
                source_tag_ids = (
                    select(WorkSource.work_id)
                    .join(WorkSourceTag, WorkSourceTag.work_source_id == WorkSource.id)
                    .join(Tag, Tag.id == WorkSourceTag.tag_id)
                    .where(Tag.normalized_name == value)
                )
                expression = or_(
                    Work.id.in_(direct_tag_ids),
                    Work.id.in_(source_tag_ids),
                )
            elif key == "is":
                expression = {
                    "favorite": Work.is_favorite.is_(True),
                    "nsfw": Work.is_nsfw.is_(True),
                    "sfw": Work.is_nsfw.is_(False),
                    "ai": Work.is_ai_generated.is_(True),
                    "human": Work.is_ai_generated.is_(False),
                    "visible": visible_expression,
                    "trashed": trashed_expression,
                }.get(value)
            elif key == "has":
                if value == "tags":
                    expression = has_tags_expression
                elif value == "description":
                    expression = func.length(
                        func.btrim(func.coalesce(Work.description, ""))
                    ) > 0
                elif value == "multiple-assets":
                    expression = Work.id.in_(multi_asset_ids)
                elif value in {"image", "animation", "video"}:
                    expression = media_exists(value)
            elif key in {"posted", "created", "updated"}:
                expression = _sql_date_expression(getattr(Work, f"{key}_at"), value)
            if expression is not None:
                expressions.append(not_(expression) if negated else expression)
        if expressions:
            conditions.append(and_(*expressions) if negated else or_(*expressions))
    return conditions, requested_visibility, has_tags_expression

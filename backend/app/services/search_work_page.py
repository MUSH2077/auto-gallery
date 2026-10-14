"""Reusable SQL statements for the PostgreSQL work-list projection."""

from __future__ import annotations

from sqlalchemy import bindparam, func, or_, select
from sqlalchemy.dialects.postgresql import aggregate_order_by
from sqlalchemy.orm import aliased

from app.models import Asset, AssetSource, Work, WorkSource


def _build_work_page_media_statement():
    distinct_assets = (
        select(
            WorkSource.work_id.label("work_id"),
            Asset.id.label("asset_id"),
            Asset.mime_type.label("mime_type"),
            Asset.file_name.label("file_name"),
            Asset.width.label("width"),
            Asset.height.label("height"),
            func.min(AssetSource.ordinal).label("ordinal"),
            func.bool_or(
                func.lower(func.coalesce(AssetSource.role, "")) == "video"
            ).label("video_role"),
            func.bool_or(
                func.lower(func.coalesce(AssetSource.role, "")).in_(
                    ("animation", "archive")
                )
            ).label("animation_role"),
        )
        .join(AssetSource, AssetSource.work_source_id == WorkSource.id)
        .join(Asset, Asset.id == AssetSource.asset_id)
        .where(
            WorkSource.work_id.in_(bindparam("work_ids", expanding=True))
        )
        .group_by(
            WorkSource.work_id,
            Asset.id,
            Asset.mime_type,
            Asset.file_name,
            Asset.width,
            Asset.height,
        )
        .cte("work_page_distinct_assets")
        .prefix_with("MATERIALIZED", dialect="postgresql")
    )
    lowered_mime = func.lower(func.coalesce(distinct_assets.c.mime_type, ""))
    lowered_name = func.lower(func.coalesce(distinct_assets.c.file_name, ""))
    media_summary = (
        select(
            distinct_assets.c.work_id,
            func.count().label("asset_count"),
            func.bool_or(lowered_mime.like("image/%")).label("has_image"),
            func.bool_or(
                or_(
                    distinct_assets.c.video_role,
                    lowered_mime.like("video/%"),
                    lowered_name.like("%.mp4"),
                    lowered_name.like("%.webm"),
                )
            ).label("has_video"),
            func.bool_or(
                or_(
                    distinct_assets.c.animation_role,
                    lowered_mime.in_(("image/gif", "image/apng")),
                    lowered_name.like("%.gif"),
                    lowered_name.like("%.zip"),
                )
            ).label("has_animation"),
        )
        .group_by(distinct_assets.c.work_id)
        .cte("work_page_media_summary")
    )
    ranked_assets = select(
        distinct_assets.c.work_id,
        distinct_assets.c.asset_id,
        distinct_assets.c.width,
        distinct_assets.c.height,
        func.row_number().over(
            partition_by=distinct_assets.c.work_id,
            order_by=(
                distinct_assets.c.ordinal.asc().nulls_last(),
                distinct_assets.c.file_name,
                distinct_assets.c.asset_id,
            ),
        ).label("preview_position"),
    ).cte("work_page_ranked_assets")
    media_previews = (
        select(
            ranked_assets.c.work_id,
            func.array_agg(
                aggregate_order_by(
                    ranked_assets.c.asset_id,
                    ranked_assets.c.preview_position,
                )
            ).label("preview_asset_ids"),
            func.max(ranked_assets.c.width).filter(
                ranked_assets.c.preview_position == 1
            ).label("preview_width"),
            func.max(ranked_assets.c.height).filter(
                ranked_assets.c.preview_position == 1
            ).label("preview_height"),
        )
        .where(ranked_assets.c.preview_position <= 10)
        .group_by(ranked_assets.c.work_id)
        .cte("work_page_media_previews")
    )
    thumbnail_asset = aliased(Asset)
    return (
        select(
            media_summary.c.work_id,
            media_summary.c.asset_count,
            media_summary.c.has_image,
            media_summary.c.has_video,
            media_summary.c.has_animation,
            media_previews.c.preview_asset_ids,
            media_previews.c.preview_width,
            media_previews.c.preview_height,
            thumbnail_asset.width.label("thumbnail_width"),
            thumbnail_asset.height.label("thumbnail_height"),
        )
        .select_from(media_summary)
        .join(Work, Work.id == media_summary.c.work_id)
        .outerjoin(
            thumbnail_asset,
            thumbnail_asset.id == Work.thumbnail_asset_id,
        )
        .outerjoin(
            media_previews,
            media_previews.c.work_id == media_summary.c.work_id,
        )
    )


# Keep the CTE graph stable across requests. work_ids is a request
# parameter, so IDs from one page cannot be captured by another page.
WORK_PAGE_MEDIA_STATEMENT = _build_work_page_media_statement()

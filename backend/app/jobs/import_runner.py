import json
import shutil
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.config import settings
from app.database import async_session
from app.models import (
    Work, WorkSource, Asset, AssetSource, Tag, WorkTag, WorkSourceTag, SourceCreator,
)
from app.models.import_job import ImportJob
from app.models.download_job import DownloadJob
from app.providers import registry


async def run_import_job(import_job_id: str):
    job_uuid = UUID(import_job_id)
    async with async_session() as db:
        result = await db.execute(select(ImportJob).where(ImportJob.id == job_uuid))
        import_job = result.scalar_one_or_none()
        if not import_job:
            return

        dj_result = await db.execute(select(DownloadJob).where(DownloadJob.id == import_job.download_job_id))
        download_job = dj_result.scalar_one_or_none()
        if not download_job:
            import_job.status = "failed"
            import_job.error_log = "Download job not found"
            await db.commit()
            return

        import_job.status = "running"
        await db.flush()

        try:
            provider = registry.get(download_job.source)
            job_dir = Path(settings.download_root) / str(download_job.id)
            info_files = list(job_dir.rglob("*.json"))

            if not info_files:
                import_job.status = "failed"
                import_job.error_log = "No .json metadata files found in download directory"
                await db.commit()
                return

            imported = 0
            for info_file in info_files:
                try:
                    with open(info_file) as f:
                        raw_metadata = json.load(f)

                    # 1. Find/create SourceCreator
                    sc_data = provider.parse_source_creator(raw_metadata)
                    existing = await db.execute(
                        select(SourceCreator).where(
                            SourceCreator.source == sc_data["source"],
                            SourceCreator.source_creator_id == sc_data["source_creator_id"],
                        )
                    )
                    if not existing.scalar_one_or_none():
                        sc = SourceCreator(
                            source=sc_data["source"],
                            source_creator_id=sc_data["source_creator_id"],
                            source_url=sc_data.get("source_url"),
                            display_name=sc_data.get("display_name"),
                            raw_metadata=sc_data.get("raw_metadata"),
                        )
                        db.add(sc)
                        await db.flush()

                    # 2. Create Work
                    ws_data = provider.parse_work_source(raw_metadata)
                    work = Work(
                        title=ws_data.get("title"),
                        description=ws_data.get("description"),
                        posted_at=ws_data.get("posted_at"),
                    )
                    db.add(work)
                    await db.flush()

                    # 3. Create WorkSource (skip if exists)
                    existing_ws = await db.execute(
                        select(WorkSource).where(
                            WorkSource.source == ws_data["source"],
                            WorkSource.source_work_id == ws_data["source_work_id"],
                        )
                    )
                    work_source = existing_ws.scalar_one_or_none()
                    if not work_source:
                        work_source = WorkSource(
                            work_id=work.id,
                            source=ws_data["source"],
                            source_work_id=ws_data["source_work_id"],
                            source_url=ws_data.get("source_url"),
                            source_creator_id=ws_data.get("source_creator_id"),
                            title=ws_data.get("title"),
                            description=ws_data.get("description"),
                            posted_at=ws_data.get("posted_at"),
                            raw_metadata=ws_data.get("raw_metadata"),
                        )
                        db.add(work_source)
                        await db.flush()

                    # 4. Create Assets
                    ad_list = provider.parse_assets(raw_metadata, [])
                    library_dir = Path(settings.library_root) / str(work.id)
                    library_dir.mkdir(parents=True, exist_ok=True)

                    for ad in ad_list:
                        asset = Asset(
                            file_name=f"{ad.get('source_asset_id', uuid4().hex)}.jpg",
                            file_path=f"library/{work.id}/{ad.get('source_asset_id', uuid4().hex)}",
                            width=ad.get("width"),
                            height=ad.get("height"),
                            mime_type="image/jpeg",
                        )
                        db.add(asset)
                        await db.flush()

                        existing_as = await db.execute(
                            select(AssetSource).where(
                                AssetSource.source == ad["source"],
                                AssetSource.source_asset_id == ad.get("source_asset_id"),
                            )
                        )
                        if not existing_as.scalar_one_or_none():
                            db.add(AssetSource(
                                asset_id=asset.id,
                                work_source_id=work_source.id,
                                source=ad["source"],
                                source_asset_id=ad.get("source_asset_id"),
                                source_url=ad.get("source_url"),
                                raw_metadata=ad.get("raw_metadata"),
                            ))
                            await db.flush()

                    # 5. Create Tags
                    try:
                        tag_list = provider.parse_source_tags(raw_metadata)
                        seen_tags = set()
                        for td in tag_list:
                            name = td.get("original_name", "").lower().strip()
                            if not name or name in seen_tags:
                                continue
                            seen_tags.add(name)

                            existing_t = await db.execute(select(Tag).where(Tag.normalized_name == name))
                            tag = existing_t.scalar_one_or_none()
                            if not tag:
                                tag = Tag(normalized_name=name, category=td.get("category"))
                                db.add(tag)
                                await db.flush()

                            # Skip if already linked
                            existing_wt = await db.execute(
                                select(WorkTag).where(WorkTag.work_id == work.id, WorkTag.tag_id == tag.id)
                            )
                            if not existing_wt.scalar_one_or_none():
                                db.add(WorkTag(work_id=work.id, tag_id=tag.id, source=download_job.source))

                            existing_wst = await db.execute(
                                select(WorkSourceTag).where(
                                    WorkSourceTag.work_source_id == work_source.id,
                                    WorkSourceTag.tag_id == tag.id,
                                )
                            )
                            if not existing_wst.scalar_one_or_none():
                                db.add(WorkSourceTag(
                                    work_source_id=work_source.id,
                                    tag_id=tag.id,
                                    source=td.get("source", download_job.source),
                                    original_name=td.get("original_name"),
                                ))
                    except NotImplementedError:
                        pass

                    imported += 1

                except Exception as e:
                    import traceback
                    import_job.status = "failed"
                    import_job.error_log = f"Error on {info_file.name}: {str(e)[:500]}"
                    await db.commit()
                    return

            import_job.status = "complete"
            download_job.status = "complete"
            await db.commit()

        except NotImplementedError:
            import_job.status = "failed"
            import_job.error_log = f"Import not implemented for: {download_job.source}"
            await db.commit()
        except Exception as e:
            import traceback
            import_job.status = "failed"
            import_job.error_log = f"{str(e)[:1000]}\n{traceback.format_exc()[-500:]}"
            await db.commit()

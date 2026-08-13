#!/usr/bin/env python3
"""Irreversibly redact a restored production snapshot before app startup."""

from __future__ import annotations

import asyncio

from sqlalchemy import inspect, text

from app.auth import hash_password
from app.database import async_session, engine


URL_COLUMNS = {"source_url", "profile_url", "avatar_url", "web_url"}
PATH_COLUMNS = {"file_path", "file_name", "download_dir", "gallerydl_config_path"}
EXTERNAL_ID_COLUMNS = {"source_creator_id", "source_work_id"}
TEXT_COLUMNS = {
    "title",
    "description",
    "display_name",
    "name",
    "username",
    "message",
    "reason",
}
SECRET_FRAGMENTS = ("password", "secret", "token", "cookie", "credential", "api_key")

async def main() -> None:
    async with engine.begin() as connection:
        tables = await connection.run_sync(lambda sync: inspect(sync).get_table_names())
        columns: dict[str, list[dict]] = {}
        for table in tables:
            columns[table] = await connection.run_sync(
                lambda sync, name=table: inspect(sync).get_columns(name)
            )

    async with async_session() as db:
        for table, definitions in columns.items():
            for definition in definitions:
                column = str(definition["name"])
                type_name = str(definition["type"]).lower()
                quoted_table = '"' + table.replace('"', '""') + '"'
                quoted_column = '"' + column.replace('"', '""') + '"'
                if "json" in type_name:
                    cast = "jsonb" if "jsonb" in type_name else "json"
                    await db.execute(
                        text(
                            f"UPDATE {quoted_table} SET {quoted_column} = '{{}}'::{cast} "
                            f"WHERE {quoted_column} IS NOT NULL"
                        )
                    )
                    continue
                if not any(kind in type_name for kind in ("char", "text")):
                    continue
                if column in URL_COLUMNS or column.endswith("_url"):
                    expression = (
                        "'https://example.invalid/' || "
                        f"md5(coalesce({quoted_column}, ''))"
                    )
                elif column in PATH_COLUMNS or column.endswith("_path"):
                    expression = (
                        "'acceptance/' || md5(coalesce("
                        f"{quoted_column}, '')) || '.fixture'"
                    )
                elif column in EXTERNAL_ID_COLUMNS:
                    expression = f"'fixture-id-' || md5(coalesce({quoted_column}, ''))"
                elif any(fragment in column.lower() for fragment in SECRET_FRAGMENTS):
                    expression = "'redacted'"
                elif column in TEXT_COLUMNS and table not in {"alembic_version"}:
                    expression = f"'fixture-' || substr(md5(coalesce({quoted_column}, '')), 1, 16)"
                else:
                    continue
                await db.execute(
                    text(
                        f"UPDATE {quoted_table} SET {quoted_column} = {expression} "
                        f"WHERE {quoted_column} IS NOT NULL"
                    )
                )

        if "system_settings" in tables:
            await db.execute(text("UPDATE system_settings SET value = '{}'::jsonb"))
        for table in ("subscriptions", "subscription_sources"):
            if table in tables and any(item["name"] == "is_active" for item in columns[table]):
                await db.execute(text(f"UPDATE {table} SET is_active = false"))
        for table in (
            "gitllery_projection_targets",
            "gitllery_projection_outbox",
            "gitllery_repository_state",
            "gitllery_builds",
            "import_curation_outbox",
            "media_derivative_outbox",
            "asset_dedup_outbox",
            "search_projection_outbox",
        ):
            if table in tables:
                await db.execute(text(f'DELETE FROM "{table}"'))
        if "storage_artifacts" in tables:
            await db.execute(
                text(
                    "UPDATE storage_artifacts SET state='done', attempts=0, "
                    "lease_token=NULL, lease_expires_at=NULL, last_error=NULL"
                )
            )
        if "download_jobs" in tables:
            await db.execute(
                text(
                    "UPDATE download_jobs SET status='failed', last_heartbeat_at=NULL, "
                    "worker_pid=NULL WHERE status NOT IN ('complete','failed','cancelled')"
                )
            )
        if "import_jobs" in tables:
            await db.execute(
                text(
                    "UPDATE import_jobs SET status='failed', last_heartbeat_at=NULL, "
                    "worker_pid=NULL, execution_token=NULL "
                    "WHERE status NOT IN ('complete','failed','cancelled')"
                )
            )
        if "task_runs" in tables:
            await db.execute(
                text(
                    "UPDATE task_runs SET status='failed', resource_state='yielded', "
                    "last_heartbeat_at=NULL WHERE status NOT IN "
                    "('complete','failed','cancelled')"
                )
            )
        if "users" in tables:
            await db.execute(text("DELETE FROM users"))
            await db.execute(
                text(
                    """
                    INSERT INTO users (
                      username, password_hash, display_name, is_active,
                      must_change_password, is_admin, permissions, preferences,
                      nsfw_visible, upload_used_bytes, created_at, updated_at
                    ) VALUES (
                      'acceptance-admin', :password_hash, 'Acceptance Admin', true,
                      false, true, '[]'::jsonb, '{}'::jsonb, true, 0, now(), now()
                    )
                    """
                ),
                {"password_hash": hash_password("acceptance-only-password")},
            )
        await db.commit()
    print("sanitized restored snapshot and disabled automatic subscriptions")


if __name__ == "__main__":
    asyncio.run(main())

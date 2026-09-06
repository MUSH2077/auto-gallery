from __future__ import annotations

import asyncio
import socket
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from app.services.creator_aliases import (
    AliasObservation,
    backfill_all_creator_aliases,
    backfill_creator_alias_batch,
    collect_creator_alias_observations,
    extract_danbooru_note_aliases,
    list_creator_aliases,
    normalize_creator_alias,
    observe_creator_aliases,
)


def test_creator_alias_migration_extends_current_head_with_required_contract():
    migration = (
        Path(__file__).resolve().parents[1]
        / "alembic"
        / "versions"
        / "c9e1a3b5d7f2_add_creator_aliases.py"
    ).read_text()

    assert 'revision = "c9e1a3b5d7f2"' in migration
    assert 'down_revision = "b8d0f2a4c6e9"' in migration
    assert '"creator_aliases"' in migration
    assert 'name="uq_creator_aliases_identity"' in migration
    assert 'ondelete="CASCADE"' in migration


def test_admin_backfill_endpoint_enqueues_registered_resumable_operation(monkeypatch):
    from app.api.admin import data as admin_data
    from app.services.operations import ADMIN_OPERATION_REGISTRY

    captured = {}

    async def fake_enqueue(**kwargs):
        captured.update(kwargs)
        return {"status": "enqueued", "job_id": "alias-job"}

    monkeypatch.setattr("app.services.operations.enqueue_admin_operation", fake_enqueue)

    result = asyncio.run(admin_data.backfill_creator_aliases_operation())

    assert result["job_id"] == "alias-job"
    assert captured["operation_type"] == "admin-creator-alias-backfill"
    assert captured["lock_key"] == "library:creator-alias-backfill:active"
    spec = ADMIN_OPERATION_REGISTRY["admin-creator-alias-backfill"]
    assert spec.required_permission == "system"
    assert spec.queue_names == frozenset({"maintenance"})


def test_registered_dispatch_routes_creator_alias_backfill(monkeypatch):
    from app.jobs import admin_operations

    calls = []

    async def fake_run(task_id, options):
        calls.append((task_id, options))
        return {"scanned": 812}

    monkeypatch.setattr(
        admin_operations,
        "_run_creator_alias_backfill_operation",
        fake_run,
        raising=False,
    )

    result = asyncio.run(
        admin_operations._execute_registered_admin_operation(
            "admin-creator-alias-backfill",
            "task-id",
            1,
            {"resume": True},
        )
    )

    assert result == {"scanned": 812}
    assert calls == [("task-id", {"resume": True})]


def test_alias_backfill_handler_runs_resumable_service(monkeypatch):
    from app.jobs import admin_operations
    from app.services import creator_aliases

    session = object()

    class SessionContext:
        async def __aenter__(self):
            return session

        async def __aexit__(self, *_args):
            return False

    async def fake_backfill(db, **kwargs):
        assert db is session
        assert kwargs["request_projection"] is True
        return {"scanned": 812, "total": 812, "pages": 9}

    monkeypatch.setattr(admin_operations, "async_session", SessionContext)
    monkeypatch.setattr(creator_aliases, "backfill_all_creator_aliases", fake_backfill)

    result = asyncio.run(
        admin_operations._run_creator_alias_backfill_operation("task-id", {})
    )

    assert result["message"] == "Creator alias backfill complete"
    assert result["scanned"] == 812


def test_alias_normalization_nfkc_casefolds_and_collapses_whitespace():
    assert normalize_creator_alias("  ＡＡＫＩＮ\t 5349  ", kind="name") == "aakin 5349"


def test_account_normalization_ignores_one_optional_at_prefix():
    assert normalize_creator_alias(" @User_DSNJ5842 ", kind="account") == "user_dsnj5842"
    assert normalize_creator_alias("user_dsnj5842", kind="account") == "user_dsnj5842"


def test_alias_observation_rejects_blank_normalized_values():
    try:
        AliasObservation(source="pixiv", kind="account", value="  @  ")
    except ValueError as exc:
        assert str(exc) == "Creator alias value must not be empty."
    else:
        raise AssertionError("blank account aliases must be rejected")


def test_danbooru_machine_note_parser_accepts_only_the_generated_format():
    assert extract_danbooru_note_aliases(
        "Danbooru artist tag: aakin5349 (aka: AA𝙠𝙞𝙣, aakin)"
    ) == ("aakin5349", "AA𝙠𝙞𝙣", "aakin")
    assert extract_danbooru_note_aliases(
        "Danbooru artist tag: airfish_(lefko_d) (aka: airfish, 空气鱼(artist))"
    ) == ("airfish_(lefko_d)", "airfish", "空气鱼(artist)")
    assert extract_danbooru_note_aliases(
        "Danbooru artist tag: diffusyaga"
    ) == ("diffusyaga",)
    assert extract_danbooru_note_aliases("aka: free-form, text") == ()
    assert extract_danbooru_note_aliases(
        "Danbooru artist tag: malformed free text"
    ) == ()
    assert extract_danbooru_note_aliases("Danbooru artist tag: broken (aka: )") == ()


def test_alias_collection_covers_local_source_url_handle_and_danbooru_names():
    creator = SimpleNamespace(
        id="creator-id",
        name="aakin5349",
        display_name="AAkin",
    )
    source_creators = (
        SimpleNamespace(
            id="source-creator-id",
            source="pixiv",
            source_creator_id="108990866",
            source_url="https://www.pixiv.net/users/108990866",
            display_name="AAkin Pixiv",
            raw_metadata={
                "account": "user_dsnj5842",
                "username": "user_dsnj5842",
            },
        ),
        SimpleNamespace(
            id="invalid-source-creator-url",
            source="lofter",
            source_creator_id=None,
            source_url="https://www.lofter.com/",
            display_name=None,
            raw_metadata={},
        ),
    )
    links = (
        SimpleNamespace(
            id="stacc-link",
            url="https://www.pixiv.net/stacc/user_dsnj5842",
            source="danbooru",
            notes="Danbooru artist tag: aakin5349 (aka: AA𝙠𝙞𝙣, aakin)",
        ),
    )
    repositories = (
        SimpleNamespace(
            id="repository-id",
            source="pixiv",
            source_creator_id="108990866",
            source_url="https://www.pixiv.net/users/108990866",
        ),
    )

    observations = collect_creator_alias_observations(
        creator,
        source_creators=source_creators,
        links=links,
        subscription_sources=repositories,
    )
    keys = {
        (item.source, item.kind, normalize_creator_alias(item.value, kind=item.kind))
        for item in observations
    }

    assert ("local", "name", "aakin5349") in keys
    assert ("local", "name", "aakin") in keys
    assert ("pixiv", "account", "user_dsnj5842") in keys
    assert ("pixiv", "source_id", "108990866") in keys
    assert (
        "pixiv",
        "url",
        "https://www.pixiv.net/stacc/user_dsnj5842",
    ) in keys
    assert ("pixiv", "url_handle", "user_dsnj5842") in keys
    assert ("danbooru", "other_name", "aaKIN".casefold()) in keys
    assert ("lofter", "url", "https://www.lofter.com/") not in keys
    assert len(keys) == len(observations)


def test_pixiv_stacc_url_is_classified_as_a_creator_identity():
    from app.services.source_search_identity import parse_source_url

    parsed = parse_source_url("https://www.pixiv.net/stacc/User_DSNJ5842")

    assert parsed is not None
    assert parsed.source == "pixiv"
    assert parsed.kind == "creator"
    assert parsed.normalized_url == "https://www.pixiv.net/stacc/User_DSNJ5842"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_observation_is_idempotent_retains_history_and_allows_conflicts():
    from app.database import async_session
    from app.models import Creator, CreatorAlias

    try:
        async with async_session() as db:
            transaction = await db.begin()
            first = Creator(name="alias-test-first")
            second = Creator(name="alias-test-second")
            db.add_all((first, second))
            await db.flush()

            account_ref = "source_creator:pixiv/108990866:account"
            original = AliasObservation(
                source="pixiv",
                kind="account",
                value="@User_DSNJ5842",
                source_ref=account_ref,
            )
            await observe_creator_aliases(db, first.id, (original,))
            await observe_creator_aliases(db, first.id, (original,))
            await observe_creator_aliases(
                db,
                first.id,
                (
                    AliasObservation(
                        source="pixiv",
                        kind="account",
                        value="New_User_Name",
                        source_ref=account_ref,
                    ),
                ),
            )
            await observe_creator_aliases(db, second.id, (original,))
            await db.flush()

            rows = list(
                (
                    await db.execute(
                        select(CreatorAlias).order_by(
                            CreatorAlias.creator_id,
                            CreatorAlias.normalized_value,
                        )
                    )
                ).scalars()
            )
            first_rows = [row for row in rows if row.creator_id == first.id]
            second_rows = [row for row in rows if row.creator_id == second.id]

            assert [(row.normalized_value, row.is_current) for row in first_rows] == [
                ("new_user_name", True),
                ("user_dsnj5842", False),
            ]
            assert [(row.normalized_value, row.is_current) for row in second_rows] == [
                ("user_dsnj5842", True)
            ]
            assert first_rows[1].last_seen_at >= first_rows[1].first_seen_at
            await transaction.rollback()
    except (OSError, socket.gaierror) as exc:
        pytest.skip(f"PostgreSQL is unavailable from the host test runner: {exc}")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_backfill_collects_stored_identities_and_reports_malformed_notes():
    from app.database import async_session
    from app.models import Creator, CreatorAlias, CreatorLink, SourceCreator

    try:
        async with async_session() as db:
            transaction = await db.begin()
            creator = Creator(name="aakin5349", display_name="AAkin")
            db.add(creator)
            await db.flush()
            db.add_all(
                (
                    CreatorAlias(
                        creator_id=creator.id,
                        value="https://www.lofter.com/",
                        normalized_value="https://www.lofter.com/",
                        source="lofter",
                        kind="url",
                        source_ref="source_creator:stale-root:url",
                    ),
                    SourceCreator(
                        creator_id=creator.id,
                        source="pixiv",
                        source_creator_id="108990866",
                        source_url="https://www.pixiv.net/users/108990866",
                        display_name="AAkin",
                        raw_metadata={"account": "user_dsnj5842"},
                    ),
                    CreatorLink(
                        creator_id=creator.id,
                        url="https://www.pixiv.net/stacc/user_dsnj5842",
                        link_type="source",
                        source="danbooru",
                        notes="Danbooru artist tag: aakin5349 (aka: AA𝙠𝙞𝙣, aakin)",
                    ),
                    CreatorLink(
                        creator_id=creator.id,
                        url="https://example.invalid/free-form",
                        link_type="other",
                        source="manual",
                        notes="Danbooru artist tag: malformed free text",
                    ),
                )
            )
            await db.flush()

            first = await backfill_creator_alias_batch(
                db,
                (creator.id,),
                request_projection=False,
            )
            await db.flush()
            first_count = int(
                await db.scalar(
                    select(func.count())
                    .select_from(CreatorAlias)
                    .where(CreatorAlias.creator_id == creator.id)
                )
                or 0
            )
            rows_before = list(
                (
                    await db.execute(
                        select(CreatorAlias).where(CreatorAlias.creator_id == creator.id)
                    )
                ).scalars()
            )
            second = await backfill_creator_alias_batch(
                db,
                (creator.id,),
                request_projection=False,
            )
            rows_after = list(
                (
                    await db.execute(
                        select(CreatorAlias).where(CreatorAlias.creator_id == creator.id)
                    )
                ).scalars()
            )

            assert first_count >= 4
            assert first["creators"] == second["creators"] == 1
            assert first["malformed_danbooru_notes"] == 1
            assert {row.normalized_value for row in rows_before} >= {
                "user_dsnj5842",
                "108990866",
                "aakin5349",
                "aakin",
            }
            assert "https://www.lofter.com/" not in {
                row.normalized_value for row in rows_before
            }
            assert len(rows_after) == len(rows_before)
            await transaction.rollback()
    except (OSError, socket.gaierror) as exc:
        pytest.skip(f"PostgreSQL is unavailable from the host test runner: {exc}")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_snapshot_refresh_marks_removed_link_identity_historical():
    from app.database import async_session
    from app.models import Creator, CreatorAlias, CreatorLink

    try:
        async with async_session() as db:
            transaction = await db.begin()
            creator = Creator(name="alias-link-history")
            db.add(creator)
            await db.flush()
            link = CreatorLink(
                creator_id=creator.id,
                url="https://www.pixiv.net/stacc/old_handle",
                link_type="source",
                source="danbooru",
            )
            db.add(link)
            await db.flush()
            await backfill_creator_alias_batch(
                db,
                (creator.id,),
                request_projection=False,
            )
            await db.delete(link)
            await db.flush()
            await backfill_creator_alias_batch(
                db,
                (creator.id,),
                request_projection=False,
            )

            historical = (
                await db.execute(
                    select(CreatorAlias).where(
                        CreatorAlias.creator_id == creator.id,
                        CreatorAlias.normalized_value == "old_handle",
                    )
                )
            ).scalar_one()
            assert historical.kind == "url_handle"
            assert historical.is_current is False
            await transaction.rollback()
    except (OSError, socket.gaierror) as exc:
        pytest.skip(f"PostgreSQL is unavailable from the host test runner: {exc}")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_creator_link_crud_observes_aliases_in_the_same_mutation():
    from app.database import async_session
    from app.models import Creator, CreatorAlias
    from app.services.creator import CreatorService

    try:
        async with async_session() as db:
            creator = Creator(name=f"alias-crud-{id(db)}")
            db.add(creator)
            await db.commit()
            creator_id = creator.id

            service = CreatorService(db)
            link = await service.add_link(
                {
                    "creator_id": creator_id,
                    "url": "https://www.pixiv.net/stacc/user_dsnj5842",
                    "link_type": "source",
                    "source": "danbooru",
                }
            )
            observed = (
                await db.execute(
                    select(CreatorAlias).where(
                        CreatorAlias.creator_id == creator_id,
                        CreatorAlias.kind == "url_handle",
                        CreatorAlias.normalized_value == "user_dsnj5842",
                    )
                )
            ).scalar_one_or_none()
            assert observed is not None and observed.is_current is True

            await service.delete_link(link.id)
            await db.refresh(observed)
            assert observed.is_current is False

            await db.delete(creator)
            await db.commit()
    except (OSError, socket.gaierror) as exc:
        pytest.skip(f"PostgreSQL is unavailable from the host test runner: {exc}")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_disk_identity_refresh_preserves_previous_account_as_history():
    from app.database import async_session
    from app.models import Creator, CreatorAlias, SourceCreator
    from app.services.disk_identity import (
        MetadataIdentity,
        provision_identity_for_disk_import,
    )

    try:
        async with async_session() as db:
            transaction = await db.begin()
            creator = Creator(name="alias-account-history")
            db.add(creator)
            await db.flush()
            db.add(
                SourceCreator(
                    creator_id=creator.id,
                    source="pixiv",
                    source_creator_id="108990866",
                    source_url="https://www.pixiv.net/users/108990866",
                    raw_metadata={"account": "old_account"},
                )
            )
            await db.flush()
            await backfill_creator_alias_batch(
                db,
                (creator.id,),
                request_projection=False,
            )

            await provision_identity_for_disk_import(
                db,
                MetadataIdentity(
                    source="pixiv",
                    source_creator_id="108990866",
                    source_url="https://www.pixiv.net/users/108990866",
                    display_name="AAkin",
                    raw_metadata={"account": "new_account"},
                    creator_dir="new_account",
                ),
                prefer_danbooru=False,
            )

            accounts = list(
                (
                    await db.execute(
                        select(CreatorAlias)
                        .where(
                            CreatorAlias.creator_id == creator.id,
                            CreatorAlias.source == "pixiv",
                            CreatorAlias.kind == "account",
                        )
                        .order_by(CreatorAlias.normalized_value)
                    )
                ).scalars()
            )
            assert [(row.normalized_value, row.is_current) for row in accounts] == [
                ("new_account", True),
                ("old_account", False),
            ]
            await transaction.rollback()
    except (OSError, socket.gaierror) as exc:
        pytest.skip(f"PostgreSQL is unavailable from the host test runner: {exc}")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_aliases_project_to_creator_subscription_repository_and_work_documents():
    from app.database import async_session
    from app.models import (
        Creator,
        CreatorLink,
        SourceCreator,
        Subscription,
        SubscriptionSource,
        Work,
        WorkSource,
    )
    from app.services.search import SearchService

    try:
        async with async_session() as db:
            transaction = await db.begin()
            creator = Creator(name="aakin5349", display_name="AAkin")
            db.add(creator)
            await db.flush()
            source_creator = SourceCreator(
                creator_id=creator.id,
                source="pixiv",
                source_creator_id="108990866",
                raw_metadata={"account": "user_dsnj5842"},
            )
            subscription = Subscription(creator_id=creator.id, name="Custom label")
            work = Work(title="Alias projection work")
            db.add_all((source_creator, subscription, work))
            await db.flush()
            repository = SubscriptionSource(
                subscription_id=subscription.id,
                source="pixiv",
                source_creator_id="108990866",
                source_url="https://www.pixiv.net/users/108990866",
            )
            work_source = WorkSource(
                work_id=work.id,
                source="pixiv",
                source_work_id="123456",
                source_creator_id="108990866",
            )
            db.add_all((repository, work_source))
            await db.flush()
            await backfill_creator_alias_batch(
                db,
                (creator.id,),
                request_projection=False,
            )

            service = SearchService(db)
            creator_doc = (await service._build_creator_documents((creator.id,)))[0]
            subscription_doc = (
                await service._build_subscription_documents((subscription.id,))
            )[0]
            repository_doc = (
                await service._build_repository_documents((repository.id,))
            )[0]
            work_doc = (await service._build_work_documents((work.id,)))[0]

            for document in (
                creator_doc,
                subscription_doc,
                repository_doc,
                work_doc,
            ):
                assert "user_dsnj5842" in document["alias_identities_current"]
                assert any(
                    record["creator_id"] == str(creator.id)
                    and record["kind"] == "account"
                    for record in document["alias_records"]
                )
            await transaction.rollback()
    except (OSError, socket.gaierror) as exc:
        pytest.skip(f"PostgreSQL is unavailable from the host test runner: {exc}")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_exact_account_search_is_postgres_authoritative_across_creator_surfaces(monkeypatch):
    from app.database import async_session
    from app.models import (
        Creator,
        CreatorLink,
        SourceCreator,
        Subscription,
        SubscriptionSource,
        Work,
        WorkSource,
    )
    from app.services.search import SearchService

    try:
        async with async_session() as db:
            transaction = await db.begin()
            creator = Creator(name="aakin5349", display_name="AAkin")
            db.add(creator)
            await db.flush()
            subscription = Subscription(creator_id=creator.id, name="AAkin feed")
            work = Work(title="Exact alias work")
            db.add_all((subscription, work))
            await db.flush()
            repository = SubscriptionSource(
                subscription_id=subscription.id,
                source="pixiv",
                source_creator_id="108990866",
                source_url="https://www.pixiv.net/users/108990866",
            )
            db.add_all(
                (
                    SourceCreator(
                        creator_id=creator.id,
                        source="pixiv",
                        source_creator_id="108990866",
                        raw_metadata={"account": "user_dsnj5842"},
                    ),
                    CreatorLink(
                        creator_id=creator.id,
                        url="https://www.pixiv.net/stacc/user_dsnj5842",
                        link_type="source",
                        source="pixiv",
                    ),
                    repository,
                    WorkSource(
                        work_id=work.id,
                        source="pixiv",
                        source_work_id="exact-alias-work",
                        source_creator_id="108990866",
                    ),
                )
            )
            await db.flush()
            await backfill_creator_alias_batch(
                db,
                (creator.id,),
                request_projection=False,
            )
            await observe_creator_aliases(
                db,
                creator.id,
                (
                    AliasObservation(
                        source="danbooru",
                        kind="other_name",
                        value="sample_alias_(circle)",
                        source_ref="test:danbooru-parenthesized-alias",
                    ),
                ),
                request_projection=False,
            )

            async def fail_if_indexed(*_args, **_kwargs):
                raise AssertionError("an exact stored identity must bypass Meilisearch")

            service = SearchService(db)
            monkeypatch.setattr(service, "_search_meili", fail_if_indexed)
            result = await service.search(
                "@USER_DSNJ5842",
                scope="global",
                permissions={"library", "subscriptions"},
            )

            assert [item["id"] for item in result["groups"]["creators"]["items"]] == [
                str(creator.id)
            ]
            assert [item["id"] for item in result["groups"]["subscriptions"]["items"]] == [
                str(subscription.id)
            ]
            assert [item["id"] for item in result["groups"]["repositories"]["items"]] == [
                str(repository.id)
            ]
            assert [item["id"] for item in result["groups"]["works"]["items"]] == [
                str(work.id)
            ]
            for group in result["groups"].values():
                for item in group["items"]:
                    assert item["matched_identity"] == {
                        "creator_id": str(creator.id),
                        "value": "user_dsnj5842",
                        "source": "pixiv",
                        "kind": "account",
                        "is_current": True,
                        "match_type": "exact",
                    }

            creator_filtered = await service.search(
                "creator:user_dsnj5842",
                scope="creators",
                permissions={"library"},
            )
            assert creator_filtered["groups"]["creators"]["items"][0]["id"] == str(
                creator.id
            )
            stacc = await service.search(
                "https://www.pixiv.net/stacc/user_dsnj5842",
                scope="global",
                permissions={"library", "subscriptions"},
            )
            assert stacc["canonical_query"] == (
                'url:"https://www.pixiv.net/stacc/user_dsnj5842"'
            )
            assert stacc["groups"]["creators"]["items"][0]["id"] == str(
                creator.id
            )
            parenthesized = await service.search(
                "sample_alias_(circle)",
                scope="creators",
                permissions={"library"},
            )
            assert parenthesized["groups"]["creators"]["items"][0]["id"] == str(
                creator.id
            )
            assert parenthesized["groups"]["creators"]["items"][0][
                "matched_identity"
            ]["value"] == "sample_alias_(circle)"
            await transaction.rollback()
    except (OSError, socket.gaierror) as exc:
        pytest.skip(f"PostgreSQL is unavailable from the host test runner: {exc}")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_exact_alias_conflict_returns_all_candidates_with_current_first(monkeypatch):
    from app.database import async_session
    from app.models import Creator
    from app.services.search import SearchService

    try:
        async with async_session() as db:
            transaction = await db.begin()
            current = Creator(name="z-current-alias-owner")
            historical = Creator(name="a-historical-alias-owner")
            db.add_all((current, historical))
            await db.flush()
            await observe_creator_aliases(
                db,
                current.id,
                (
                    AliasObservation(
                        source="pixiv",
                        kind="account",
                        value="shared_handle",
                        source_ref="test:current",
                    ),
                ),
                request_projection=False,
            )
            await observe_creator_aliases(
                db,
                historical.id,
                (
                    AliasObservation(
                        source="pixiv",
                        kind="account",
                        value="shared_handle",
                        source_ref="test:historical",
                        is_current=False,
                    ),
                ),
                request_projection=False,
            )

            async def fail_if_indexed(*_args, **_kwargs):
                raise AssertionError("exact conflicts remain PostgreSQL authoritative")

            service = SearchService(db)
            monkeypatch.setattr(service, "_search_meili", fail_if_indexed)
            result = await service.search(
                "shared_handle",
                scope="creators",
                permissions={"library"},
                limit=20,
            )

            items = result["groups"]["creators"]["items"]
            assert [item["id"] for item in items] == [
                str(current.id),
                str(historical.id),
            ]
            assert [item["matched_identity"]["is_current"] for item in items] == [
                True,
                False,
            ]
            await transaction.rollback()
    except (OSError, socket.gaierror) as exc:
        pytest.skip(f"PostgreSQL is unavailable from the host test runner: {exc}")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_full_backfill_walks_a_fixed_snapshot_in_keyset_pages():
    from app.database import async_session
    from app.models import Creator, CreatorAlias, SourceCreator

    try:
        async with async_session() as db:
            transaction = await db.begin()
            creator = Creator(name=f"alias-full-{id(db)}")
            db.add(creator)
            await db.flush()
            db.add(
                SourceCreator(
                    creator_id=creator.id,
                    source="pixiv",
                    source_creator_id="108990866",
                    raw_metadata={"account": "user_dsnj5842"},
                )
            )
            await db.flush()

            result = await backfill_all_creator_aliases(
                db,
                page_size=1,
                request_projection=False,
            )

            aliases = list(
                (
                    await db.execute(
                        select(CreatorAlias).where(CreatorAlias.creator_id == creator.id)
                    )
                ).scalars()
            )
            assert result["scanned"] == result["total"]
            assert result["pages"] == result["total"]
            assert any(row.normalized_value == "user_dsnj5842" for row in aliases)
            await transaction.rollback()
    except (OSError, socket.gaierror) as exc:
        pytest.skip(f"PostgreSQL is unavailable from the host test runner: {exc}")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_alias_read_filters_history_and_orders_current_before_historical():
    from app.database import async_session
    from app.models import Creator

    try:
        async with async_session() as db:
            transaction = await db.begin()
            creator = Creator(name=f"alias-read-{id(db)}")
            db.add(creator)
            await db.flush()
            await observe_creator_aliases(
                db,
                creator.id,
                (
                    AliasObservation(
                        source="pixiv",
                        kind="account",
                        value="current_handle",
                        source_ref="test:current",
                    ),
                    AliasObservation(
                        source="danbooru",
                        kind="other_name",
                        value="old_alias",
                        source_ref="test:history",
                        is_current=False,
                    ),
                ),
                request_projection=False,
            )

            current = await list_creator_aliases(
                db,
                creator.id,
                include_history=False,
            )
            all_aliases = await list_creator_aliases(
                db,
                creator.id,
                include_history=True,
            )

            assert [item.value for item in current] == ["current_handle"]
            assert [item.value for item in all_aliases] == [
                "current_handle",
                "old_alias",
            ]
            await transaction.rollback()
    except (OSError, socket.gaierror) as exc:
        pytest.skip(f"PostgreSQL is unavailable from the host test runner: {exc}")

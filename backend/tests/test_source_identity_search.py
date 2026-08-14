from __future__ import annotations

import socket
from uuid import uuid4

import pytest


@pytest.mark.integration
@pytest.mark.asyncio
async def test_source_identity_search_is_authoritative_and_source_paired(monkeypatch):
    from app.database import async_session
    from app.models import (
        Creator,
        SourceCreator,
        Subscription,
        SubscriptionSource,
        Work,
        WorkSource,
    )
    from app.services.search import SearchService

    seed = str(uuid4().int)[:12]
    creator_identity = seed
    work_identity = str(int(seed) + 1)

    try:
        async with async_session() as db:
            transaction = await db.begin()
            pixiv_creator = Creator(name=f"pixiv-{seed}", display_name="Pixiv Exact")
            x_creator = Creator(name=f"x-{seed}", display_name="X Exact")
            db.add_all((pixiv_creator, x_creator))
            await db.flush()

            pixiv_profile = f"https://www.pixiv.net/users/{creator_identity}"
            db.add(
                SourceCreator(
                    creator_id=x_creator.id,
                    source="x",
                    source_creator_id=creator_identity,
                    source_url=f"https://x.com/x_{seed}",
                )
            )

            pixiv_subscription = Subscription(creator_id=pixiv_creator.id, name="Pixiv Exact")
            x_subscription = Subscription(creator_id=x_creator.id, name="X Exact")
            db.add_all((pixiv_subscription, x_subscription))
            await db.flush()
            pixiv_repository = SubscriptionSource(
                subscription_id=pixiv_subscription.id,
                source="pixiv",
                source_creator_id=creator_identity,
                source_url=pixiv_profile,
            )
            x_repository = SubscriptionSource(
                subscription_id=x_subscription.id,
                source="x",
                source_creator_id=creator_identity,
                source_url=f"https://x.com/x_{seed}",
            )
            db.add_all((pixiv_repository, x_repository))

            pixiv_work = Work(title="Pixiv paired work")
            x_work = Work(title="X paired work")
            db.add_all((pixiv_work, x_work))
            await db.flush()
            pixiv_work_url = f"https://www.pixiv.net/artworks/{work_identity}"
            db.add_all((
                WorkSource(
                    work_id=pixiv_work.id,
                    source="pixiv",
                    source_work_id=work_identity,
                    source_creator_id=creator_identity,
                    source_url=pixiv_work_url,
                ),
                WorkSource(
                    work_id=x_work.id,
                    source="x",
                    source_work_id=work_identity,
                    source_creator_id=creator_identity,
                    source_url=f"https://x.com/x_{seed}/status/{work_identity}",
                ),
            ))
            await db.flush()

            async def fail_if_indexed(*_args, **_kwargs):
                raise AssertionError("pure identity searches must not require Meilisearch")

            service = SearchService(db)
            monkeypatch.setattr(service, "_search_meili", fail_if_indexed)
            permissions = {"library", "subscriptions"}

            uid_result = await service.search(
                f"uid:pixiv/{creator_identity}",
                scope="global",
                permissions=permissions,
            )
            assert [item["id"] for item in uid_result["groups"]["works"]["items"]] == [str(pixiv_work.id)]
            assert [item["id"] for item in uid_result["groups"]["creators"]["items"]] == [str(pixiv_creator.id)]
            assert [item["id"] for item in uid_result["groups"]["repositories"]["items"]] == [str(pixiv_repository.id)]
            assert [item["id"] for item in uid_result["groups"]["subscriptions"]["items"]] == [str(pixiv_subscription.id)]
            assert uid_result["groups"]["creators"]["items"][0]["source_creator_keys"] == [
                f"pixiv/{creator_identity}"
            ]

            repeated_uid_result = await service.search(
                f"uid:pixiv/{creator_identity} uid:x/{creator_identity}",
                scope="works",
                permissions=permissions,
            )
            assert {item["id"] for item in repeated_uid_result["groups"]["works"]["items"]} == {
                str(pixiv_work.id),
                str(x_work.id),
            }

            combined_result = await service.search(
                f"uid:pixiv/{creator_identity} pid:pixiv/{work_identity}",
                scope="works",
                permissions=permissions,
            )
            assert [item["id"] for item in combined_result["groups"]["works"]["items"]] == [
                str(pixiv_work.id)
            ]

            pid_result = await service.search(
                f"pid:pixiv/{work_identity}",
                scope="global",
                permissions=permissions,
            )
            assert tuple(pid_result["groups"]) == ("works",)
            assert [item["id"] for item in pid_result["groups"]["works"]["items"]] == [str(pixiv_work.id)]

            profile_result = await service.search(
                pixiv_profile,
                scope="global",
                permissions=permissions,
            )
            assert profile_result["canonical_query"] == f'url:"{pixiv_profile}"'
            assert [item["id"] for item in profile_result["groups"]["works"]["items"]] == [str(pixiv_work.id)]

            work_url_result = await service.search(
                pixiv_work_url,
                scope="global",
                permissions=permissions,
            )
            assert tuple(work_url_result["groups"]) == ("works",)
            assert [item["id"] for item in work_url_result["groups"]["works"]["items"]] == [str(pixiv_work.id)]
            await transaction.rollback()
    except (OSError, socket.gaierror) as exc:
        pytest.skip(f"PostgreSQL is unavailable from the host test runner: {exc}")

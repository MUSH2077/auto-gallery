from __future__ import annotations

from datetime import datetime, timedelta, timezone
import socket
from uuid import uuid4

import pytest


def _ids_in_order(items, wanted):
    wanted_set = {str(value) for value in wanted}
    return [item["id"] for item in items if item["id"] in wanted_set]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reference_lists_share_authoritative_name_order_and_private_filters(
    monkeypatch,
):
    """Reintroducing Meili or custom-title subscription sorting must fail."""
    from app.database import async_session
    from app.models import Creator, Subscription, User, UserSubscription
    from app.services.search import SearchService

    marker = uuid4().hex
    now = datetime.now(timezone.utc)
    try:
        async with async_session() as db:
            transaction = await db.begin()
            user = User(
                username=f"reference-{marker}",
                password_hash="test",
                permissions=["library", "subscriptions"],
            )
            alpha = Creator(
                name=f"zz-internal-{marker}",
                display_name="Ａlpha",
                is_active=False,
                created_at=now - timedelta(days=3),
                updated_at=now - timedelta(hours=1),
            )
            beta = Creator(
                name=f"aa-internal-{marker}",
                display_name="Beta",
                is_active=False,
                created_at=now - timedelta(days=2),
                updated_at=now - timedelta(hours=3),
            )
            db.add_all((user, alpha, beta))
            await db.flush()

            alpha_subscription = Subscription(
                creator_id=alpha.id,
                name="Zulu custom title",
                is_active=True,
                sync_enabled=True,
                created_at=now - timedelta(days=1),
                updated_at=now - timedelta(hours=1),
            )
            beta_subscription = Subscription(
                creator_id=beta.id,
                name="Able custom title",
                is_active=True,
                sync_enabled=True,
                created_at=now,
                updated_at=now - timedelta(hours=2),
            )
            db.add_all((alpha_subscription, beta_subscription))
            await db.flush()
            memberships = (
                UserSubscription(
                    user_id=user.id,
                    subscription_id=alpha_subscription.id,
                    name="Zulu custom title",
                    is_active=False,
                    sync_enabled=False,
                    schedule_mode="manual",
                    created_at=now - timedelta(days=1),
                ),
                UserSubscription(
                    user_id=user.id,
                    subscription_id=beta_subscription.id,
                    name="Able custom title",
                    is_active=False,
                    sync_enabled=False,
                    schedule_mode="manual",
                    created_at=now,
                ),
            )
            db.add_all(memberships)
            await db.flush()

            async def fail_if_indexed(*_args, **_kwargs):
                raise AssertionError(
                    "structured creator/subscription browsing must use PostgreSQL"
                )

            service = SearchService(db)
            monkeypatch.setattr(service, "_search_meili", fail_if_indexed)

            creator_result = await service.search(
                "is:inactive sort:name-asc",
                scope="creators",
                permissions={"library"},
                limit=100,
            )
            default_creator_result = await service.search(
                "",
                scope="creators",
                permissions={"library"},
                limit=100,
            )
            default_subscription_result = await service.search(
                "",
                scope="subscriptions",
                permissions={"subscriptions"},
                allowed_subscription_ids={
                    alpha_subscription.id,
                    beta_subscription.id,
                },
                user_id=user.id,
                limit=100,
            )
            subscription_result = await service.search(
                "is:inactive sort:name-asc",
                scope="subscriptions",
                permissions={"subscriptions"},
                allowed_subscription_ids={
                    alpha_subscription.id,
                    beta_subscription.id,
                },
                user_id=user.id,
                limit=100,
            )

            expected_creators = [str(alpha.id), str(beta.id)]
            assert _ids_in_order(
                creator_result["groups"]["creators"]["items"],
                (alpha.id, beta.id),
            ) == expected_creators
            assert [
                item["creator_id"]
                for item in subscription_result["groups"]["subscriptions"]["items"]
            ] == expected_creators
            assert _ids_in_order(
                default_creator_result["groups"]["creators"]["items"],
                (alpha.id, beta.id),
            ) == expected_creators
            default_subscriptions = default_subscription_result["groups"][
                "subscriptions"
            ]["items"]
            assert [item["creator_id"] for item in default_subscriptions] == (
                expected_creators
            )
            assert [item["name"] for item in default_subscriptions] == [
                "Zulu custom title",
                "Able custom title",
            ]
            assert [item["name_sort"] for item in default_subscriptions] == [
                "alpha",
                "beta",
            ]

            from app.api.subscriptions import list_subscriptions

            api_subscriptions = await list_subscriptions(
                offset=0,
                limit=100,
                q="",
                db=db,
                user=user,
            )
            assert [item.creator_id for item in api_subscriptions] == [
                alpha.id,
                beta.id,
            ]

            sort_expectations = {
                "name-desc": [beta_subscription.id, alpha_subscription.id],
                "created-asc": [alpha_subscription.id, beta_subscription.id],
                "created-desc": [beta_subscription.id, alpha_subscription.id],
                "updated-asc": [beta_subscription.id, alpha_subscription.id],
                "updated-desc": [alpha_subscription.id, beta_subscription.id],
            }
            for sort_name, expected in sort_expectations.items():
                result = await service.search(
                    f"is:inactive sort:{sort_name}",
                    scope="subscriptions",
                    permissions={"subscriptions"},
                    allowed_subscription_ids={
                        alpha_subscription.id,
                        beta_subscription.id,
                    },
                    user_id=user.id,
                    limit=100,
                )
                assert [
                    item["id"]
                    for item in result["groups"]["subscriptions"]["items"]
                ] == [str(identity) for identity in expected]

            private_sync_result = await service.search(
                "is:sync-disabled sort:name-asc",
                scope="subscriptions",
                permissions={"subscriptions"},
                allowed_subscription_ids={
                    alpha_subscription.id,
                    beta_subscription.id,
                },
                user_id=user.id,
                limit=100,
            )
            assert [
                item["creator_id"]
                for item in private_sync_result["groups"]["subscriptions"][
                    "items"
                ]
            ] == expected_creators

            subscription_anchors = await service.name_anchors(
                scope="subscriptions",
                query="is:inactive sort:name-asc",
                permissions={"subscriptions"},
                allowed_subscription_ids={
                    alpha_subscription.id,
                    beta_subscription.id,
                },
                user_id=user.id,
            )
            subscription_anchor_map = {
                item["key"]: item for item in subscription_anchors["items"]
            }
            assert subscription_anchors["total"] == 2
            assert subscription_anchor_map["A"]["offset"] == 0
            assert subscription_anchor_map["B"]["offset"] == 1
            await transaction.rollback()
    except (OSError, socket.gaierror) as exc:
        pytest.skip(f"PostgreSQL is unavailable from the host test runner: {exc}")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_name_anchors_cover_unicode_scripts_and_reverse_offsets(monkeypatch):
    """Wrong Unicode buckets or descending offsets must move an expected anchor."""
    from app.database import async_session
    from app.models import Creator
    from app.services.search import SearchService

    marker = uuid4().hex
    names = ("Ａlpha", "Beta", "3D", "あお", "アカ", "汉字", "Ωmega")
    try:
        async with async_session() as db:
            transaction = await db.begin()
            creators = [
                Creator(
                    name=f"{marker}-{index}",
                    display_name=name,
                    is_active=False,
                )
                for index, name in enumerate(names)
            ]
            db.add_all(creators)
            await db.flush()

            service = SearchService(db)
            ascending = await service.name_anchors(
                scope="creators",
                query="is:inactive sort:name-asc",
                permissions={"library"},
            )
            descending = await service.name_anchors(
                scope="creators",
                query="is:inactive sort:name-desc",
                permissions={"library"},
            )

            assert ascending["total"] == len(names)
            assert len(ascending["items"]) == 30
            asc = {item["key"]: item for item in ascending["items"]}
            assert asc["A"] == {
                "key": "A",
                "label": "A",
                "kind": "latin",
                "offset": 0,
                "count": 1,
            }
            assert (asc["B"]["offset"], asc["0-9"]["offset"]) == (1, 2)
            assert (asc["kana"]["offset"], asc["han"]["offset"]) == (3, 5)
            assert asc["other"]["offset"] == 6
            assert asc["Z"]["offset"] is None
            assert asc["Z"]["count"] == 0

            assert descending["direction"] == "desc"
            assert [item["key"] for item in descending["items"][:4]] == [
                "other",
                "han",
                "kana",
                "0-9",
            ]
            desc = {item["key"]: item for item in descending["items"]}
            assert (desc["other"]["offset"], desc["han"]["offset"]) == (0, 1)
            assert (desc["kana"]["offset"], desc["0-9"]["offset"]) == (2, 4)
            assert (desc["B"]["offset"], desc["A"]["offset"]) == (5, 6)
            await transaction.rollback()
    except (OSError, socket.gaierror) as exc:
        pytest.skip(f"PostgreSQL is unavailable from the host test runner: {exc}")

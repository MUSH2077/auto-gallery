from dataclasses import FrozenInstanceError
from datetime import UTC, datetime

import pytest


def _contract():
    from app.remote_discovery import contract

    return contract


def _classifier():
    from app.remote_discovery import classifier

    return classifier


def test_remote_collection_is_validated_and_deeply_immutable():
    contract = _contract()
    collection = contract.RemoteCollection(
        id="private",
        name="Private follows",
        selector={"restrict": "private"},
    )

    assert collection.selector["restrict"] == "private"
    with pytest.raises(FrozenInstanceError):
        collection.name = "changed"
    with pytest.raises(TypeError):
        collection.selector["restrict"] = "public"
    with pytest.raises(ValueError, match="collection id"):
        contract.RemoteCollection(id="", name="Missing id")
    with pytest.raises(TypeError, match="selector"):
        contract.RemoteCollection(id="public", name="Public", selector=["not", "a", "mapping"])


def test_candidate_identity_uses_source_creator_id_and_rejects_invalid_profiles():
    contract = _contract()
    candidate = contract.RemoteCandidateIdentity(
        source="pixiv",
        source_creator_id="123",
        profile_url="https://www.pixiv.net/users/123",
        display_name="Artist",
        username="artist",
        metadata={"follow_state": "public"},
    )

    assert candidate.source_creator_id == "123"
    with pytest.raises(TypeError):
        candidate.metadata["follow_state"] = "private"
    with pytest.raises(ValueError, match="source_creator_id"):
        contract.RemoteCandidateIdentity(source="pixiv", source_creator_id="", profile_url=None)
    with pytest.raises(ValueError, match="HTTPS"):
        contract.RemoteCandidateIdentity(
            source="x",
            source_creator_id="42",
            profile_url="http://x.com/artist",
        )
    with pytest.raises(ValueError, match="source"):
        contract.RemoteCandidateIdentity(
            source="mastodon",
            source_creator_id="42",
            profile_url="https://example.test/@artist",
        )


def test_discovery_page_freezes_items_and_cursor_and_rejects_mixed_sources():
    contract = _contract()
    pixiv = contract.RemoteCandidateIdentity(
        source="pixiv",
        source_creator_id="123",
        profile_url="https://www.pixiv.net/users/123",
    )
    page = contract.DiscoveryPage(items=[pixiv], next_cursor={"offset": 30}, done=False)

    assert page.items == (pixiv,)
    assert page.is_complete is False
    assert page.done is False
    with pytest.raises(TypeError):
        page.next_cursor["offset"] = 60
    with pytest.raises(ValueError, match="same source"):
        contract.DiscoveryPage(
            items=[
                pixiv,
                contract.RemoteCandidateIdentity(
                    source="x",
                    source_creator_id="456",
                    profile_url="https://x.com/artist",
                ),
            ],
            done=True,
        )
    with pytest.raises(ValueError, match="done"):
        contract.DiscoveryPage(items=[pixiv], next_cursor={"offset": 30}, done=True)


def test_remote_creator_detail_contract_validates_generic_profile_and_work_pages():
    """A provider cannot leak malformed or source-mismatched creator media into shared APIs."""
    contract = _contract()
    fetched_at = datetime.now(UTC)
    created_at = datetime(2026, 8, 30, tzinfo=UTC)
    profile = contract.RemoteCreatorProfile(
        source="pixiv",
        source_creator_id="123",
        display_name="Artist",
        username="artist",
        profile_url="https://www.pixiv.net/users/123",
        avatar_url="https://i.pximg.net/user-profile/img/123/avatar.jpg",
        comment="fixture profile",
        work_counts={"illust": 12, "manga": 3, "novel": 1},
        is_followed=True,
        fetched_at=fetched_at,
    )
    work = contract.RemoteWorkPreview(
        source="pixiv",
        source_work_id="456",
        source_creator_id="123",
        title="Fixture work",
        work_url="https://www.pixiv.net/artworks/456",
        created_at=created_at,
        work_type="manga",
        page_count=2,
        x_restrict=1,
        thumbnail_url="https://i.pximg.net/c/360x360/img-master/thumb.jpg",
        preview_urls=(
            "https://i.pximg.net/img-master/page0.jpg",
            "https://i.pximg.net/img-master/page1.jpg",
        ),
    )
    page = contract.RemoteWorkPage(items=[work], next_cursor={"offset": 20}, done=False)
    detail = contract.RemoteCreatorDetail(profile=profile, works=page)

    assert detail.profile.source_creator_id == "123"
    assert detail.works.items == (work,)
    assert detail.works.next_cursor["offset"] == 20
    with pytest.raises(TypeError):
        detail.profile.work_counts["illust"] = 99
    with pytest.raises(TypeError):
        detail.works.next_cursor["offset"] = 40
    with pytest.raises(ValueError, match="same creator"):
        contract.RemoteCreatorDetail(
            profile=profile,
            works=contract.RemoteWorkPage(
                items=[
                    contract.RemoteWorkPreview(
                        source="pixiv",
                        source_work_id="789",
                        source_creator_id="999",
                        title="Wrong creator",
                        work_url="https://www.pixiv.net/artworks/789",
                        created_at=created_at,
                        work_type="illust",
                        page_count=1,
                        x_restrict=0,
                        thumbnail_url=None,
                        preview_urls=(),
                    )
                ],
                done=True,
            ),
        )
    with pytest.raises(ValueError, match="x_restrict"):
        contract.RemoteWorkPreview(
            source="pixiv",
            source_work_id="456",
            source_creator_id="123",
            title="Bad rating",
            work_url="https://www.pixiv.net/artworks/456",
            created_at=created_at,
            work_type="illust",
            page_count=1,
            x_restrict=3,
            thumbnail_url=None,
            preview_urls=(),
        )


def test_remote_creator_contracts_are_exported_from_package_boundary():
    from app import remote_discovery

    assert remote_discovery.RemoteCreatorProfile is _contract().RemoteCreatorProfile
    assert remote_discovery.RemoteCreatorDetail is _contract().RemoteCreatorDetail
    assert remote_discovery.RemoteWorkPreview is _contract().RemoteWorkPreview
    assert remote_discovery.RemoteWorkPage is _contract().RemoteWorkPage


def test_discovery_registry_resolves_standard_adapter_and_rejects_unknown_source():
    contract = _contract()
    registry_module = __import__(
        "app.remote_discovery.registry", fromlist=["DiscoveryAdapterRegistry"]
    )

    class FixtureAdapter(contract.RemoteDiscoveryAdapter):
        source = "pixiv"
        auth_methods = ("refresh_token",)

        async def validate_account(self, credentials):
            return contract.RemoteCandidateIdentity(
                source="pixiv",
                source_creator_id="1",
                profile_url="https://www.pixiv.net/users/1",
            )

        async def list_collections(self, credentials):
            return ()

        async def fetch_page(self, credentials, *, selector=None, cursor=None, page_size=100):
            return contract.DiscoveryPage(items=(), done=True)

        def build_download_auth(self, credentials):
            return None

    registry = registry_module.DiscoveryAdapterRegistry()
    adapter = FixtureAdapter()
    registry.register(adapter)

    assert registry.get("pixiv") is adapter
    with pytest.raises(KeyError, match="Unknown remote discovery source"):
        registry.get("mastodon")


@pytest.mark.parametrize(
    ("signals", "confidence", "reason"),
    [
        ({"source": "x", "local_identity_match_count": 1}, "high", "unique_local_identity"),
        ({"source": "x", "danbooru_verified_link": True}, "high", "danbooru_verified_link"),
        ({"source": "bilibili", "verified_cross_site_link": True}, "high", "verified_cross_site_link"),
        ({"source": "pixiv", "pixiv_illustration_preview": True}, "high", "pixiv_illustration_preview"),
        (
            {"source": "x", "art_focused_bio": True, "recent_visual_post": True},
            "high",
            "multiple_creator_evidence",
        ),
        ({"source": "bilibili", "supported_site_link": True}, "medium", "single_creator_evidence"),
        ({"source": "x"}, "low", "no_creator_evidence"),
    ],
)
def test_confidence_classifier_returns_approved_explainable_tiers(signals, confidence, reason):
    classifier = _classifier()

    result = classifier.classify_identity(classifier.IdentityMatchSignals(**signals))

    assert result.confidence == confidence
    assert reason in result.reasons
    assert result.identity_conflict is False
    assert result.auto_importable is True


def test_conflicting_identity_is_never_auto_importable_even_with_exact_id():
    classifier = _classifier()

    result = classifier.classify_identity(
        classifier.IdentityMatchSignals(
            source="pixiv",
            local_identity_match_count=2,
            pixiv_illustration_preview=True,
        )
    )

    assert result.confidence == "high"
    assert result.identity_conflict is True
    assert result.auto_importable is False
    assert result.reasons == ("multiple_local_identity_matches", "pixiv_illustration_preview")

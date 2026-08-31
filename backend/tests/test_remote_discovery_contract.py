from dataclasses import FrozenInstanceError

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

"""Index identities and schema shared by query and projection adapters."""

from urllib.parse import urlparse
from app.config import settings
from app.services.search_language import SearchTarget
from app.services.search_pagination import MEILI_SHUFFLE_FIELDS

def _validate_index_namespace(database_url: str, index_prefix: str) -> str:
    database_name = urlparse(database_url).path.lstrip("/").split("?", 1)[0]
    if database_name.endswith("_test") and not index_prefix:
        raise RuntimeError(
            "Test database search isolation failure: MEILI_INDEX_PREFIX must be set "
            "before a *_test database may access Meilisearch."
        )
    return index_prefix


_INDEX_PREFIX = _validate_index_namespace(
    settings.database_url,
    settings.meili_index_prefix,
)
WORKS_INDEX = f"{_INDEX_PREFIX}works"
CREATORS_INDEX = f"{_INDEX_PREFIX}creators"
TAGS_INDEX = f"{_INDEX_PREFIX}tags"
REPOSITORIES_INDEX = f"{_INDEX_PREFIX}repositories"
SUBSCRIPTIONS_INDEX = f"{_INDEX_PREFIX}subscriptions"
MEMBERSHIPS_INDEX = f"{_INDEX_PREFIX}subscription_memberships_v1"


INDEX_LABELS = {
    WORKS_INDEX: "works",
    CREATORS_INDEX: "creators",
    TAGS_INDEX: "tags",
    REPOSITORIES_INDEX: "repositories",
    SUBSCRIPTIONS_INDEX: "subscriptions",
    MEMBERSHIPS_INDEX: "subscription_memberships",
}

MEILI_TARGET_INDEX: dict[SearchTarget, str] = {
    "works": WORKS_INDEX,
    "creators": CREATORS_INDEX,
    "tags": TAGS_INDEX,
    "repositories": REPOSITORIES_INDEX,
    "subscriptions": SUBSCRIPTIONS_INDEX,
}

INDEX_SETTINGS = {
    WORKS_INDEX: {
        "searchableAttributes": [
            "title",
            "description",
            "creator_names",
            "alias_names_current",
            "alias_names_historical",
            "alias_identities_current",
            "alias_identities_historical",
            "tags",
            "source_work_ids",
        ],
        "filterableAttributes": [
            "id",
            "is_nsfw",
            "is_ai_generated",
            "is_favorite",
            "visibility",
            "sources",
            "tags",
            "creator_ids",
            "repository_ids",
            "source_creator_keys",
            "source_work_keys",
            "has_tags",
            "has_description",
            "has_multiple_assets",
            "has_image",
            "has_animation",
            "has_video",
            "posted_ts",
            "created_ts",
            "updated_ts",
            "heat_available",
            "shuffle_key",
            *MEILI_SHUFFLE_FIELDS,
        ],
        # ``id`` is the deterministic final key for every non-relevance sort.
        # Without it, equal timestamps/titles can move between offset pages.
        "sortableAttributes": [
            "posted_ts",
            "created_ts",
            "updated_ts",
            "title",
            "heat_available",
            "heat_score",
            "shuffle_key",
            *MEILI_SHUFFLE_FIELDS,
            "id",
        ],
        "pagination": {"maxTotalHits": 100_000},
        "nonSeparatorTokens": ["_", "@"],
        "typoTolerance": {
            "enabled": True,
            "disableOnAttributes": [
                "alias_identities_current",
                "alias_identities_historical",
                "source_work_ids",
            ],
        },
    },
    CREATORS_INDEX: {
        "searchableAttributes": [
            "name",
            "display_name",
            "alias_names_current",
            "alias_names_historical",
            "alias_identities_current",
            "alias_identities_historical",
            "description",
            "source_creator_ids",
        ],
        "filterableAttributes": [
            "id",
            "is_active",
            "is_favorite",
            "has_subscription",
            "has_repository",
            "has_danbooru",
            "sources",
            "source_creator_keys",
            "created_ts",
            "updated_ts",
        ],
        "sortableAttributes": ["name_sort", "created_ts", "updated_ts", "id"],
        "pagination": {"maxTotalHits": 100_000},
        "nonSeparatorTokens": ["_", "@"],
        "typoTolerance": {
            "enabled": True,
            "disableOnAttributes": [
                "alias_identities_current",
                "alias_identities_historical",
                "source_creator_ids",
            ],
        },
    },
    TAGS_INDEX: {
        "searchableAttributes": ["normalized_name", "category"],
        "filterableAttributes": ["id", "normalized_name", "category", "created_ts", "updated_ts"],
        "sortableAttributes": ["normalized_name", "usage_count", "created_ts", "updated_ts"],
        "pagination": {"maxTotalHits": 100_000},
    },
    REPOSITORIES_INDEX: {
        "searchableAttributes": [
            "name",
            "creator_name",
            "alias_names_current",
            "alias_names_historical",
            "alias_identities_current",
            "alias_identities_historical",
            "source",
            "source_creator_id",
            "source_url",
            "subscription_name",
        ],
        "filterableAttributes": [
            "id",
            "creator_id",
            "subscription_id",
            "source",
            "source_creator_keys",
            "is_enabled",
            "auth_healthy",
            "auth_state",
            "credential_state",
            "has_last_sync",
            "has_source_creator_id",
            "created_ts",
            "updated_ts",
            "synced_ts",
        ],
        "sortableAttributes": ["name_sort", "created_ts", "updated_ts", "synced_ts", "id"],
        "pagination": {"maxTotalHits": 100_000},
        "nonSeparatorTokens": ["_", "@"],
        "typoTolerance": {
            "enabled": True,
            "disableOnAttributes": [
                "alias_identities_current",
                "alias_identities_historical",
                "source_creator_id",
                "source_url",
            ],
        },
    },
    SUBSCRIPTIONS_INDEX: {
        "searchableAttributes": [
            "name",
            "creator_name",
            "alias_names_current",
            "alias_names_historical",
            "alias_identities_current",
            "alias_identities_historical",
            "sources",
            "source_urls",
            "source_creator_ids",
        ],
        "filterableAttributes": [
            "id",
            "creator_id",
            "repository_ids",
            "sources",
            "source_creator_keys",
            "is_active",
            "sync_enabled",
            "never_synced",
            "has_last_sync",
            "created_ts",
            "updated_ts",
            "synced_ts",
        ],
        "sortableAttributes": ["name_sort", "created_ts", "updated_ts", "synced_ts", "id"],
        "pagination": {"maxTotalHits": 100_000},
        "nonSeparatorTokens": ["_", "@"],
        "typoTolerance": {
            "enabled": True,
            "disableOnAttributes": [
                "alias_identities_current",
                "alias_identities_historical",
                "source_urls",
                "source_creator_ids",
            ],
        },
    },
}

# One physical index for all actors; identity is the private membership UUID.
INDEX_SETTINGS[MEMBERSHIPS_INDEX] = {
    **INDEX_SETTINGS[SUBSCRIPTIONS_INDEX],
    "searchableAttributes": ["name", "canonical_name", *INDEX_SETTINGS[SUBSCRIPTIONS_INDEX]["searchableAttributes"][1:]],
    "filterableAttributes": [*INDEX_SETTINGS[SUBSCRIPTIONS_INDEX]["filterableAttributes"], "user_id", "subscription_id"],
    "sortableAttributes": [*INDEX_SETTINGS[SUBSCRIPTIONS_INDEX]["sortableAttributes"], "subscription_id"],
}

WORK_PROJECTION_VERSION = 5
WORK_HYDRATION_QUERY_COUNT = 4
# The 4 MiB payload bound remains authoritative.  A larger identity window
# amortizes the four indexed hydration scans on high-latency NAS storage; the
# payload splitter still sends only one bounded Meilisearch write at a time.
WORK_DOCUMENT_BATCH_SIZE = 2_000
MEILI_DOCUMENT_PAYLOAD_BYTES = 4 * 1024 * 1024

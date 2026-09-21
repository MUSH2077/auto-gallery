# Multi-user Remote Follow Discovery Implementation Plan

> **For agentic workers:** Use `superpowers:subagent-driven-development` task by task. Follow TDD for every behavior change.

**Goal:** Add per-user remote accounts, private subscription memberships, explainable follow discovery, preview/import workflows, and shared downloads for Pixiv, X, and Bilibili.

**Architecture:** Existing `subscriptions` and `subscription_sources` remain canonical shared repositories. New user-membership tables own private policy, due state, credential selection, and auth health; aggregate fields on canonical rows remain compatibility caches. Remote credentials are AES-GCM encrypted and only materialized into short-lived download configuration.

**Tech Stack:** FastAPI, SQLAlchemy async, Alembic, PostgreSQL, RQ/Redis, gallery-dl 1.32.9, Next.js/React, TanStack Query, Playwright.

## Global Constraints

- `Creator`, `SourceCreator`, `Work`, `Asset`, storage, and canonical repository/source rows remain globally shared and deduplicated.
- Remote accounts, discovery candidates, subscription memberships, membership source policy, and automatic-import policy are private to one local user; administrators get metadata-only audit access.
- One remote account per `(user_id, source)` for `pixiv`, `x`, and `bilibili`.
- Remote unfollow never deletes, disables, or archives an imported local subscription.
- Preview/import is always available. Automatic import is opt-in, defaults to high confidence, defaults to 25 imports per scan, and accepts 1-200.
- Imported memberships enter normal scheduling; manual import may explicitly request immediate synchronization.
- Personal credentials may drive that member's download, while imported media remains shared. Secrets may never enter API reads, logs, Redis, TaskRun payloads, job manifests, or durable per-job config paths.
- Deliver provider slices in order Pixiv, X, Bilibili. Pixiv/Bilibili are marked experimental; X prefers OAuth 2 PKCE with Cookie fallback.
- Existing public subscription routes keep the canonical subscription UUID and overlay the current user's membership state.
- Existing 809 repository rows are backfilled to the earliest active administrator; this installation currently has one active administrator.

---

### Task 1: Private Membership and Remote Discovery Persistence

Create `UserSubscription`, `UserSubscriptionSource`, `RemoteAccount`, and `DiscoveryCandidate` models and schemas with the constraints and fields from the approved plan. Add optional triggering membership/account ownership fields to download/task records where required for later tasks. Add an additive Alembic migration that backfills existing subscriptions and sources to the earliest active administrator without deleting legacy columns. Export relationships safely and add behavior tests for constraints, defaults, backfill SQL, and secret-free model representation.

Verification: targeted model/schema tests and Alembic metadata tests pass; existing provider/subscription tests remain green.

### Task 2: Credential Cryptography, Adapter Contract, and Provider Adapters

Implement `REMOTE_CREDENTIAL_KEY` configuration and an AES-GCM credential vault using `user_id/source/account_id` as AAD. Define `RemoteDiscoveryAdapter`, `RemoteCollection`, `RemoteCandidateIdentity`, `DiscoveryPage`, registry lookup, and explainable confidence classification. Implement Pixiv refresh-token following, X official OAuth/API plus Cookie fallback, and Bilibili Cookie/group following adapters behind injectable HTTP/API boundaries. Extend Provider capabilities and Bilibili `/dynamic` and `/upload/opus` normalization. Test encryption tampering, redaction, pagination, collections, malformed payloads, auth errors, rate limits, and confidence tiers without live credentials.

Verification: credential, adapter, provider, and existing provider contract tests pass.

### Task 3: Membership, Remote Account, Discovery, and Import Services/APIs

Implement private-membership services and overlay existing subscription CRUD/list/detail/source operations for the authenticated user while preserving canonical UUID routes. Implement own-account CRUD/test/collections, X OAuth PKCE state, metadata-only admin audit, scan enqueue/list, candidate pagination, batch import/dismiss/restore, and conflict resolution. Scans are single-flight per account, commit cursor plus candidates per page, and only mark absent followings after complete scans. Imports are idempotent, reuse existing creator identity/Danbooru matching, preserve dismissed candidates, and create only the current user's membership/source binding.

Verification: two-user isolation, administrator audit redaction, idempotent scan/import, incomplete scan, dismissal, conflict, and API permission tests pass.

### Task 4: Shared Scheduling, Personal Download Authentication, and Discovery Queue

Make `UserSubscriptionSource` authoritative for due/auth state and transactionally aggregate canonical `is_enabled`/`next_sync_at`. On a canonical due row, select the earliest healthy due membership, record only opaque trigger/account IDs, and materialize selected credentials into a mode-0600 temporary gallery-dl overlay removed in all exit paths. Successful shared download replans every member; auth failure damages only the selected binding/account and allows another member to take over. Add the `discovery` queue and scheduled remote-account scan admission using existing task heartbeat/retry/notification conventions.

Verification: earliest-demand selection, one canonical job for two users, success fan-out, credential fallback, secret absence, temp-file cleanup, queue routing, and existing scheduler tests pass.

### Task 5: Remote Discovery Admin UI

Add typed API clients and a `/admin/discovery` page under Subscriptions & Sources. Build account cards for connect/reauth/test, collections, confidence threshold, interval, auto-import, and per-scan cap. Build a paginated candidate table showing source, remote state, explainable confidence, local match/conflict, and batch import/dismiss/restore; conflict resolution supports attach-existing or create-new. Manual import defaults to no immediate synchronization. Apply existing permissions, i18n, components, TanStack Query patterns, and React performance guidance.

Verification: TypeScript check/build pass and Playwright covers account setup, scan preview, batch actions, conflicts, settings, and cross-user inaccessibility.

### Task 6: Contracts, Documentation, and Rollout Gates

Regenerate OpenAPI/client types, update provider capability documentation, setup/security/runbook instructions, backup/key warning, queue inventory, and feature flags. Add optional live-provider smoke tests that are disabled unless explicit test credentials are supplied. Run backend unit/integration suites, frontend typecheck/build/E2E, migration upgrade validation, secret scans, and compose/runtime contract checks.

Verification: all required suites pass with no secret output and the application remains rollback-compatible while retaining the additive schema.

# Pixiv Live Work State Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Open Pixiv manual remote discovery in production and replace stale Pixiv work-detail statistics with a credential-isolated live provider request on every page opening.

**Architecture:** Extend the existing remote discovery adapter with an optional typed work-state read, implement it for Pixiv, and keep credential selection/generation fencing inside `RemoteAccountService`. Expose a no-store work endpoint and render it as an independent TanStack Query region so provider failures never block local work content.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy async, Pydantic, httpx, PostgreSQL, React 19, Next.js 16, TanStack Query 5, TypeScript, Playwright, Docker Compose.

**Spec:** `docs/superpowers/specs/2026-08-30-pixiv-live-work-state-design.md`

## Global Constraints

- Use only the current user's enabled, healthy Pixiv `RemoteAccount`; never borrow another user's or a global gallery-dl credential.
- Fetch on every work-detail mount with no Redis, database, process-memory, HTTP, or browser freshness cache.
- Return only live view count, live bookmark count, and read-only current-account Pixiv bookmark state.
- Never read or render `raw_metadata.total_view` or `raw_metadata.total_bookmarks` as fallback values.
- Keep Pixiv automatic import, X, and Bilibili rollout disabled.
- Never emit credentials or provider payloads into logs, API errors, Redis, task metadata, manifests, tests, or Git.
- Make no live Pixiv request during automated implementation or rollout verification; the user performs the first authorized request through the UI.

---

### Task 1: Typed provider work-state contract and Pixiv implementation

**Files:**
- Modify: `backend/app/remote_discovery/contract.py`
- Modify: `backend/app/remote_discovery/pixiv.py`
- Modify: `backend/tests/test_remote_discovery_adapters.py`

**Interfaces:**
- Produces: `RemoteWorkState(source, source_work_id, fetched_at, total_views, total_bookmarks, is_bookmarked)`.
- Produces: `RemoteDiscoveryAdapter.fetch_work_state(credentials, *, source_work_id) -> RemoteWorkState` with a fail-closed default.
- Produces: `PixivRemoteDiscoveryAdapter.fetch_work_state(...)` backed by `/v1/illust/detail`.

- [ ] **Step 1: Add failing contract and Pixiv mapping tests**

Append tests that prove immutability/validation, the exact request, and provider error mapping:

```python
@pytest.mark.asyncio
async def test_pixiv_work_state_uses_live_illust_detail_and_maps_volatile_fields():
    from app.remote_discovery.pixiv import PixivRemoteDiscoveryAdapter

    response = _common().RemoteHTTPResponse
    transport = FixtureTransport(
        response(200, {"access_token": "short-lived-access", "expires_in": 3600}, {}),
        response(200, {"illust": {
            "id": 38362603,
            "total_view": 123456,
            "total_bookmarks": 7890,
            "is_bookmarked": True,
        }}, {}),
    )

    state = await PixivRemoteDiscoveryAdapter(transport).fetch_work_state(
        {"refresh_token": "refresh"}, source_work_id="38362603"
    )

    assert state.source == "pixiv"
    assert state.source_work_id == "38362603"
    assert state.total_views == 123456
    assert state.total_bookmarks == 7890
    assert state.is_bookmarked is True
    method, url, kwargs = transport.requests[1]
    assert method == "GET"
    assert url == "https://app-api.pixiv.net/v1/illust/detail"
    assert kwargs["params"] == {"illust_id": "38362603"}
    assert kwargs["headers"]["Authorization"] == "Bearer short-lived-access"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [("total_view", -1), ("total_view", True), ("total_bookmarks", "9"),
     ("is_bookmarked", 1)],
)
async def test_pixiv_work_state_rejects_malformed_volatile_fields(field, value):
    from app.remote_discovery.common import MalformedRemoteResponse
    from app.remote_discovery.pixiv import PixivRemoteDiscoveryAdapter

    payload = {
        "id": 38362603,
        "total_view": 10,
        "total_bookmarks": 2,
        "is_bookmarked": False,
    }
    payload[field] = value
    response = _common().RemoteHTTPResponse
    adapter = PixivRemoteDiscoveryAdapter(FixtureTransport(
        response(200, {"access_token": "access"}, {}),
        response(200, {"illust": payload}, {}),
    ))

    with pytest.raises(MalformedRemoteResponse, match="Pixiv"):
        await adapter.fetch_work_state({"refresh_token": "refresh"}, source_work_id="38362603")
```

Also add focused cases where detail returns `401`, `429` with `Retry-After: 27`, a missing `illust` object, and an illust ID different from the requested ID. Assert the existing `RemoteReauthenticationRequired`, `RemoteRateLimited`, and `MalformedRemoteResponse` types.

- [ ] **Step 2: Run adapter tests and verify the new tests fail**

Run:

```bash
cd backend
python -m pytest tests/test_remote_discovery_adapters.py -q
```

Expected: the new tests fail because `RemoteWorkState` and `fetch_work_state` do not exist.

- [ ] **Step 3: Implement the typed contract and Pixiv method**

Add this frozen dataclass and concrete default to `contract.py`:

```python
@dataclass(frozen=True)
class RemoteWorkState:
    source: RemoteSource
    source_work_id: str
    fetched_at: datetime
    total_views: int
    total_bookmarks: int
    is_bookmarked: bool

    def __post_init__(self) -> None:
        if self.source not in {"pixiv", "x", "bilibili"}:
            raise ValueError("remote work state source is not supported")
        if not self.source_work_id.strip():
            raise ValueError("source_work_id must not be empty")
        if self.fetched_at.tzinfo is None:
            raise ValueError("fetched_at must be timezone-aware")
        for name in ("total_views", "total_bookmarks"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if not isinstance(self.is_bookmarked, bool):
            raise ValueError("is_bookmarked must be a boolean")


async def fetch_work_state(
    self,
    credentials: RedactedCredentials | Mapping[str, Any],
    *,
    source_work_id: str,
) -> RemoteWorkState:
    raise NotImplementedError(f"{self.source} does not support remote work state")
```

Import `datetime` and make this method non-abstract so X and Bilibili remain valid adapters. In `pixiv.py`, add `ILLUST_DETAIL_URL`, reuse `_authentication`, call `checked_payload`, validate the `illust` mapping and exact ID, and construct `RemoteWorkState` with `datetime.now(UTC)`. Use a helper that rejects booleans as integers and all negative/missing values. Set the default Pixiv `HttpxRemoteTransport` timeout to ten seconds; injected fixture transports remain unchanged.

- [ ] **Step 4: Run adapter tests and verify they pass**

Run:

```bash
cd backend
python -m pytest tests/test_remote_discovery_adapters.py -q
```

Expected: all adapter tests pass with no network access.

- [ ] **Step 5: Commit the provider boundary**

```bash
git add backend/app/remote_discovery/contract.py backend/app/remote_discovery/pixiv.py backend/tests/test_remote_discovery_adapters.py
git commit -m "feat: fetch live Pixiv work state"
```

---

### Task 2: Current-user credential transaction and generation fencing

**Files:**
- Modify: `backend/app/services/remote_accounts.py`
- Create: `backend/tests/test_remote_work_state.py`

**Interfaces:**
- Consumes: `RemoteDiscoveryAdapter.fetch_work_state(..., source_work_id=...)` from Task 1.
- Produces: `RemoteAccountService.fetch_work_state(source: str, source_work_id: str) -> RemoteWorkState`.
- Produces stable service exceptions: `RemoteWorkStateAccountRequired`, `RemoteWorkStateAccountUnhealthy`, and the existing `RemoteCredentialGenerationChanged`.

- [ ] **Step 1: Write failing user-isolation and account-health tests**

Create a PostgreSQL integration test module using the existing async engine/session fixtures. Seed two users, two encrypted Pixiv accounts with different vault AAD, and a fake adapter that records the redacted credential it receives:

```python
@pytest.mark.asyncio
async def test_work_state_uses_only_current_users_enabled_healthy_pixiv_account(db_session):
    owner = await seed_user(db_session, "work-state-owner")
    other = await seed_user(db_session, "work-state-other")
    vault = CredentialVault(TEST_REMOTE_KEY)
    owner_account = await seed_pixiv_account(
        db_session, owner.id, vault, token="owner-token", auth_status="healthy", is_enabled=True
    )
    await seed_pixiv_account(
        db_session, other.id, vault, token="other-token", auth_status="healthy", is_enabled=True
    )
    adapter = RecordingWorkStateAdapter()

    state = await RemoteAccountService(
        db_session, owner.id, vault=vault, adapters=registry_with(adapter)
    ).fetch_work_state("pixiv", "38362603")

    assert state.source_work_id == "38362603"
    assert adapter.tokens == ["owner-token"]
    assert owner_account.auth_status == "healthy"
```

Add parameterized cases for no account, disabled account, `untested`, `unhealthy`, deleted, and missing ciphertext. Assert no adapter invocation and the exact stable exception type. Add a case proving a user cannot obtain state when only another user's healthy account exists.

- [ ] **Step 2: Write failing generation-change and reauthentication tests**

Use a blocking fake adapter so the test can update `credential_generation` in a second transaction while the provider call is in flight. Assert `RemoteCredentialGenerationChanged` and no stale result. Add a `RemoteReauthenticationRequired` fake response and assert only the selected account becomes unhealthy, its bindings are updated through `_set_binding_health`, the other user's account remains healthy, and no provider error text contains the token canary.

- [ ] **Step 3: Run the new service tests and verify they fail**

Run:

```bash
cd backend
python -m pytest tests/test_remote_work_state.py -q
```

Expected: collection fails because `RemoteAccountService.fetch_work_state` and its stable account-state errors do not exist.

- [ ] **Step 4: Implement the account-scoped read transaction**

In `remote_accounts.py`, define stable, message-free exception classes and add:

```python
async def fetch_work_state(self, source: str, source_work_id: str) -> RemoteWorkState:
    require_preview(source)
    account = (
        await self.db.execute(
            select(RemoteAccount)
            .where(
                RemoteAccount.user_id == self.user_id,
                RemoteAccount.source == source,
                RemoteAccount.auth_status != "deleted",
            )
            .order_by(RemoteAccount.id)
            .with_for_update(of=RemoteAccount)
        )
    ).scalar_one_or_none()
    # Map missing, disabled/missing-ciphertext, and non-healthy states to the
    # stable service exceptions before decrypting or invoking the adapter.
```

Follow the same pinned identity/generation flow used by `test()` and
`collections()`: capture `_credential_use_identity`, decrypt through
`credentials_for_adapter`, call `adapter.fetch_work_state`, and relock through
`_relock_provider_result` before accepting the result. Validate the returned
source and work ID. On `RemoteReauthenticationRequired`, relock, mark this
account/bindings unhealthy, flush, and re-raise. Do not mutate the account or
database on success, rate limit, timeout, malformed payload, or generic remote
failure.

- [ ] **Step 5: Run service and credential regression tests**

Run:

```bash
cd backend
python -m pytest \
  tests/test_remote_work_state.py \
  tests/test_remote_credentials.py \
  tests/test_remote_credential_generation.py \
  tests/test_remote_account_services.py -q
```

Expected: all selected tests pass.

- [ ] **Step 6: Commit the credential-scoped service**

```bash
git add backend/app/services/remote_accounts.py backend/tests/test_remote_work_state.py
git commit -m "feat: isolate live work state by user account"
```

---

### Task 3: No-store work API and safe error vocabulary

**Files:**
- Modify: `backend/app/schemas/work.py`
- Modify: `backend/app/api/works.py`
- Modify: `backend/tests/test_remote_work_state.py`
- Modify: `backend/tests/test_permission_matrix.py`
- Modify: `backend/tests/test_nsfw_filter.py`

**Interfaces:**
- Consumes: `RemoteAccountService.fetch_work_state("pixiv", source_work_id)` from Task 2.
- Produces: `GET /api/v1/works/{work_id}/remote-state`.
- Produces: `RemoteWorkStateRead` with `source`, `source_work_id`, `fetched_at`, `total_views`, `total_bookmarks`, and `is_bookmarked`.

- [ ] **Step 1: Add failing API success and no-fallback tests**

Add ASGI tests that seed a visible Pixiv work whose `raw_metadata` contains deliberately different stale values:

```python
response = await client.get(
    f"/api/v1/works/{work.id}/remote-state",
    headers=library_headers,
)
assert response.status_code == 200
assert response.headers["cache-control"] == "private, no-store"
assert response.json() == {
    "source": "pixiv",
    "source_work_id": "38362603",
    "fetched_at": "2026-08-30T00:00:00Z",
    "total_views": 321,
    "total_bookmarks": 45,
    "is_bookmarked": True,
}
assert response.json()["total_views"] != work_source.raw_metadata["total_view"]
assert response.json()["total_bookmarks"] != work_source.raw_metadata["total_bookmarks"]
```

Patch only the injected adapter/registry boundary, never outbound networking.
Assert a second endpoint request invokes the fake adapter a second time.

- [ ] **Step 2: Add failing API authorization and error-mapping tests**

Cover anonymous `401`, user without `library` permission `403`, hidden NSFW work
`404`, missing work `404`, non-Pixiv work `409 remote_work_state_unsupported`,
missing account `409 remote_account_required`, unhealthy account `409
remote_account_reauthentication_required`, closed rollout `503
remote_discovery_unavailable`, rate limit `429` with sanitized `Retry-After`,
generation change `409 remote_account_stale`, and malformed/timeout provider
failure `502 remote_provider_unavailable`. Assert token and payload canaries are
absent from JSON and captured logs.

- [ ] **Step 3: Run API tests and verify they fail**

Run:

```bash
cd backend
python -m pytest \
  tests/test_remote_work_state.py \
  tests/test_permission_matrix.py \
  tests/test_nsfw_filter.py -q
```

Expected: route/schema assertions fail because the API is not registered.

- [ ] **Step 4: Implement the schema and route**

Add to `schemas/work.py`:

```python
class RemoteWorkStateRead(BaseModel):
    source: Literal["pixiv"]
    source_work_id: str
    fetched_at: datetime
    total_views: int = Field(ge=0)
    total_bookmarks: int = Field(ge=0)
    is_bookmarked: bool
```

Add the route before generic nested work routes in `api/works.py`. Reuse
`WorkRepository.get(work_id, force_sfw=not user.nsfw_visible)`, select the
Pixiv `WorkSource` with `order_by(WorkSource.id).limit(1)`, call the Task 2
service, and set `response.headers["Cache-Control"] = "private, no-store"` only
on success. Map each known exception to the stable codes listed in Step 2;
sanitize `Retry-After` to a positive integer. Commit account-health changes on
reauthentication, roll back stale-generation conflicts, and never return raw
exception messages for provider failures.

- [ ] **Step 5: Run API and relevant backend tests**

Run:

```bash
cd backend
python -m pytest \
  tests/test_remote_work_state.py \
  tests/test_remote_discovery_rollout.py \
  tests/test_permission_matrix.py \
  tests/test_nsfw_filter.py \
  tests/test_api_contract.py -q
```

Expected: all selected tests pass.

- [ ] **Step 6: Commit the public API**

```bash
git add backend/app/schemas/work.py backend/app/api/works.py backend/tests/test_remote_work_state.py backend/tests/test_permission_matrix.py backend/tests/test_nsfw_filter.py
git commit -m "feat: expose no-store Pixiv work state"
```

---

### Task 4: Work-detail live statistics UI

**Files:**
- Modify: `admin-web/src/lib/api/types.ts`
- Modify: `admin-web/src/lib/api/endpoints/works.ts`
- Modify: `admin-web/src/lib/api/index.ts`
- Modify: `admin-web/src/app/admin/works/[id]/page.tsx`
- Modify: `admin-web/src/lib/i18n.tsx`
- Create: `admin-web/tests/e2e/work-remote-state.spec.ts`

**Interfaces:**
- Consumes: `GET /api/v1/works/{work_id}/remote-state` from Task 3.
- Produces: `api.getWorkRemoteState(id) -> Promise<RemoteWorkState>`.
- Produces: `queryKeys.works.remoteState(id)`.

- [ ] **Step 1: Add a failing Playwright fixture for fresh values**

Create a route-isolated E2E test that mocks auth, one work, Pixiv source,
assets/tags/history, and the remote-state endpoint. Put stale values in
`raw_metadata` and different live values in the endpoint:

```typescript
test("work detail renders live Pixiv state and never renders stale metadata stats", async ({ page }) => {
  let remoteCalls = 0;
  await installWorkFixtures(page.context(), {
    rawMetadata: { total_view: 11, total_bookmarks: 12 },
    remoteState: {
      source: "pixiv",
      source_work_id: "38362603",
      fetched_at: "2026-08-30T00:00:00Z",
      total_views: 987654,
      total_bookmarks: 4321,
      is_bookmarked: true,
    },
    onRemoteState: () => { remoteCalls += 1; },
  });

  await page.goto("/admin/works/work-pixiv-live");
  await expect(page.getByText("987,654")).toBeVisible();
  await expect(page.getByText("4,321")).toBeVisible();
  await expect(page.getByText("Pixiv bookmarked")).toBeVisible();
  await expect(page.getByText("11", { exact: true })).toHaveCount(0);
  await expect(page.getByText("12", { exact: true })).toHaveCount(0);
  expect(remoteCalls).toBe(1);
});
```

Add a navigation-away-and-back case that observes two calls total, and assert
focus changes while mounted do not add a call.

- [ ] **Step 2: Add failing non-blocking error-state tests**

Parameterize `409 remote_account_required`, `409
remote_account_reauthentication_required`, `429`, and `502`. In every case,
assert the localized live-state error/reconnect hint, work title, and asset
viewer remain visible. Add a pending response test for the statistics skeleton
and a false bookmark case for `Pixiv not bookmarked`. Assert the local favorite
button retains its own `Local library favorite` accessible label.

- [ ] **Step 3: Run the new E2E file and verify it fails**

Run:

```bash
cd admin-web
PLAYWRIGHT_MANAGE_SERVER=1 npx playwright test tests/e2e/work-remote-state.spec.ts --project=chromium --workers=1
```

Expected: tests fail because the endpoint client and live statistics component do not exist.

- [ ] **Step 4: Add frontend types, API method, and query key**

Add:

```typescript
export interface RemoteWorkState {
  source: "pixiv";
  source_work_id: string;
  fetched_at: string;
  total_views: number;
  total_bookmarks: number;
  is_bookmarked: boolean;
}
```

Implement `getWorkRemoteState`, and add
`remoteState: (id: string) => ["works", id, "remote-state"] as const`.

- [ ] **Step 5: Replace metadata stats with the independent live query**

In the work detail component, derive a Pixiv source from `sources.data` and
create the query before early returns:

```typescript
const hasPixivSource = ((sources.data || []) as WorkSourceData[])
  .some((source) => source.source === "pixiv");
const remoteState = useQuery({
  queryKey: queryKeys.works.remoteState(id),
  queryFn: () => api.getWorkRemoteState(id),
  enabled: hasPixivSource,
  staleTime: 0,
  gcTime: 0,
  refetchOnMount: "always",
  refetchOnWindowFocus: false,
  retry: false,
});
```

Delete the `raw.total_view`, `raw.total_bookmarks`, and `hasStats` reads. Render
a focused `PixivLiveStateCard` for loading, success, and safe error states.
Display the explicit source-account bookmark label separately from the local
favorite star. Add bilingual keys for live views, live bookmark count, Pixiv
bookmarked/not bookmarked, local-library favorite/unfavorite, unavailable,
connect-account, reconnect-account, and rate-limited messages.

- [ ] **Step 6: Run E2E, i18n, and TypeScript checks**

Run:

```bash
cd admin-web
PLAYWRIGHT_MANAGE_SERVER=1 npx playwright test tests/e2e/work-remote-state.spec.ts --project=chromium --workers=1
npm run check:i18n
npm run typecheck
```

Expected: all commands pass.

- [ ] **Step 7: Commit the work-detail UI**

```bash
git add admin-web/src/lib/api/types.ts admin-web/src/lib/api/endpoints/works.ts admin-web/src/lib/api/index.ts admin-web/src/app/admin/works/[id]/page.tsx admin-web/src/lib/i18n.tsx admin-web/tests/e2e/work-remote-state.spec.ts
git commit -m "feat: show live Pixiv work state"
```

---

### Task 5: Public contracts, documentation, and full regression

**Files:**
- Modify: `docs/api/openapi.json`
- Modify: `admin-web/src/lib/api/types.generated.ts`
- Modify: `docs/providers.md`
- Modify: `docs/providers.zh.md`
- Modify: `docs/RUNBOOK.md`

**Interfaces:**
- Consumes: the completed backend OpenAPI application from Task 3.
- Produces: checked-in OpenAPI and generated TypeScript contracts matching the live API.

- [ ] **Step 1: Export and regenerate deterministic API contracts**

Run:

```bash
cd backend
python scripts/export_api_contracts.py
cd ../admin-web
npm run generate:api-types
npm run check:api-types
```

Expected: `RemoteWorkStateRead` and `/api/v1/works/{work_id}/remote-state` appear in both generated files and `check:api-types` passes.

- [ ] **Step 2: Document exact live-state behavior and operator recovery**

Update English and Chinese provider docs to state that Pixiv work views,
bookmark count, and current-account bookmark state are live App API reads using
the viewing user's healthy account, with no metadata fallback or cache. Update
the runbook with safe response-code diagnosis (`409`, `429`, `502`, `503`) and
the fact that closing Pixiv preview also disables live work state without
affecting local work pages.

- [ ] **Step 3: Run backend static and focused regression checks**

Run:

```bash
cd backend
ruff check app tests
python -m pytest \
  tests/test_remote_discovery_adapters.py \
  tests/test_remote_work_state.py \
  tests/test_remote_discovery_rollout.py \
  tests/test_remote_credentials.py \
  tests/test_remote_credential_generation.py \
  tests/test_remote_account_services.py \
  tests/test_permission_matrix.py \
  tests/test_nsfw_filter.py \
  tests/test_api_contract.py -q
```

Expected: Ruff exits zero and all selected tests pass.

- [ ] **Step 4: Run all frontend contracts, production build, and relevant E2E**

Run:

```bash
cd admin-web
npm run check:i18n
npm run check:charts
npm run check:media
npm run check:search-contract
npm run check:admin-routes
npm run check:api-types
npm run typecheck
npm run build
PLAYWRIGHT_MANAGE_SERVER=1 npx playwright test \
  tests/e2e/work-remote-state.spec.ts \
  tests/e2e/remote-discovery.spec.ts \
  tests/e2e/admin-shell.spec.ts \
  --project=chromium --workers=1
```

Expected: every command passes with zero Playwright failures.

- [ ] **Step 5: Run backend non-live suite and repository checks**

Run the complete backend suite excluding only explicitly marked integration and
live-provider tests, then the repository contracts:

```bash
cd backend
python -m pytest -m "not integration and not live_provider"
cd ..
docker compose --env-file .env.ci config --quiet
bash scripts/privacy-scan.sh
git diff --check
```

Expected: all commands exit zero; live provider tests are skipped and no remote provider is contacted.

- [ ] **Step 6: Commit generated contracts and documentation**

```bash
git add docs/api/openapi.json admin-web/src/lib/api/types.generated.ts docs/providers.md docs/providers.zh.md docs/RUNBOOK.md
git commit -m "docs: describe live Pixiv work state"
```

---

### Task 6: Push, guarded Pixiv-preview rollout, and production verification

**Files:**
- Modify outside Git: `/volume2/docker/auto-gallery/.env` with mode `0600`
- Create outside Git: guarded deployment rollback directory under `/volume2/docker/auto-gallery-deployments/`

**Interfaces:**
- Consumes: completed and verified feature branch.
- Produces: production Pixiv connection/manual preview and live work-state UI, with automatic import still disabled.

- [ ] **Step 1: Review branch state and push the existing PR branch**

Run:

```bash
git status --short
git log --oneline --decorate -8
git push origin feat/remote-follow-discovery
gh pr checks 63
```

Expected: only the intentional untracked `admin-web/node_modules` symlink is present; the push succeeds. Wait for every required PR check to pass before production mutation.

- [ ] **Step 2: Update the production environment without exposing secrets**

Use a secret-safe script that reads `/volume2/docker/auto-gallery/.env`, preserves
all unrelated lines, generates a URL-safe 32-byte key only when
`REMOTE_CREDENTIAL_KEY` is absent/empty, and atomically replaces the file with
mode `0600`. Set the seven rollout values exactly as listed in Global
Constraints. The script must print only key presence and boolean flag names,
never values of secrets. Validate with name-only/presence checks and
`docker compose config --quiet`.

- [ ] **Step 3: Execute the guarded deployment**

Copy the production `.env` temporarily into the feature worktree with mode
`0600`, then run:

```bash
COMPOSE_PROJECT_NAME=auto-gallery \
DEPLOY_SNAPSHOT_ROOT=/volume2/docker/auto-gallery-deployments \
LOCAL_BUILD_MEMORY_LIMIT=1536m \
bash scripts/deploy.sh
```

Expected: candidate builds and checks pass; a complete rollback point is
created; migration remains at head; backend/admin and all workers become
healthy. Remove the exact temporary worktree `.env` immediately after
verification without touching the production root `.env`.

- [ ] **Step 4: Verify effective rollout and runtime health**

Run `scripts/verify-runtime.sh` with the production env file, inspect only
container state/image/OOM/restart fields, and verify logs show separate
`operations` and `discovery` listeners. Use an authenticated, read-only
capability request to assert:

```json
{
  "pixiv": {"manual_preview": true, "auto_import": false},
  "x": {"manual_preview": false, "auto_import": false},
  "bilibili": {"manual_preview": false, "auto_import": false}
}
```

Also assert `REMOTE_CREDENTIAL_KEY` is present without printing it and the
production environment file remains mode `0600`.

- [ ] **Step 5: Run production browser smoke without provider credentials**

Use Playwright against `http://host.docker.internal:13000` to verify login,
authenticated Remote Discovery navigation, visible Pixiv connection card,
absent X/Bilibili connection cards, disabled/absent auto-import controls, and
desktop/mobile layout with zero browser/server errors. Do not enter a token and
do not invoke test, collections, scan, or work remote-state endpoints.

- [ ] **Step 6: Hand off the first authorized Pixiv validation**

Report the PR commit, deployment ID, rollback script, image IDs, migration head,
runtime/browser results, and the exact user flow:

1. Open `/admin/discovery`.
2. Connect Pixiv with a refresh token.
3. Run the account test until health is `healthy`.
4. Select public/private follows and start one manual scan.
5. Keep automatic import off while reviewing candidates.
6. Open one Pixiv work detail page and verify live views, bookmark count, and
   read-only Pixiv bookmark state.

Do not claim a live provider smoke passed until the user performs those steps
and reports the result.

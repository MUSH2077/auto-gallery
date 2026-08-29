# Pixiv Preview Rollout and Live Work State Design

## Context

The private remote-discovery foundation and Pixiv adapter are implemented and
deployed behind fail-closed rollout flags. Production currently contains 809
global subscriptions, 809 user memberships, and 2,670 user source bindings,
but no remote accounts or discovery candidates. `REMOTE_CREDENTIAL_KEY` and
all remote-discovery rollout flags are unset.

The work detail page currently reads Pixiv `total_view` and
`total_bookmarks` from `WorkSource.raw_metadata`. Those values are import-time
snapshots and must no longer be presented as current statistics.

## Goals

- Open Pixiv account connection and manual discovery preview in production,
  while leaving every automatic-import stage and every other provider closed.
- Fetch Pixiv view count, bookmark count, and the current Pixiv account's
  bookmark state whenever a user opens a Pixiv work detail page.
- Use only the viewing user's enabled, healthy Pixiv `RemoteAccount` and keep
  all credential isolation and generation-fencing guarantees.
- Never display import-time metadata as a fallback for volatile remote state.

Remote bookmark mutation, comment counts, background statistics refresh,
cross-provider work state, and automatic discovery import are not part of this
stage.

## Rollout

Generate one independent 32-byte URL-safe base64 `REMOTE_CREDENTIAL_KEY`
without printing it to command output. Store it only in the mode-`0600`
production environment file and the deployment's protected rollback material;
never commit it. Enable only:

```text
REMOTE_DISCOVERY_PRIVATE_MEMBERS_ENABLED=true
REMOTE_DISCOVERY_PIXIV_PREVIEW_ENABLED=true
REMOTE_DISCOVERY_PIXIV_AUTO_IMPORT_ENABLED=false
REMOTE_DISCOVERY_X_ENABLED=false
REMOTE_DISCOVERY_X_AUTO_IMPORT_ENABLED=false
REMOTE_DISCOVERY_BILIBILI_ENABLED=false
REMOTE_DISCOVERY_BILIBILI_AUTO_IMPORT_ENABLED=false
```

Deploy through the existing guarded deployment script so backend, scheduler,
and `worker-operations` receive the new configuration. The discovery worker
must continue to listen on its independent queue. The user will connect and
test the Pixiv refresh token through `/admin/discovery`; no token is supplied
through chat, shell history, logs, Redis, task metadata, manifests, or source
control. Account creation leaves the account untested, so live work state and
manual scan remain unavailable until the user explicitly runs the account test
and it becomes healthy.

## Backend Contract and Data Flow

Add an optional provider work-state capability beside the existing discovery
operations. The typed provider result contains:

```text
RemoteWorkState
  source: "pixiv"
  source_work_id: string
  fetched_at: timezone-aware datetime
  total_views: non-negative integer
  total_bookmarks: non-negative integer
  is_bookmarked: boolean
```

Pixiv implements it with the existing refresh-token authentication flow and a
single `GET https://app-api.pixiv.net/v1/illust/detail?illust_id=...` request.
Response validation rejects missing, negative, boolean-as-integer, or malformed
fields. The provider response and credentials remain redacted in exception
representations and logs.

Expose `GET /api/v1/works/{work_id}/remote-state`, protected by the existing
`library` permission. The service:

1. Applies the same work visibility/NSFW rules as the work detail endpoint.
2. Selects the work's Pixiv source and the current user's one non-deleted,
   enabled Pixiv remote account.
3. Requires Pixiv preview rollout, stored credentials, and `auth_status=healthy`.
4. Pins the account credential generation, decrypts with the existing AES-GCM
   vault and AAD, performs the provider request, then rejects any response whose
   account identity or credential generation changed in flight.
5. Returns the typed state with `Cache-Control: private, no-store`.

There is no successful-path database write and no Redis, process-memory, HTTP,
or browser freshness cache. Each detail-page mount performs a new provider
request. The endpoint never reads, updates, or returns volatile fields from
`raw_metadata`.

Error behavior is safe and typed:

- Missing or hidden work returns `404`.
- Unsupported/non-Pixiv work or missing current-user account returns `409`
  with a stable non-secret error code.
- Closed rollout or unavailable credential vault returns `503`.
- Provider `401/403` or invalid refresh token marks only that user's account
  and bindings unhealthy, then returns a reauthentication-required error.
- Provider `429` returns `429` and a sanitized integer `Retry-After` when
  supplied.
- Timeout, malformed response, and other provider failures return a generic
  `502`; response bodies, URLs with secrets, and provider payloads are not
  logged or returned.

The remote call has a ten-second timeout. Its failure cannot fail the base work,
source, asset, tag, or history requests.

## Frontend Behavior

The existing Remote Discovery navigation remains visible. Once backend rollout
capabilities open Pixiv preview, the page shows the Pixiv account card and
connection dialog; X and Bilibili remain absent. Auto-import controls remain
disabled.

On a Pixiv work detail page, issue the remote-state request after the base work
and source data identify a Pixiv source. Configure the query with zero stale
time, no retry, no refetch-on-focus, and refetch-on-mount so one new request is
made for each page opening without repeated calls while the same page remains
open.

Remove all rendering of `raw_metadata.total_view` and
`raw_metadata.total_bookmarks`. The statistics area independently displays:

- live Pixiv views;
- live Pixiv bookmark count;
- a read-only `Pixiv 已收藏` or `Pixiv 未收藏` state.

The existing auto-gallery local favorite star remains unchanged and is labeled
as a local library action. Loading uses a local skeleton. Missing account,
reauthentication, rate limit, or provider failure renders a compact localized
"实时数据暂不可用" state with the appropriate reconnect hint where relevant;
the rest of the work page stays usable.

## Verification and Acceptance

- Provider tests cover authentication, exact detail endpoint/parameters,
  response mapping, malformed/negative values, `401/403`, `429 Retry-After`,
  and timeout behavior without live network access.
- Service/API tests cover permissions, NSFW visibility, current-user account
  isolation, healthy/enabled requirements, credential generation fencing,
  fail-closed rollout, safe error payloads, `no-store`, and the absence of any
  metadata fallback or statistics persistence.
- Frontend tests prove Pixiv connection becomes available only under the
  effective rollout, every page mount requests remote state, the three live
  values render, local and Pixiv bookmark states remain distinct, and all
  remote failures are non-blocking.
- Existing discovery, credential-security, shared-scheduling, work detail,
  API contract, i18n, typecheck, production build, and Playwright suites remain
  green.
- Deployment verification confirms all services healthy, no OOM/restarts,
  Alembic remains at head, `operations` and `discovery` queues are listening,
  only Pixiv manual preview is effective, and auto import remains closed.
- The automated rollout makes no real Pixiv calls. Acceptance ends with the
  page ready for the user to enter a token, test the account, run a manual scan,
  and open a Pixiv work detail page for the first authorized live request.

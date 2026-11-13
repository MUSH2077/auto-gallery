# auto-gallery

NAS-hosted multi-source media archive and gallery manager, using a FastAPI
backend, PostgreSQL, RQ workers, and a Next.js admin interface.

## Communication (语言规范)

- 默认用中文（简体）回复用户。Reply in Simplified Chinese by default.
- Keep code, identifiers, commit messages, and file content in their original
  language unless the user requests otherwise.

## Maintained project references

Start with [the documentation index](../docs/README.md). Use the maintained
[architecture](../docs/architecture.md), [development guide](../docs/development.md),
[setup guide](../docs/setup.md), and [provider guide](../docs/providers.md) for
current implementation and procedures. Retired audits and task plans are
available in Git history, rather than as instructions for new work.

Dependency versions come from `backend/requirements.txt`,
`admin-web/package.json`, and `admin-web/package-lock.json`; deployment topology
and resource limits come from `docker-compose.yaml` and
[deployment profiles](../docs/deployment-profiles.md). Do not copy old version
numbers, resource budgets, or phase-based feature status into new instructions.

## Stack and services

- Backend: Python 3.12, FastAPI, SQLAlchemy 2 async, Alembic, Pydantic v2.
- Infrastructure: PostgreSQL 16, Redis 7, RQ, Meilisearch.
- Media: gallery-dl, pyvips, ffmpeg.
- Frontend: Next.js 16, React 19, TypeScript 5, Tailwind CSS 4, TanStack Query.
- Deployment: Docker Compose v2 on a NAS or LAN server.

The backend, `worker-download`, `worker-import`, `worker-operations`, and
`scheduler` share the backend image. `admin-web` serves the browser interface;
`postgres`, `redis`, and `meilisearch` are internal data services. Only download
workers execute gallery-dl. Default host ports are 8818 for the backend and
13000 for admin-web; `.env` can override them.

Follow [the setup guide](../docs/setup.md) for installation, migrations, and
upgrades. Use the isolated test procedures in
[the development guide](../docs/development.md) for verification.

## Architecture and providers

Keep API routes thin, business logic in services, database access in
repositories, and response contracts in Pydantic schemas. PostgreSQL job,
lease, artifact, and outbox records own durable state; Redis carries execution
and live notifications. Shared import and task lifecycle services enforce
transaction ordering, attempt fencing, recovery, and idempotency.

Source-specific URL and metadata behavior belongs in `backend/app/providers/`.
The registry has eight remote download providers (Pixiv, X, Iwara, Danbooru,
Weibo, Bilibili, Pinterest, LOFTER), plus Danbooru Reference, Local Folder, and
Manual Upload. Manual uploads and disk recovery have service workflows; do not
infer their availability from an old implementation phase. Read the provider
capabilities and maintained provider guide before changing a source.

Use source-agnostic domain names: `creator`, `source_creator`, `creator_link`,
`subscription`, `subscription_source`, `work`, `work_source`, `asset`,
`asset_source`, `tag`, `work_tag`, and `work_source_tag`. Preserve the existing
unique constraints and shared versus user-scoped ownership model.

## Safety and data constraints

- Never use `shell=True`; subprocess arguments must be lists.
- Validate source URLs with the provider before creating download jobs.
- Enforce authentication and authorization at the API boundary, including
  user ownership and admin permissions; never trust client-side role checks.
- Browser authentication uses revocable Redis sessions. Preserve HttpOnly
  cookies, session-bound CSRF tokens, and exact Origin checks. Bearer JWT is
  supported for independent clients; WebSockets use short-lived single-use
  tickets. See [setup](../docs/setup.md) and [API contracts](../docs/api/README.md).
- Thumbnails require an active browser session, Bearer authentication, or a
  scoped signed ticket. Preview and poster routes use signed URLs; original
  media accepts authentication or a signed URL. Do not make media public.
- Resolve and check filesystem containment with `Path.relative_to()`, never
  string-prefix matching. Keep real NAS paths out of client responses.
- Keep credentials in environment/runtime configuration. Never log database
  passwords, gallery-dl cookies, tokens, or secret configuration.
- Preserve startup secret validation and `.env` exclusion from Git. Follow
  the setup guide for bootstrap credentials and password rotation.
- Keep PostgreSQL, Redis, and Meilisearch on the internal Docker network.
- Deduplication defaults off. Never introduce automatic merges or deletion.
- Preserve originals and existing transaction, lease, and publication fences.
  Download staging, artifact registration, and import publication must use
  shared services; do not bypass them with direct filesystem writes.

Original media belongs under `DOWNLOAD_ROOT`; indexed metadata and derived
media belong under `LIBRARY_ROOT`. gallery-dl settings and cookies use
`GALLERYDL_CONFIG_ROOT`; application runtime settings use `APP_CONFIG_ROOT`.
For storage layouts and cleanup ordering, consult the architecture guide and
current storage services rather than historical directory assumptions.

## Skills and detailed constraints

Use applicable session skills when they help complete the requested work.
User instructions determine scope, verification effort, and whether to commit
or deploy. Read the relevant constraint documents before touching their area:

- [Security](constraints/security.md), [filesystem paths](constraints/filesystem-paths.md),
  and [metadata detection](constraints/metadata-detection.md).
- [Source abstraction](constraints/source-abstraction.md),
  [creator display names](constraints/creator-display-name.md),
  [Danbooru reference](constraints/danbooru-reference.md), and
  [deduplication](constraints/deduplication.md).
- [Docker](constraints/docker.md), [deployment](constraints/deployment.md), and
  [remote access](constraints/remote-access.md).
- [Frontend discipline](constraints/frontend-discipline.md),
  [client API](constraints/client-api.md), and [source colors](constraints/source-colors.md).
- [Technology stack](constraints/tech-stack.md), [verification](constraints/verification.md),
  and [commit checklist](constraints/commit-checklist.md).

These files include legacy phase descriptions. For versions, current auth
behavior, module paths, and deployed features, use the maintained references
above and the implementation. Preserve applicable safety requirements.

## Coding and completion workflow

- Use Pydantic schemas for API inputs and outputs.
- Keep import operations transactional and task transitions idempotent.
- Use shared Redis clients, lock helpers, and centralized cache TTLs.
- Keep provider registration in `init_providers()` and package public exports
  in `__all__`.
- Return per-item batch outcomes instead of silently swallowing failures.
- Use the typed frontend API client and shared TanStack Query keys. Keep pages
  thin and user-facing strings in the shared bilingual i18n catalogs.
- Before changing Next.js behavior, follow `admin-web/AGENTS.md` and read the
  relevant installed Next.js documentation.

Run checks relevant to the change as described in
[CONTRIBUTING.md](../CONTRIBUTING.md) and the development guide. Batch related
edits, inspect Git diffs, and report failures or unavailable checks. Comments
and documentation-only changes need reference and diff checks, not a production
rebuild or a full backend test run.

Commit or deploy only within the user's requested scope. When deployment is
requested, follow [the runbook](../docs/RUNBOOK.md), the release checklist, and
applicable rollout guides, including rollback and data-safety requirements.
Do not automatically deploy, commit all unrelated changes, or claim that
unexecuted checks passed.

# Contributing

Thanks for helping improve auto-gallery. This project is a self-hosted media
archive manager, so changes should favor reliability, clear failure states, and
respect for source platform rules.

## Development Setup

```bash
cp .env.example .env
docker compose up -d postgres redis meilisearch
docker compose build backend admin-web
docker compose up -d backend worker-download worker-import stream-import scheduler admin-web
```

Run backend tests:

```bash
cd backend
pytest
```

Run the admin build:

```bash
cd admin-web
npm ci
npm run build
```

## Scripts

<!-- AUTO-GENERATED: scripts reference -->

| Command | Description |
|---------|-------------|
| `npm run dev` | Start Next.js dev server with hot reload (port 3000) |
| `npm run build` | Production build with type checking |
| `npm run start` | Start production Next.js server (port 3000) |
| `npm run typecheck` | Run TypeScript type checking (`tsc --noEmit`) |
| `npm run generate:api-types` | Generate TypeScript types from backend OpenAPI spec |
| `bash scripts/deploy.sh` | Single-command deploy: detect changes → build → restart → wait healthy → verify |
| `bash scripts/debug.sh <mode>` | Debug toolkit: `quick`, `backend`, `download`, `storage`, `proxy` |
| `bash scripts/generate-env.sh` | Generate `.env` with random service secrets |
| `bash scripts/verify-runtime.sh` | End-to-end runtime verification (health, API, media endpoints) |
| `bash scripts/smoke-test.sh` | Full pipeline smoke test (download → import → verify) |
| `bash scripts/package-release.sh` | Package a release tarball for distribution |
| `bash scripts/privacy-scan.sh` | Scan for secrets, PII, and internal references before release |

<!-- /AUTO-GENERATED -->

## Services

| Service | Role | Queue / Mode |
|---------|------|--------------|
| `backend` | FastAPI API server | — |
| `worker-download` | RQ worker — gallery-dl subprocess | `downloads:{source}` per-source queues |
| `worker-import` | RQ worker — metadata import, batch import | `imports` |
| `stream-import` | Redis Stream consumer — event-driven import | `work:ready` stream (2 consumers) |
| `scheduler` | RQ scheduler — subscription sync loop | `scheduled` |
| `admin-web` | Next.js admin interface | — |
| `postgres`, `redis`, `meilisearch` | Data services | — |

Backend, workers, stream-import, and scheduler share the same Docker image.
Only worker-download executes gallery-dl.

## Pull Request Guidelines

- Keep backend API, database, and frontend changes clearly separated when
  possible.
- Add tests for provider contracts, scheduler behavior, and job state changes.
- Do not commit `.env`, cookies, downloads, library data, database dumps, or
  screenshots containing private account information.
- For new providers, document URL validation, gallery-dl config, auth
  requirements, and metadata parsing behavior.
- For UI changes, keep the GitHub-like admin style: dense, calm, accessible,
  and easy to scan.
- Job state machine transitions must follow the defined FSM (`pending →
  downloading → downloaded → importing → complete | failed | stale | paused`).
  Never add ad-hoc transitions without updating `download_job` state logic.
- Per-source download queues use the pattern `downloads:{source}`. New sources
  must be added to `worker-download` command in `docker-compose.yaml`.
- Redis Stream consumer groups (`work:ready`) handle event-driven import.
  Batch import still goes through the `imports` RQ queue.

## Legal and Platform Boundaries

Contributions must not encourage bypassing paywalls, DRM, access controls, or
platform restrictions. auto-gallery should only help users archive content they
are authorized to access and download.

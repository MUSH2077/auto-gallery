# auto-gallery

[中文版本](README.zh.md)

![License](https://img.shields.io/badge/license-AGPL--3.0--only-blue)
![Status](https://img.shields.io/badge/status-beta-yellow)
![Deploy](https://img.shields.io/badge/deploy-Docker%20Compose-2496ed)
![Backend](https://img.shields.io/badge/backend-pytest-2ea44f)
![Admin](https://img.shields.io/badge/admin-Next.js%2014-black)
![LAN First](https://img.shields.io/badge/security-LAN--first-57606a)

Self-hosted, LAN-first media archive and gallery manager for creator-based
collections.

auto-gallery downloads, imports, indexes, and manages media from multiple
sources through a Docker Compose stack. It is built for personal NAS and Linux
hosts, with a GitHub-like admin UI for creators, source URLs, works,
subscriptions, jobs, search, and gallery-dl configuration.

> Beta notice: auto-gallery is intended for self-hosted personal archives. Do
> not expose the admin web or backend API directly to the public internet.

## Screenshots

| Dashboard | Creator detail |
|---|---|
| ![Sanitized dashboard demo](docs/assets/dashboard.svg) | ![Sanitized creator detail demo](docs/assets/creator-detail.svg) |

| Repositories | Work detail | gallery-dl settings |
|---|---|---|
| ![Sanitized repositories demo](docs/assets/repositories.svg) | ![Sanitized work detail demo](docs/assets/works-detail.svg) | ![Sanitized gallery-dl settings demo](docs/assets/gallerydl-settings.svg) |

The SVGs above are sanitized demo screenshots built from fictional data. They
do not include private creators, credentials, local paths, or downloaded media.

## Highlights

- Creator-first archive model: one canonical creator can link many platform
  accounts.
- GitHub-like creator pages: profile, activity, works, links, and repository
  style subscription URLs.
- Source URL repositories: every valid gallery-dl subscription URL is treated
  as an independent sync unit.
- gallery-dl worker isolation: downloads run in background workers, not inside
  API request handlers.
- Import pipeline: metadata parsing, hashing, tagging, thumbnails, search
  indexing, and media integrity checks.
- Admin workflows: subscriptions, scheduler, jobs, batch actions, dedup
  candidates, auth health, proxy settings, backup/restore, and data management.
- LAN-first security: JWT admin login, ignored `.env`, no public exposure
  recommendation for v1.

## Supported Sources

| Source | Download | Auth | Status |
|---|---|---|---|
| Pixiv | gallery-dl | Refresh token or cookies | Supported |
| X/Twitter | gallery-dl `twitter` extractor | Cookies | Supported |
| Iwara | gallery-dl | Username/password or cookies | Supported |
| Danbooru | gallery-dl | API key, username/password, or cookies | Supported |
| Weibo | gallery-dl | Optional cookies | Supported |
| Bilibili | gallery-dl | Public content | Supported |
| Pinterest | gallery-dl | Public content | Supported |
| LOFTER | gallery-dl | Public content | Supported |
| Local folder | Direct import | Local path | Planned |
| Manual upload | Admin web | Admin user | Planned |

Provider compatibility depends on gallery-dl and on source platform behavior.
See [docs/providers.md](docs/providers.md) and
[docs/gallerydl-config.md](docs/gallerydl-config.md).

## Quick Start

```bash
git clone <repo-url> auto-gallery
cd auto-gallery

scripts/generate-env.sh
# Review .env: set ports, timezone, and host paths.
# ADMIN_PASSWORD may stay change-me-admin for first login; the UI forces a change.

docker compose up -d
docker compose exec backend alembic upgrade head

curl http://localhost:8818/api/v1/system/health
```

On first startup, the backend creates `data/config/gallery-dl/config.json`
with safe default file organization patterns. Edit them later in
**Settings -> gallery-dl Config -> File Organization**.

Open the admin web at:

```text
http://<host>:13000
```

For a full deployment guide, see [docs/setup.md](docs/setup.md).

## First Deployment Checklist

- Run `scripts/generate-env.sh`, or copy `.env.example` to `.env` and replace
  `POSTGRES_PASSWORD`, `REDIS_PASSWORD`, `MEILI_MASTER_KEY`, and `SECRET_KEY`.
- First login is `admin / change-me-admin`; you may also set a custom
  `ADMIN_PASSWORD` before deployment. The UI forces a password change after login.
- Choose host paths for downloads, library, app config, gallery-dl config, and
  service data.
- Set `TIMEZONE` for scheduler behavior.
- If using a reverse proxy, ensure `/api/v1/ws` forwards WebSocket upgrades; otherwise set `NEXT_PUBLIC_WS_URL` and rebuild admin-web. If WebSocket is unavailable, the Jobs page falls back to polling so task status still refreshes.
- Start Docker Compose.
- Run Alembic migrations.
- Confirm backend health at `/api/v1/system/health`.
- Log in to the admin web.
- Configure gallery-dl credentials and file organization in Settings.
- Create creators and subscription URLs, then run a small sync first.
- Confirm backups and restore expectations before large imports.

## Architecture

```text
Sources
  -> gallery-dl worker jobs
    -> gallery-dl job output
      -> Original Media Store (DOWNLOAD_ROOT)
        -> Library Index (LIBRARY_ROOT metadata + thumbnails)
          -> PostgreSQL canonical data
          -> Meilisearch full-text index
          -> Next.js admin web
```

Core models are source-agnostic: `creator`, `work`, `asset`, `tag`, and
subscription sources. Provider modules own URL validation, normalization,
gallery-dl configuration, and metadata parsing.

Read more in [docs/architecture.md](docs/architecture.md).

## Documentation

- [Setup](docs/setup.md)
- [Architecture](docs/architecture.md)
- [Provider guide](docs/providers.md)
- [gallery-dl configuration](docs/gallerydl-config.md)
- [Development](docs/development.md)
- [Distribution and privacy](docs/distribution.md)
- [Risk register](docs/risks.md)
- [Release checklist](docs/release-checklist.md)
- [Security policy](SECURITY.md)
- [Contributing](CONTRIBUTING.md)

## Known Limitations

- Source sites can change without warning, which may break gallery-dl
  extractors.
- gallery-dl version changes can affect output metadata and parser behavior.
- Cookies, refresh tokens, and platform credentials must be maintained by the
  user.
- Large first syncs can take a long time and heavy disk I/O on NAS hardware.
- Search can lag behind PostgreSQL until reindexing completes.
- v1 is LAN-first and admin-oriented; it is not a public multi-tenant service.

## Legal and Responsible Use

auto-gallery is an archive manager for content you are authorized to access and
download. You are responsible for following source platform terms, copyright
law, and local law.

This project does not encourage bypassing paywalls, DRM, access controls, rate
limits, or platform restrictions. Do not publish cookies, credentials, private
creator URLs, private media, or database dumps in issues or pull requests.

## Roadmap

- Add more sanitized demo screenshots and short workflow videos.
- Improve provider compatibility smoke tests.
- Add richer backup/restore validation.
- Add local folder import and manual upload flows.
- Add optional remote client workflows after the LAN-first admin experience is
  stable.

## License

auto-gallery is licensed under `AGPL-3.0-only`. See [LICENSE](LICENSE).

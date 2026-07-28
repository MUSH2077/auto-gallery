# auto-gallery

[简体中文](README.zh.md)

[![CI](https://github.com/MUSH2077/auto-gallery/actions/workflows/ci.yml/badge.svg)](https://github.com/MUSH2077/auto-gallery/actions/workflows/ci.yml)
[![CodeQL](https://github.com/MUSH2077/auto-gallery/actions/workflows/codeql.yml/badge.svg)](https://github.com/MUSH2077/auto-gallery/actions/workflows/codeql.yml)
[![OpenSSF Scorecard](https://api.securityscorecards.dev/projects/github.com/MUSH2077/auto-gallery/badge)](https://securityscorecards.dev/viewer/?uri=github.com/MUSH2077/auto-gallery)
[![License: AGPL-3.0-only](https://img.shields.io/badge/license-AGPL--3.0--only-blue)](LICENSE)
[![Status: public beta](https://img.shields.io/badge/status-public%20beta-f59e0b)](CHANGELOG.md)

Self-hosted, LAN-first media archiving for creator-based collections.

auto-gallery downloads, imports, indexes, and manages media from multiple
sources through a Docker Compose stack. It is designed for personal NAS and
Linux hosts and includes an accessible admin UI for creators, repositories,
works, subscriptions, jobs, search, scheduling, deduplication, and gallery-dl
configuration.

> [!WARNING]
> auto-gallery is a public beta for trusted self-hosted environments. Do not
> expose the admin web or backend API directly to the internet. Use a reverse
> proxy, TLS, access controls, and a threat model appropriate for your network.

## Why auto-gallery?

- **Creator-first organization.** One canonical creator can link identities
  and repositories from multiple platforms.
- **Independent repository sync.** Every supported gallery-dl subscription URL
  is an observable, independently scheduled sync unit.
- **Original media preservation.** Downloads remain the source of truth while
  PostgreSQL, thumbnails, and Meilisearch provide a browsable library index.
- **Background processing.** Download, import, and maintenance work run in
  isolated workers instead of API request handlers.
- **Operational visibility.** Health, schedules, queues, storage, logs,
  backups, source authentication, and data checks are available in the admin UI.
- **Cross-source asset deduplication.** Visual assets are compared across
  sources, with ambiguous matches routed to review instead of silently merged.
- **Bilingual and accessible.** Chinese and English UI, keyboard navigation,
  responsive layouts, dark mode, and reduced-motion support.

## Screenshots

| Dashboard | Creator detail |
|---|---|
| ![Sanitized dashboard demo](docs/assets/dashboard.svg) | ![Sanitized creator detail demo](docs/assets/creator-detail.svg) |

| Repositories | Work detail | gallery-dl settings |
|---|---|---|
| ![Sanitized repositories demo](docs/assets/repositories.svg) | ![Sanitized work detail demo](docs/assets/works-detail.svg) | ![Sanitized gallery-dl settings demo](docs/assets/gallerydl-settings.svg) |

These SVGs use fictional data. They contain no credentials, local paths,
private creators, or downloaded media.

## Supported sources

Provider compatibility depends on gallery-dl and can change when a source site
changes. See the [provider guide](docs/providers.md) for current limitations.

| Source | Download | Authentication | Status |
|---|---|---|---|
| Pixiv | gallery-dl | Refresh token or cookies | Supported |
| X/Twitter | gallery-dl `twitter` extractor | Cookies | Supported |
| Iwara | gallery-dl | Username/password or cookies | Supported |
| Danbooru | gallery-dl | API key, username/password, or cookies | Supported |
| Weibo | gallery-dl | Optional cookies | Supported |
| Bilibili | gallery-dl | Public content | Supported |
| Pinterest | gallery-dl | Public content | Supported |
| LOFTER | gallery-dl | Public content | Supported |
| Manual upload | Admin web | Admin user | Supported |
| Local folder | Direct import | Local path | Experimental |

## Quick start

### Requirements

- Docker Engine with Docker Compose v2
- A Linux host or NAS with persistent storage
- Sufficient disk capacity for original media, thumbnails, and backups

```bash
git clone https://github.com/MUSH2077/auto-gallery.git
cd auto-gallery

bash scripts/generate-env.sh
# Review .env, especially host paths, ports, timezone, and ADMIN_PASSWORD.

docker compose up -d --build
curl http://localhost:8818/api/v1/system/health
```

Database migrations run automatically when the backend starts. Open the admin
web at `http://<host>:13000`, change the initial password immediately, then
configure gallery-dl credentials and file organization in Settings.

For reverse proxies, storage layouts, upgrades, and troubleshooting, follow the
[complete setup guide](docs/setup.md).

## Architecture

```text
Sources
  -> gallery-dl download workers / manual upload
    -> Original Media Store (DOWNLOAD_ROOT)
      -> import and asset processing
        -> Library Index (metadata + thumbnails)
        -> PostgreSQL canonical data
        -> Meilisearch full-text index
        -> Next.js admin web
```

Core models are source-agnostic: creators, works, assets, tags, and subscription
sources. Provider modules own URL validation and normalization, gallery-dl
configuration, and metadata parsing. Read the
[architecture guide](docs/architecture.md) before changing these boundaries.

## Documentation

- [Documentation index](docs/README.md)
- [Setup and upgrades](docs/setup.md)
- [Architecture](docs/architecture.md)
- [Provider development](docs/providers.md)
- [Development](docs/development.md)
- [Security policy](SECURITY.md)
- [Contributing](CONTRIBUTING.md)
- [Project governance](GOVERNANCE.md)
- [Support](SUPPORT.md)
- [Changelog](CHANGELOG.md)

## Project status

auto-gallery is pre-1.0. Database migrations, provider behavior, and deployment
settings may change between beta releases. Back up both media and application
state before upgrading.

Known limitations:

- Source sites can change without warning and break gallery-dl extractors.
- Cookies and platform credentials require ongoing maintenance by the operator.
- Large first syncs can take significant time and disk I/O on NAS hardware.
- Search may briefly lag PostgreSQL until indexing completes.
- The product is admin-oriented and is not a public multi-tenant media service.

The near-term roadmap is tracked through
[GitHub issues](https://github.com/MUSH2077/auto-gallery/issues) and focuses on
provider compatibility tests, restore validation, local-folder import, and a
stable first tagged beta.

## Responsible use

Use auto-gallery only for content you are authorized to access and archive. You
are responsible for source-platform terms, copyright law, and local law.

The project does not encourage bypassing paywalls, DRM, access controls, rate
limits, or platform restrictions. Never publish cookies, credentials, private
creator URLs, private media, database dumps, or unsanitized logs in issues or
pull requests.

## Community and license

Please read the [contribution guide](CONTRIBUTING.md) and
[Code of Conduct](CODE_OF_CONDUCT.md) before participating. Security issues
must be reported through the private process in [SECURITY.md](SECURITY.md).

auto-gallery is licensed under
[GNU Affero General Public License v3.0 only](LICENSE).

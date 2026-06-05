# Contributing

Thanks for helping improve auto-gallery. This project is a self-hosted media
archive manager, so changes should favor reliability, clear failure states, and
respect for source platform rules.

## Development Setup

```bash
cp .env.example .env
docker compose up -d postgres redis meilisearch
docker compose build backend admin-web
docker compose up -d backend worker scheduler admin-web
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

## Legal and Platform Boundaries

Contributions must not encourage bypassing paywalls, DRM, access controls, or
platform restrictions. auto-gallery should only help users archive content they
are authorized to access and download.

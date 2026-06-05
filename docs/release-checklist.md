# Release Checklist

Use this checklist before tagging a public release.

## Version and Changelog

- Choose a version tag, for example `v0.1.0-beta.1`.
- Update `CHANGELOG.md`.
- Confirm whether the release includes database migrations.
- Call out provider compatibility changes.

## Documentation

- Verify `README.md` and `README.zh.md` describe the current UI and providers.
- Confirm `docs/assets/` screenshots are sanitized demo or sanitized real
  screenshots with no private creators, credentials, local paths, account
  names, or downloaded media.
- Confirm README screenshot alt text and captions describe the assets as
  sanitized demo or sanitized real screenshots.
- Confirm the Sources admin page descriptions match provider capabilities.
- Confirm `.env.example` includes every required deployment variable.
- Confirm legal and safety notes are visible in the README.

## Local Checks

```bash
cd backend && pytest
cd ../admin-web && npm ci && npm run build
cd ..
docker compose build backend admin-web
docker compose up -d backend worker scheduler admin-web
docker compose ps backend worker scheduler admin-web
curl -I http://localhost:13000/admin
```

Expected result:

- `backend`, `worker`, `scheduler`, and `admin-web` are healthy.
- `http://localhost:13000/admin` returns `200`.
- No `.env`, cookies, private media, database dumps, or local runtime data are tracked.

## Publishing

- Tag the release.
- Attach release notes from `CHANGELOG.md`.
- Mention known limitations and any migration steps.
- Ask users to report deployment issues with sanitized logs only.

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
cd backend
python -m pytest
ruff check app tests

cd ../admin-web
npm ci
npm run typecheck
npm run build

cd ..
docker compose --env-file .env.ci config --quiet
docker compose build backend admin-web
docker compose up -d
scripts/verify-runtime.sh
curl -I http://localhost:13000/admin
```

Expected result:

- All Compose services are healthy.
- `http://localhost:13000/admin` returns `200`.
- No `.env`, cookies, private media, database dumps, or local runtime data are tracked.
- Release package dry run only includes tracked files and excludes runtime/cache paths.

## Publishing

- Tag the release.
- Attach release notes from `CHANGELOG.md`.
- Mention known limitations and any migration steps.
- Ask users to report deployment issues with sanitized logs only.

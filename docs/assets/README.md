# README assets

This directory contains sanitized screenshots and fictional media fixtures used
by the English and Chinese README files.

## Regenerate

From `admin-web/`, run:

```bash
PLAYWRIGHT_CHROMIUM_EXECUTABLE=/path/to/chromium npm run screenshots:readme
```

The command starts an isolated local Next.js server, fixes the clock to
2026-07-28, selects English and dark mode, uses a 1440×1024 viewport, requests
reduced motion, and writes these seven PNGs:

- `admin-dashboard.png`
- `works-library.png`
- `creators.png`
- `repository-detail.png`
- `tag-bubbles.png`
- `asset-dedup-review.png`
- `jobs-operations.png`

## Data isolation

`admin-web/tests/e2e/readme-screenshots.spec.ts` intercepts every API and media
request. All visible identities, URLs, counts, dates, jobs, repositories, tags,
and permissions are fictional. Any unknown API or media request receives an
error and fails the screenshot test; requests are never allowed to fall back
to the real backend or `/data`.

The three committed WebP files under `fixtures/` were generated specifically
for documentation. They contain no people, text, logos, private media, or
downloaded source content:

- `fixtures/harbor-light-study.webp`
- `fixtures/summer-color-notes.webp`
- `fixtures/rainy-evening-drive.webp`

## Privacy checklist

Before committing regenerated screenshots:

1. Run `npm run screenshots:readme` and confirm the strict fixture test passes.
2. Inspect all seven images at full resolution.
3. Confirm there are no real creator names, account identifiers, credentials,
   cookies, local paths, private URLs, or personal media.
4. Run `bash scripts/privacy-scan.sh` from the repository root.

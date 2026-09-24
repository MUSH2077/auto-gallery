# Release Checklist

Use this checklist before changing repository visibility and before every
public tag.

## Release content

- [ ] Choose a SemVer tag such as `v0.1.0-beta.1`.
- [ ] Update `CHANGELOG.md` and move relevant Unreleased items into the release.
- [ ] Document database migrations, backfills, reindexing, configuration
      changes, and provider compatibility changes.
- [ ] Verify `README.md`, `README.zh.md`, setup, provider, and development docs.
- [ ] Confirm screenshots use fictional or fully sanitized data.
- [ ] Confirm `.env.example` describes every required deployment variable.
- [ ] Confirm remote discovery rollout flags remain fail-closed in the release
      environment and that `REMOTE_CREDENTIAL_KEY` is backed up separately.

## Source and privacy

- [ ] Run `bash scripts/privacy-scan.sh`.
- [ ] Review Git history for tracked secrets, environment files, database
      dumps, private media, local account data, and oversized objects.
- [ ] Review old pull requests, workflow logs, artifacts, caches, and release
      assets before making a private repository public.
- [ ] Rotate any credential that may have appeared in Git or CI history.
- [ ] Build the source archive and inspect its file list:

  ```bash
  archive="$(bash scripts/package-release.sh v0.1.0-beta.1)"
  tar -tzf "$archive" | less
  ```

## Verification

```bash
docker compose run --rm -T --volume "$PWD/backend:/app" \
  -e PYTHONDONTWRITEBYTECODE=1 backend \
  ruff check app tests
docker compose run --rm -T --volume "$PWD/backend:/app" \
  -e PYTHONDONTWRITEBYTECODE=1 backend \
  python -m pytest

cd admin-web
npm ci
npm run typecheck
npm run build
npm run test:e2e

cd ..
docker compose --env-file .env.ci config --quiet
docker compose --env-file .env.ci build backend admin-web
docker compose --env-file .env.ci up -d
COMPOSE_ENV_FILE=.env.ci bash scripts/verify-runtime.sh
docker compose --env-file .env.ci down -v
```

- [ ] All services become healthy.
- [ ] Admin login, a small authorized sync, import, search, media preview,
      backup creation, and restore validation were exercised.
- [ ] Upgrade and rollback were tested when the release contains migrations.
- [ ] Stability/performance releases completed the disposable 70k-intent /
      100k-asset benchmark and recorded a production-data read-only sample.
- [ ] Works-control releases completed the 70k-work heat/random first-page and
      cursor benchmark, recorded the existing-sort baseline comparison, and
      followed `works-page-controls-rollout.md`.
- [ ] Gitllery remains `shadow` unless the separate 24-hour incremental gate in
      `stability-performance-rollout.md` has complete reviewed evidence.
- [ ] The seven-day soak is recorded as pending or complete; it is never
      inferred from automated tests.
- [ ] For multi-user discovery schema, application rollback retains additive
      tables and summary caches; no production Alembic downgrade drops them.
- [ ] Every migration is backward-compatible with the previous application
      image, and the generated schema-forward rollback was tested at both the
      pre-deploy and candidate revisions. Candidate-schema rollback skipped old
      migrate and produced an auditable `rollback-receipt.env`.
- [ ] Normal pytest skipped live-provider smoke tests and made no provider
      network calls. Any explicitly opted-in smoke used dedicated test accounts.

## GitHub controls

- [ ] CI, CodeQL, and Scorecard are green on the default branch.
- [ ] Default-branch and immutable-release-tag rulesets are active.
- [ ] Required status-check names match the live workflow check names.
- [ ] Dependabot alerts, secret scanning, push protection, and private
      vulnerability reporting are enabled.
- [ ] About text, topics, license, issue forms, CODEOWNERS, and social preview
      render correctly.

## Publish and observe

- [ ] Create the signed or annotated release tag from the protected default branch.
- [ ] Publish release notes from `CHANGELOG.md` with upgrade and rollback steps.
- [ ] Attach only inspected artifacts and their checksums.
- [ ] Verify the release and documentation in a logged-out browser.
- [ ] Monitor installation, migration, and provider reports after publishing.

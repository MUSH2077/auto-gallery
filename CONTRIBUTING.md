# Contributing to auto-gallery

[简体中文](CONTRIBUTING.zh.md)

Thank you for helping improve auto-gallery. Contributions should favor data
safety, observable failure modes, accessible interfaces, and respect for
source-platform rules.

## Before you start

- Search existing issues and pull requests before opening a duplicate.
- Use the issue forms for bugs, deployment help, provider requests, and larger
  feature proposals.
- Report vulnerabilities privately as described in [SECURITY.md](SECURITY.md).
- For a large architectural change, open an issue first so its boundaries and
  migration path can be agreed before significant implementation work.

Small documentation fixes, focused bug fixes, and test improvements can go
directly to a pull request.

## Development setup

1. Fork the repository and create a focused branch from the default branch.
2. Generate local secrets and start the stack:

   ```bash
   bash scripts/generate-env.sh
   docker compose up -d --build
   ```

3. Confirm the backend health endpoint responds:

   ```bash
   curl http://localhost:8818/api/v1/system/health
   ```

See [docs/development.md](docs/development.md) for local iteration, migrations,
provider development, and the frontend structure.

## Branches and commits

Use short, descriptive branch names such as:

- `fix/x-metadata-import`
- `feat/repository-tag-view`
- `docs/public-installation`

Write commits in the imperative mood and keep unrelated changes separate.
Conventional prefixes such as `feat:`, `fix:`, `docs:`, `test:`, and
`refactor:` are encouraged but not required. Maintainers may squash a pull
request; make the PR title suitable for the final changelog entry.

## Required checks

Run the checks relevant to your change before opening a pull request.

Backend:

```bash
docker compose run --rm -T --volume "$PWD/backend:/app" \
  -e PYTHONDONTWRITEBYTECODE=1 backend \
  python -m pytest
docker compose run --rm -T --volume "$PWD/backend:/app" \
  -e PYTHONDONTWRITEBYTECODE=1 backend \
  ruff check app tests
```

Admin web:

```bash
npm --prefix admin-web ci
npm --prefix admin-web run typecheck
npm --prefix admin-web run build
npm --prefix admin-web run test:e2e
```

Repository and deployment:

```bash
docker compose --env-file .env.ci config --quiet
bash scripts/privacy-scan.sh
bash scripts/package-release.sh ci
```

The end-to-end suite requires Playwright browsers. If a check cannot run in
your environment, say exactly which check was skipped and why in the pull
request.

## Change requirements

### Backend and database

- Put business logic in services and database access in repositories.
- Use Pydantic schemas for API inputs and outputs.
- Keep import and maintenance operations idempotent.
- Add tests for API contracts, provider parsing, task transitions, and
  regression cases.
- Review generated Alembic migrations. Test both upgrade and downgrade for
  schema changes and disclose any data-loss risk.
- Do not add ad-hoc download-job transitions; update the state machine and its
  tests together.

### Providers

- Document URL validation, normalization, authentication, gallery-dl
  configuration, metadata mapping, and representative sanitized fixtures.
- Never commit real cookies or private content URLs.
- Keep provider modules independent from database access.
- Add the provider to the worker queue configuration where required.

### Admin web

- Reuse shared navigation, page-shell, feedback, list, and overflow primitives.
- Support mouse, keyboard, and touch; do not hide the only critical action
  behind hover.
- Test narrow viewports, text expansion, Chinese and English, light and dark
  themes, and reduced-motion mode.
- Add every user-facing string in both languages.
- Update screenshots or documentation when a workflow materially changes.

### Documentation

- Update English and Chinese documents together when both variants exist.
- Use fictional or fully sanitized examples.
- Keep commands runnable from the documented working directory.

## Pull request expectations

A reviewable pull request:

- explains the user problem and the chosen boundary;
- links the related issue when one exists;
- calls out migrations, configuration changes, and compatibility impact;
- includes regression tests or explains why tests are not applicable;
- includes accessible screenshots or recordings for visual changes;
- contains no secrets, private creator data, local absolute paths, or runtime
  media.

Keep pull requests focused. Maintainers may ask to split a broad change before
review. Resolve review conversations or explain the remaining disagreement
before merge.

Substantial AI-assisted contributions are welcome, but disclose that assistance
in the PR, review every generated line, and remain responsible for correctness,
licensing, security, and tests.

## Licensing

By submitting a contribution, you agree that it is licensed under the
repository's [AGPL-3.0-only license](LICENSE). Only submit work you have the
right to license. The project does not currently require a separate contributor
license agreement.

## Legal and platform boundaries

Contributions must not encourage bypassing paywalls, DRM, access controls, rate
limits, or platform restrictions. auto-gallery should only help operators
archive content they are authorized to access and download.

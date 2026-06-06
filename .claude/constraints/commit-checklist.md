# Commit Checklist Constraint

## Rule

Every commit must pass a self-check before `git commit`. The depth of the checklist depends on what changed.

## Quick check (all commits)

- [ ] Code compiles / builds without errors
- [ ] No leftover debug prints, console.log, or commented-out code
- [ ] Commit message describes WHY, not just WHAT
- [ ] Commit message follows format: `type: description` (feat:, fix:, refactor:, docs:, chore:)

## Backend code change checklist

- [ ] Docker image builds: `docker compose build backend`
- [ ] All three shared-image containers restarted: `docker compose up -d --force-recreate backend worker scheduler`
- [ ] Backend starts without import errors: `docker compose logs backend --tail 5`
- [ ] Worker starts and listens: `docker compose logs worker --tail 5`
- [ ] Scheduler starts and listens: `docker compose logs scheduler --tail 5`
- [ ] New function/class importable in container: `docker compose exec worker python3 -c "from app.jobs.xxx import yyy"`
- [ ] Existing tests pass: `docker compose exec backend python -m pytest` (if tests exist)
- [ ] Alembic migration applied if schema changed: `docker compose exec backend alembic upgrade head`

## Backend job logic change (download/import/sync)

All of the above, plus:

- [ ] End-to-end test: triggered a real download, verified import completed
- [ ] Output verified: correct file format, correct DB records
- [ ] Error case tested: invalid URL, no-new-content source
- [ ] Worker/scheduler logs show no unexpected errors

## Frontend change checklist

- [ ] Builds without errors: `cd admin-web && npx next build`
- [ ] No TypeScript errors
- [ ] All new text has i18n keys in both `zh` and `en`
- [ ] No duplicated constants (colors, configs) — import from shared modules
- [ ] Dark mode: tested both light and dark appearance
- [ ] API types match backend response shape

## API change (routes/schemas)

All of the above, plus:

- [ ] Endpoint tested with curl: 200, 400, 404 cases
- [ ] Response matches Pydantic schema
- [ ] Frontend types updated if response shape changed
- [ ] Authentication still enforced (admin routes require token)

## Documentation change

- [ ] English and Chinese docs updated together (if applicable)
- [ ] CLAUDE.md updated if constraint/skill files added
- [ ] Commit message documents the doc change clearly

## Anti-patterns

- ❌ "It's a small change" — skipping the checklist
- ❌ "I'll test it later" — committing untested code
- ❌ "The build passed" — as the ONLY verification
- ❌ Not checking worker/scheduler logs before committing job logic changes

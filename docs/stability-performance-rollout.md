# Stability and performance rollout

This runbook is the release gate for the stability/performance maintenance
branch. It does not authorize RAG, a generic plugin system, Eagle V5 coupling,
Immich synchronization, or new library-management features.

## Non-negotiable starting state

- `GITLLERY_PROJECTION_MODE=shadow`.
- `GITLLERY_ACTIVE_VERIFIED_GENERATION` is empty.
- PostgreSQL is authoritative; original media and legacy `.gitllery` trees are
  not modified by acceptance tests.
- The candidate exposes the same non-empty `build_revision` from backend,
  scheduler, workers, and admin web.
- A checked deployment rollback point exists before migrations run.

The ordinary deployment path deliberately forces Gitllery to `shadow`. Active
projection is a later, separately reviewed operational change after every gate
below has evidence.

## Release order

1. **Candidate and rollback point.** Run the full disposable acceptance suite,
   export OpenAPI, regenerate client types, build the production frontend, and
   use `scripts/deploy.sh --verified <acceptance.json>`. Confirm the rollback
   directory and build revision before starting workers.
2. **Authentication and schema hot paths.** Apply the additive migrations. In
   particular, confirm `ix_storage_artifacts_import_job_id` and
   `ix_gitllery_projection_targets_intent_id` are valid. Confirm the workbench
   no longer treats disabled or unchecked sources as actionable failures.
3. **Queue control plane.** Start the scheduler and workers through the normal
   workers-last deploy gate. Confirm write-side wakes, 60-second fallback,
   successor handoff, and 30-day bounded cleanup. Gitllery remains excluded
   from ordinary readiness while in `shadow`.
4. **Frontend candidate.** Confirm login, dashboard, works, jobs, creators,
   media viewer, task drawer, adaptive polling, and the production bundle
   budgets. Hidden tabs must stop polling and an idle tab must make no more
   than four recurring API requests per minute.
5. **Gitllery canary.** Queue `POST /api/v1/curation/gitllery/builds` with
   `scope=canary`. The service selects the smallest, median, and largest
   repositories. Queue verification through
   `POST /api/v1/curation/gitllery/builds/{build_id}/verify`; provide reviewed
   restore-dry-run and foreground-API regression evidence. Do not promote.
6. **Full shadow build.** Only after the canary is `complete` with all gates
   passing, queue `scope=full`. Its fixed high-water mark must remain stable;
   commits after that mark remain pending. Verify every repository and confirm
   exact commit/change counts before the service settles intents through the
   high-water mark.
7. **Incremental observation and activation review.** Observe the verified
   generation for at least 24 continuous hours, recording new-commit replay,
   duplicate checks, restarts, memory, storage, build duration, and foreground
   API regression. Promotion and `active` require a separately reviewed record
   with `incremental_soak_hours >= 24`; no HTTP request automatically promotes
   a generation.
8. **Seven-day soak.** After the application release—and, if separately
   approved, Gitllery activation—complete the checklist below before declaring
   the rollout stable.

## Performance acceptance

The repeatable disposable check is:

```bash
cd backend
python scripts/benchmark_stability_hot_paths.py \
  --mode synthetic --confirm-disposable \
  --assets 100000 --intents 70000 --repositories 804 \
  --enforce-targets
```

It inserts the fixture in one transaction and always rolls it back. A remote
disposable database additionally requires `--allow-non-loopback-disposable`.
Never pass those flags to a production database.

For a current-data diagnostic, run the same script with
`--mode live-read-only`. That mode begins with `SET TRANSACTION READ ONLY`,
keeps Gitllery cache simulation in process, and never starts build, verify,
promotion, or settlement work.

The acceptance limits are:

| Surface | p95 limit |
|---|---:|
| Outbox readiness | 50 ms |
| Dedup candidate lookup | 100 ms |
| Import-job delete / read-only plan | 200 ms |
| Cached workbench | 500 ms |
| Ordinary cached Gitllery status | 200 ms |

The 2026-09-21 read-only production-shaped sample contained 104,786 assets,
362,369 storage artifacts, 70,769 pending Gitllery intents, and 804
repositories. It measured 35.3 ms, 65.1 ms, 38.3 ms, 1.8 ms, and 0.07 ms
respectively; Gitllery's uncached database read was 153.5 ms. The live schema
had not yet received `ix_storage_artifacts_import_job_id`, so the disposable
post-migration fixture is the deletion execution evidence (29.2 ms p95).

## Rollback switches

Use the generated schema-forward application rollback; do not downgrade the
database or restore a dump for an ordinary application regression:

```bash
/volume2/docker/auto-gallery-deployments/<deployment-id>/rollback.sh
```

Keep or restore these fail-closed Gitllery values before recreating any app
process:

```dotenv
GITLLERY_PROJECTION_MODE=shadow
GITLLERY_ACTIVE_VERIFIED_GENERATION=
```

Additional incident actions:

- Stop scheduler and worker services before investigating lost/duplicate work;
  PostgreSQL outboxes remain the recovery authority. Do not delete failed,
  processing, pending, or Gitllery rows.
- Roll back the backend and admin images together so their API contract and
  generated client types stay aligned.
- Retain additive indexes/tables during image rollback. The previous image must
  run against the forward schema.
- Never delete `.gitllery.build-<generation>`, `.gitllery.legacy-v0`, or the
  70k captured intents as a rollback shortcut.
- If activation was separately attempted, return to `shadow`, clear the
  verified-generation assertion, stop Gitllery workers, and preserve both the
  active and legacy directories for diagnosis.

## 24-hour Gitllery gate

All boxes require timestamped evidence from one uninterrupted window:

- [ ] Deep digest, commit count, and change count equal PostgreSQL.
- [ ] Replaying the same range creates no duplicate commit IDs.
- [ ] At least one worker restart resumes from the durable cursor.
- [ ] Restore rehearsal writes only to a temporary database and matches the
      verified summary.
- [ ] Incremental worker RSS increase remains at or below 512 MiB.
- [ ] Foreground API p95 regression remains at or below 10%.
- [ ] Projected full build remains below 24 hours and 2 GiB.
- [ ] New commits after the fixed high-water mark remain represented and
      recoverable throughout the observation.
- [ ] No legacy directory was overwritten or deleted.

If any item fails or evidence is missing, keep `shadow`; do not reinterpret an
incomplete observation as a pass.

## Seven-day soak checklist

Review daily and at the end of the window:

- [ ] No task was lost and no download/import was persisted twice.
- [ ] Durable queues do not grow continuously; failed rows have an explained
      owner and retry path.
- [ ] Redis or worker restarts recover from PostgreSQL without manual row edits.
- [ ] Workbench actionable authentication count reflects only enabled sources
      with recorded failure evidence.
- [ ] Cached workbench, outbox readiness, dedup lookup, and Gitllery status stay
      within their limits without violating the 500 ms foreground guard.
- [ ] Frontend bundle budgets, polling limits, media caching, LCP, and INP have
      no material regression on the managed LAN.
- [ ] Gitllery verification reports no digest/count drift and no duplicate
      commit IDs.
- [ ] No original media, legacy Gitllery repository, or authoritative work row
      was changed by validation or recovery tooling.

The 24-hour Gitllery gate and seven-day soak are duration-based production
evidence. They remain **pending** when this code branch is merged and must not be
reported as complete by automated tests alone.

# Verification Constraint

## Rule

Every code change that touches backend job logic (download, import, sync) or data processing (schemas, detection, classification) MUST be verified end-to-end before committing. Build success alone is insufficient.

## Required verification by change type

### Backend job changes (download.py, import_runner.py, subscription_sync.py)

1. Trigger a real download for a known-good source URL:
   ```bash
   docker compose exec backend python3 -c "
   # Enqueue a test download job to the scheduled queue
   ...
   "
   ```

2. Monitor the pipeline end-to-end:
   ```bash
   docker compose logs -f scheduler worker | grep -E '<job_id>|download|import|ugoira|error'
   ```

3. Verify the output:
   - Check download job status in DB: `SELECT status, error_log FROM download_jobs WHERE id = '...'`
   - Check import job status: `SELECT status, error_log FROM import_jobs WHERE download_job_id = '...'`
   - Check actual files on disk: correct format (GIF vs ZIP), correct directory structure
   - Check DB records: Work, WorkSource, Asset, Tags created correctly

4. Verify failure cases too:
   - Test with a source that has no new content (should show appropriate error message, not "complete" with no error)
   - Test with an invalid URL (should fail at validation, not crash worker)

### API changes (routes, schemas)

1. Call the endpoint with curl or Python:
   ```bash
   docker compose exec backend curl -s http://localhost:8000/api/v1/...
   ```

2. Verify response shape matches the Pydantic schema exactly

3. Check both success and error responses (400, 404, 500)

### Frontend changes

1. Rebuild and verify no compile errors: `cd admin-web && npx next build`

2. For pages that fetch data: verify the API returns the expected shape matching the TypeScript types

3. For visual changes: verify in the browser at `http://localhost:13000`

### Shared image changes (backend code used by worker/scheduler)

1. Verify ALL three containers (backend, worker, scheduler) are restarted

2. Verify each container has the new code:
   ```bash
   docker compose exec worker grep '<key_function>' /app/app/jobs/...
   docker compose exec scheduler grep '<key_function>' /app/app/jobs/...
   ```

## Pre-commit checklist

Before `git commit` for any change that modifies:

- [ ] Code compiles / Docker image builds
- [ ] Related containers restarted with new image
- [ ] New code verified in each container type (worker, scheduler, backend)
- [ ] Happy path tested (successful operation)
- [ ] Failure path tested (appropriate error handling)
- [ ] No regression: existing tests still pass (`docker compose exec backend python -m pytest`)
- [ ] DB state checked if schema or data processing changed

## Anti-patterns

- ❌ "It compiles, ship it" — build success ≠ correctness
- ❌ "I already tested this pattern before" — re-test after changes, gallery-dl behavior varies by version
- ❌ "The error looks unrelated" — always read the full error log before dismissing
- ❌ Restarting only backend when worker/scheduler share the same image
- ❌ Testing only the success path and skipping failure cases

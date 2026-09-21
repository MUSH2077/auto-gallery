# D33 backup archive and PostgreSQL client implementation report

**Status:** CANDIDATE_FOCUSED_GREEN; dependency-aware image build, independent review, and actual PostgreSQL backup/restore validation remain pending

**Base:** `33e9cdf2d03a2a5deb577e2da9bfa17d6bcd6cfa`

**Scope:** backend backup staging, normal and candidate backend Dockerfiles, focused backend regressions, and this report only

## Result

Database backups now create `.pgpass` in a separate `TemporaryDirectory` rather than the archive payload directory. The credential directory exists only while the blocking `pg_dump` process runs and is removed before return-code handling, manifest traversal, or archive creation. Python's context cleanup also runs when `subprocess.run` raises. Existing payload cleanup and candidate-file cleanup remain unchanged, and the restore validator allowlist was not modified.

Both backend image paths install `postgresql-client-16` from the PostgreSQL project's signed `bookworm-pgdg` repository. Builds verify the downloaded repository key against SHA-256 `0144068502a1eddd2a0280ede10ef607d1ec592ce819940991203941564e8e76` and fail unless `pg_dump --version` resolves to major 16. The declared server is PostgreSQL 16 (`postgres:16-alpine@sha256:16bc17c64a573ef34162af9298258d1aec548232985b33ed7b1eac33ba35c229`); Debian bookworm's distribution-default client major 15 is not sufficient. PostgreSQL documents that `pg_dump` refuses a server newer than its own major, and its official Debian instructions provide the versioned PGDG repository packages:

- <https://www.postgresql.org/docs/16/app-pgdump.html>
- <https://www.postgresql.org/download/linux/debian/>

The candidate Dockerfile installs the client independently because the frozen production base image `sha256:bdf855f4f469e0d88b603756ca4e89dfe02daa248dc3a1139b2af4080625c980` predates this runtime dependency. The normal Dockerfile ensures future full base builds include it.

## Root cause and RED evidence

The actual isolated request reached a durable failed TaskRun but stopped before archive creation with `[Errno 2] No such file or directory: 'pg_dump'`. That immutable diagnostic is `/volume2/docker/auto-gallery-button-audit-artifacts/20260908/fullsite-runtime/reports/backup-pgpass-diagnostic-r1-20260912.json`, SHA-256 `31ff20649a5c0ebf845619efd36358ffd64337416f32658b922ded6ae89f603e`. Its preservation check separately passed with SHA-256 `eceabaa4ea997039bc5bd35bf753584b0b7ab6a20a054f8b9b4ca48224a3b926`: no archive or restore session was created, and the failed TaskRun was the only business-table addition.

The second source defect is independently established at `backend/app/api/admin/backup.py`: the prior implementation created `.pgpass` in the payload staging directory and then recursively hashed and archived every regular file from that directory. The unchanged validator correctly rejects `.pgpass` as outside its portable restore allowlist. The new regressions failed before the fix because the observed passfile and `database.dump` had the same parent:

```text
python -m pytest -q tests/test_offline_restore.py
  -k 'database_backup_excludes_pgpass or database_backup_failure_removes_credentials'
2 failed, 45 deselected
AssertionError: passfile.parent == dump.parent
```

Exact executed RED command used the dedicated fixture and `--basetemp=/dev/shm/d33-red-2e97`; evidence is `/volume2/docker/auto-gallery-button-audit-artifacts/20260908/backend-backup-d33-20260912/unit-red.log`, SHA-256 `b3707d23aaaf564c1f325f6304d52d7d177717d9e1b9200feb3d938b42364345`.

The source-level `.pgpass` defect was not reproduced against the old runtime because the missing executable stopped first. The actual post-fix chain remains a required packaging/runtime gate.

## Focused verification

All test commands ran only in dedicated fixture `ag-button-runner` container `2e97df5556a3b57c1e7f36ad481f4672dd6e19343680d091591170899ed279ea`, using PostgreSQL fixture `d819caa53862456f7f61ed9f994292545bca6acb91c8c4b17d6abfff6b2b067c` and a unique `/dev/shm` base temp. No real runtime was changed or queried by this implementation.

Focused new regressions:

```text
docker exec -w /workspace/backend \
  -e DATABASE_URL=postgresql+asyncpg://agbutton:button-test-db@ag-button-a030-postgres:5432/agbutton_test \
  -e TEST_DATABASE_URL=postgresql+asyncpg://agbutton:button-test-db@ag-button-a030-postgres:5432/agbutton_test \
  -e REDIS_URL=redis://ag-button-redis:6379/15 \
  -e MEILI_URL=http://localhost:9 \
  ag-button-runner python -m pytest -q tests/test_offline_restore.py \
  -k 'database_backup_excludes_pgpass or database_backup_failure_removes_credentials' \
  --basetemp=/dev/shm/d33-red-2e97
2 passed, 45 deselected
```

The success regression fakes only the external `pg_dump` process boundary. It asserts the passfile exists with a distinct parent during that process, then reads the generated tar and manifest, proves the credential path/content is absent, uploads those exact archive bytes through the real offline-restore staging API, and reaches a ready payload. The failure regression returns a failed dump result and proves both temporary directories and the attempt-specific pending candidate are gone.

GREEN evidence: `/volume2/docker/auto-gallery-button-audit-artifacts/20260908/backend-backup-d33-20260912/unit-green-focused.log`, SHA-256 `ad330892cb3b50a0b9171e00632133fe48a6b49cf9a30aaade4e3c89b5697019`.

Focused restore/security suite:

```text
docker exec -w /workspace/backend [same isolated environment] ag-button-runner \
  python -m pytest -q tests/test_offline_restore.py \
  tests/test_security_media_backup.py tests/test_private_auth_temp_storage.py \
  --basetemp=/dev/shm/d33-focused-2e97
60 passed in 8.07s
```

Evidence: `/volume2/docker/auto-gallery-button-audit-artifacts/20260908/backend-backup-d33-20260912/pytest-final.log`, SHA-256 `2c25c3f14cd642817a835b1502a6e6523772ac9a1ab94457aab1efa70f1a85b2`.

Static checks:

```text
docker exec -w /workspace/backend ag-button-runner python -m ruff check app/api/admin/backup.py tests/test_offline_restore.py
All checks passed!

docker exec -w /workspace/backend ag-button-runner python -m compileall -q app/api/admin/backup.py tests/test_offline_restore.py
exit 0

docker build --check --pull=false --build-arg BASE_IMAGE=auto-gallery-backend:latest -f backend/Dockerfile.candidate backend
Check complete, no warnings found.

docker build --check --pull=false -f backend/Dockerfile backend
Check complete, no warnings found.
```

Evidence hashes:

| Evidence | SHA-256 |
| --- | --- |
| `ruff-final.log` | `82b3e6a6c090a57601d22943bd23fca9218d1031dbe5a7b754092f9a156b4f18` |
| `compileall-final.log` | `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855` |
| `dockerfile-candidate-final.log` | `c1e9a97c21ce7ebd275fd59006267f7dc1a94fb2841e60414b3e0477a1502574` |
| `dockerfile-normal-final.log` | `8e942d5ef6cf3cd913039a9e6a03e32bdef73ffaa07c40ac997e16da05d49c7a` |
| `pgdg-key-sha256.log` | `9168a93d95d28f75dfdd317189bc975c2f18306c975bda0d8d07d465993d55c4` |

The Docker checks validate syntax and build rules; they do not execute package installation. The dependency-aware candidate build and actual PostgreSQL-backed archive/validator run remain pending.

## Dependency-aware candidate build recipe

The prior network-none container-copy packager cannot add an OS package. BuildKit also does not accept the bare local image ID as a `FROM` argument; bind that exact image to a dedicated local tag, verify the tag, then build with network access:

```text
docker image tag \
  sha256:bdf855f4f469e0d88b603756ca4e89dfe02daa248dc3a1139b2af4080625c980 \
  auto-gallery-backend:d33-base-bdf855f4f469

test "$(docker image inspect auto-gallery-backend:d33-base-bdf855f4f469 --format '{{.Id}}')" = \
  'sha256:bdf855f4f469e0d88b603756ca4e89dfe02daa248dc3a1139b2af4080625c980'

docker build --pull=false --network=default \
  --build-arg BASE_IMAGE=auto-gallery-backend:d33-base-bdf855f4f469 \
  --build-arg CACHEBUST=<D33_COMMIT> \
  --file backend/Dockerfile.candidate \
  --tag auto-gallery-backend:scheduler-actions-<D33_COMMIT_12> \
  backend

docker run --rm --network=none --entrypoint sh \
  auto-gallery-backend:scheduler-actions-<D33_COMMIT_12> \
  -ceu "pg_dump --version | grep -Eq '^pg_dump \\(PostgreSQL\\) 16\\.'"
```

Record the source commit, Dockerfile SHA, base tag and verified base image ID, complete build log, candidate image ID, installed `pg_dump --version`, and PGDG key/source evidence. Root owns the controlled isolated runtime upgrade and the actual database-only backup-to-validator proof. That proof must preserve the old missing-executable failure, use a new TaskRun/archive/upload ID, verify archive and manifest membership without printing credentials, reach validator `ready`, and prove no new backup credential/payload staging residue remains.

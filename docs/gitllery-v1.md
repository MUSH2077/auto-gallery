# Gitllery v1

Gitllery v1 is auto-gallery's portable, per-repository curation history. PostgreSQL
is authoritative; Gitllery is an asynchronous audit replica and disaster-recovery
input. A Gitllery repository cannot accept offline mutations.

The product name and CLI remain **Gitllery v1**. The active disk format is
identified independently:

- `product_version`: `v1`
- `format_id`: `gitllery-segment`
- `format_revision`: `1`

The former Git-shaped `blob/tree/commit/ref` directory is called the **legacy
git-object layout**. It is frozen read-only during migration and is never called
“Gitllery v1” in operator-facing output.

## Installation

The backend image installs `/usr/local/bin/gitllery`. On an administrator's
machine, install the same CLI with:

```bash
pipx install ./backend
```

Configure a remote profile and authenticate without putting a password on the
command line:

```bash
gitllery --profile nas config set url https://gallery.example.test
gitllery --profile nas config set current-profile nas
gitllery --profile nas auth login --username admin
```

The profile file is created with mode `0600`. A token may instead be supplied
with `GITLLERY_TOKEN` or `--token-stdin`; passwords are never stored.

Create an empty portable v1 repository explicitly (this does not create a Git
repository or mutate PostgreSQL):

```bash
gitllery --repo ./creator-library init --repository-id REPOSITORY_UUID
```

## Read-only local commands

From a repository directory (or any child directory):

```bash
gitllery status
gitllery log --limit 20
gitllery show COMMIT_ID
gitllery diff FROM_COMMIT TO_COMMIT
gitllery verify --deep
gitllery export --output history.json
```

Local discovery walks upward for `.gitllery/manifest.json`. These commands do
not modify the repository. Mutation commands always call the authenticated API,
even if `--local` is supplied.

`gitllery push` and `gitllery pull` are reserved remote commands in the v1
language. During this rollout the server returns `gitllery_shadow_only`; the
CLI reports that conflict without attempting a local projection, full-history
walk, or filesystem replacement.

## Domain command language

Small changes can be expressed directly:

```bash
gitllery --remote commit -m "review" work trash WORK_UUID
gitllery --remote commit -m "favorite" work favorite WORK_UUID --set on
gitllery --remote commit -m "tag" work tag-add WORK_UUID --tag landscape
```

For a reviewed batch, use a UTF-8 `.gll` file:

```text
version 1
message "reviewed batch"
reason "manual review"
expect-head 2dc213de-1527-4ace-9b1a-4acaa329e444
work f65b1af3-c004-4b15-a9d3-71e24b4a6454 trash
work 82431af3-c004-4b15-a9d3-71e24b4a6454 favorite on
work 82431af3-c004-4b15-a9d3-71e24b4a6454 tag add "landscape"
```

Then preview and execute the exact command:

```bash
gitllery --remote commit --file changes.gll --dry-run
gitllery --remote commit --file changes.gll \
  --idempotency-key 57b49c37-6e1a-49ec-b74a-127f3eddfb1a
```

The language has no include, loop, variable, arbitrary JSON, or shell escape.
A command may affect at most 25 unique works. The server checks the expected
head and deduplicates `cli:<idempotency-key>` in the same PostgreSQL transaction
as the curation commit and projection intent.

## Rollout and recovery

`GITLLERY_PROJECTION_MODE=shadow` writes the new format to
`.gitllery.build-segment-r1`. Existing `.gitllery` directories are not changed.
After verification and a canary period, an explicit maintenance operation may
rename the legacy directory to `.gitllery.legacy` and promote the build to
`.gitllery`. No automatic deletion is permitted.

Restore is manual and curation-only: verify segments, stage into an isolated
schema, review the summary hash, take a PostgreSQL backup, then explicitly
promote. It never overwrites the public schema directly from an HTTP request.

## Exit codes

| Code | Meaning |
|---:|---|
| 0 | success |
| 2 | invalid command or request |
| 3 | authentication or permission failure |
| 4 | head/idempotency conflict |
| 5 | remote service unavailable |
| 6 | verification found corruption |
| 7 | local repository or commit not found |

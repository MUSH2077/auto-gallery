# Task 7 deployment digest source fix

Date: 2026-09-13

Status: **IMPLEMENTED; INDEPENDENT REVIEW PENDING.** This is a source-level portability correction for the final PR. It does not claim deployment, runtime acceptance, or approval of any hotfix evidence.

## Root cause and change

`scripts/deploy.sh` passed the NUL-delimited Git path stream to `sort -z` under the caller's locale. The retained diagnosis records the same clean source as `9160e1377a20ed191ccffbda18eaaa63a1bc5ac50eb564da008ae24e5dc0ea28` under inherited `en_US.UTF-8` and `55af603f35fe02599c99fa9b0305ded05a519b1878ece24c1009098bc3683e19` under C byte ordering, with the latter matching the frozen accepted candidate. The implementation now runs only the source-path sort as `LC_ALL=C sort -z`, making the deployment digest use deterministic byte ordering without changing the surrounding deployment environment.

`backend/tests/test_portable_deploy_contract.py` now extracts the actual `existing_source_paths` and `source_digest` function bodies from `scripts/deploy.sh`, runs them against a temporary Git repository containing tracked names whose ordering differs by collation, and compares results from the installed C and `en_US` UTF-8 locales. This exercises the shell algorithm and file bytes; it does not assert source text or mirror the production command with a regular expression. Systems without an `en_US` UTF-8 locale report the test as skipped.

## TDD and verification evidence

- Test-runner preflight: `python -m pytest -q backend/tests/test_portable_deploy_contract.py::test_source_digest_is_stable_across_available_collations` did not collect because this host has no `python` command (`exit 127`). All actual tests below used the cached offline `uv` environment; no container, network, database, or production operation was used.
- RED, before the production edit: `uv run --offline --with pytest python -m pytest -q backend/tests/test_portable_deploy_contract.py::test_source_digest_is_stable_across_available_collations` — **1 failed**. The extracted production function returned C digest `17f9bc37016beca3ea8937dd083de2a26ed8ef26e472125b940ab525bdf625b0` and `en_US.utf8` digest `dc0e4bf9144d50e7f6730b7c91a327b6e89597eac0b8fbe02039d0d1029d6b5a` for the same fixture.
- GREEN, after the production edit: the same focused command — **1 passed** in 0.41 seconds, with one pre-existing environment warning that the offline minimal pytest environment does not provide the configured `asyncio_mode` plugin option.
- Focused contract suite: `uv run --offline --with pytest python -m pytest -q backend/tests/test_portable_deploy_contract.py` — **16 passed** in 0.48 seconds, with the same single configuration warning.
- Shell syntax: `bash -n scripts/deploy.sh` — **passed**.
- Focused lint: `uv run --offline --with ruff ruff check backend/tests/test_portable_deploy_contract.py` — **passed** (`All checks passed!`).

## Owned changed files

- `scripts/deploy.sh`
- `backend/tests/test_portable_deploy_contract.py`
- `.superpowers/sdd/2026-09-08-scheduler-actions/task-7-deploy-digest-source-fix.md`

The pre-existing dirty plan ledger `docs/superpowers/plans/2026-09-08-scheduler-actions.md` was preserved and is outside this change. Frozen preview-hotfix-release files, incident artifacts, Docker/runtime state, databases, and production services were not modified.

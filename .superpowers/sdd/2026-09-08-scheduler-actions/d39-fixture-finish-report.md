# D39 frontend fixture finish report

Date: 2026-09-13

Status: the original Re-enrich fixture failure is diagnosed and corrected. The full five-case frontend fixture remains 4/5 under the current 2 GiB test-container envelope because a previously passing restored-notification case ended with a bare timeout during demonstrated cgroup pressure. This report does not claim whole-site or real-API acceptance.

## Root cause and correction

The R2 trace proves the Re-enrich test never exercised the product action. Its click locator waited from trace time 14,533 ms until the 150-second test deadline for exact text `Re-enrich creators`. The captured page exposed `Re-enrich Danbooru mapping` and its `Re-enrich` button; no POST to `/api/v1/admin/creators/re-enrich` occurred. Cleanup then reported the already-closed context. This was a stale fixture locator, not a product failure or a server compilation timeout.

`admin-web/tests/e2e/d39-operation-task-links.spec.ts` now clicks each data-management action through its exact, unique button role. The fixture also routes `/api/v1/ws` through Playwright and closes it locally, so websocket attempts no longer escape to the Next proxy. HTTP API requests retain the existing catch-all interception and 501 fail-closed behavior for unknown routes.

The existing R2 failure is the RED case. In R3, Import passed in 33.5 seconds and Re-enrich passed in 8.5 seconds. The latter issued its accepted-operation transport request, polled the distinct RQ job ID, navigated through the canonical task ID, rendered the canonical task drawer, and left the fixture's unhandled-request list empty.

## R3 fixture result

The Browser plugin was unavailable in this session, so the repository's Playwright fixture ran only in `ag-button-preview-webtools` against the staged source at `/tmp/ag-button-preview-web-20260913/admin-web`. The runner prewarmed all four routes. There was no backend attached, and the Next log contains no failed backend websocket proxy attempt after the new websocket route interception.

Command:

```text
PLAYWRIGHT_BASE_URL=http://127.0.0.1:3000 \
  ./node_modules/.bin/playwright test \
  tests/e2e/d39-operation-task-links.spec.ts \
  --project=chromium --workers=1
```

Result: 4 passed, 1 failed, 0 not run in 3.6 minutes.

- PASS: Import from disk links its accepted transport to the canonical task.
- PASS: Re-enrich creators links its accepted transport to the canonical task.
- PASS: Danbooru mapping refresh exposes a canonical task action while polling its transport.
- FAIL: restored operation learns its canonical task before notification navigation; Playwright reported only `Test timeout of 30000ms exceeded` and retained no trace, screenshot, or error-context file for this failure.
- PASS: retained legacy operation without a task UUID never builds a Jobs task link.

The restored-notification case passed in the earlier reviewed 4/5 R1 evidence. During R3, `ag-button-preview-webtools` used 1.567 GiB of its 2 GiB limit and `/sys/fs/cgroup/memory.events` recorded `max 1099`, with zero OOM and OOM-kill events. Memory fell to 1,031,950,336 bytes after Next and Chromium stopped. This supports container reclaim pressure as the leading explanation for the new isolated timeout, but the missing trace prevents a conclusive locator/product classification. The R3 outcome was preserved without an immediate rerun so the resource envelope can be adjusted in a bounded, host-guarded follow-up.

## Static verification

All requested checks ran in `ag-button-preview-webtools` against the same staged source and exited 0:

- `npm run typecheck`
- `npm run check:api-types` — generated OpenAPI types match `docs/api/openapi.json`
- `npm run check:admin-routes` — route contract passed
- `npm run check:i18n` — 3,012 bilingual keys checked

`git diff --check -- admin-web/tests/e2e/d39-operation-task-links.spec.ts` also passed on the worktree source.

## Source and evidence binding

The changed fixture was copied to the fast staging tree. Its worktree and staged SHA-256 are both `0996be6cd06e100a22f4f25d8b605cec897dcfb6f896baf6c7104416ad599045`.

The versioned 250-file R3 source manifest is `frontend-fast-r3-stage-manifest.json`, SHA-256 `2f26a0f37b0f4695f2093279a6056e5eb262074fc3c7d3d0fd2cb8285281df33`.

Preserved evidence under `/volume2/docker/auto-gallery-button-audit-artifacts/20260908/incident-recovery-20260913`:

- R2 Playwright results checksum list: `frontend-fast-r2-playwright-results-sha256.txt`, SHA-256 `0ec93b8ab1dc3f96a75c6dc4aaa91fd4d7ead1ae175142d784a1504d79c4135b`. The decisive R2 `trace.zip` SHA-256 is `36e2af823b951d9fd99480c584424e1648d1d799b37b61544c93c7c99feeb616`.
- R3 combined result: `frontend-fast-r3-result.json`, SHA-256 `d1892ea86b08f068a8b67afafd35b0902fd73cc46e70aa124be7eb485793ca43`.
- R3 fixture log: `frontend-fast-r3-fixture.log`, SHA-256 `1091cc79e96199114b22b94d15da4f5af8bec3ded94552a714268779c9d71d12`.
- R3 Next log: `frontend-fast-r3-next.log`, SHA-256 `9de6123720c153591cbe7f06cd90eb5eb4b32106853f8fdb782033361a20629e`.
- R3 typecheck log: SHA-256 `cf0c31198780c7e7ceeab1648c164d564de3c7ebc74efed5067e7b7ec6a63b40`.
- R3 API-types log: SHA-256 `f963d1d81e151f349a656375709260173d0611eea5a3bb62a2f7d8e4aa8a4f3f`.
- R3 admin-routes log: SHA-256 `f24273782a2614f846711dc5d819923ce7cef26d158e12f251b1178e79e58971`.
- R3 i18n log: SHA-256 `3a8ef43e722916048afe76c498037de87118b387c0ae81b8e2237fbd48f215c5`.
- R3 cgroup snapshot: `frontend-fast-r3-cgroup-memory.txt`, SHA-256 `40eff568427ba79264fcd81151c48fb56ce0ea2f1c4d194f766c2dc44a533e6b`.
- R3 Playwright result checksum list: `frontend-fast-r3-playwright-results-sha256.txt`, SHA-256 `f4253ef244a7c7e08778ec99a794c7bcf6204dfa7ec4c65617c93da4b6249e21`. The result directory contained only `.last-run.json`; Playwright did not retain diagnostic attachments for the new timeout.
- Versioned runner: `run_fast_frontend_fixture_r3.py`, SHA-256 `adb75b51b685a41ec145012531f45613ee5d34aea84f3da12eb7b9b0adfe2580`.

## Changed files

- `admin-web/tests/e2e/d39-operation-task-links.spec.ts`
- `.superpowers/sdd/2026-09-08-scheduler-actions/d39-fixture-finish-report.md`

No backend source, unrelated plan file, production container, or live API was changed. The dedicated frontend container remains running without a Next, Chromium, or Playwright test process.

## Root R5 bounded follow-up

After the same frozen source's R3 four passing cases, root ran only the unresolved restored-notification case with trace enabled on the same dedicated container. A guarded temporary test-container increase from2GiB to2.5GiB left hostavailable>4GiB before the change; productionIOpressure had returned near1%. R4 setup failed before any change because Docker top requires a PID field; that result remains separate.

R5 command: playwright test tests/e2e/d39-operation-task-links.spec.ts --project=chromium --workers=1 --max-failures=1 --grep 'a restored operation' --trace on --output /tmp/auto-gallery-playwright/r5-restored, through run_fast_frontend_fixture_r5.py. Exit0; total setup+test18.464s. Log SHA256 9a23a69b634345f4696d26d10c8f398c50c8a625c85553f19d90637d8cc20812. Source remains exact0996be6cd… and manifest2f26a0f…; R3 fourpass plus R5 fifthpass cover allfive sourcefixturecases, while the failedcombinedR3 remains failed. This is fixture-level source verification, not actualwhole-siteAPIacceptance. Trace archive frontend-fast-r5-playwright-results.tar retained; no additional application source change.

## Final method-contract correction (R6 and R7)

The final fixture review found that pathname-only handlers could accept the wrong HTTP verb. Every allowed fixture route now checks its actual client contract before fulfilling: authentication, bootstrap/page data, task detail, and operation polls require `GET`; `/api/v1/auth/ws-ticket` and `/api/v1/search/assist` require their actual `POST`; and the three action submissions retain their exact `POST` checks. A pathname with any other verb falls through to the existing `unhandled` record and 501 response. The client definitions at `admin-web/src/lib/auth.tsx:54`, `admin-web/src/lib/useWebSocket.ts:46-47`, and `admin-web/src/lib/api/index.ts:544-546` were inspected rather than inferring all read-like operations use GET.

R6 was preserved as useful RED evidence. Its initial method-specific fixture used GET for search assist; all four task-link cases completed their intended UI navigation and then failed the fail-closed assertion because the actual client sent `POST /api/v1/search/assist`. The legacy no-link case passed. The exact command was:

```text
PLAYWRIGHT_BASE_URL=http://127.0.0.1:3000 \
  ./node_modules/.bin/playwright test \
  tests/e2e/d39-operation-task-links.spec.ts \
  --project=chromium --workers=1 \
  --trace on --output /tmp/auto-gallery-playwright/r6-all
```

R6 result: 1 passed, 4 failed in 24.3 seconds; runner duration 30.647 seconds. The fixture log SHA-256 is `33c239a30e2aef006d0ca8460e90797e6772d876648ef34b9067a4a4fa5166e8`, result JSON SHA-256 is `8a2cd65cb294feb7a8dfce234866ed1f61e6c65a1ee1fa69257f878a08a81045`, and retained trace archive SHA-256 is `defe67b77af1d3920971efae0f3b097d440a2e2493a7f0580bc9babb69405558`. The R6 preflight recorded 5,158,768 KiB host MemAvailable, IO PSI avg10 `some=1.04` and `full=0.58`, and passed the 3.5 GiB/15% guard. Its source fixture SHA-256 was `37e7ad68f560cf65b32111032c39fb398536fa4d5e95f8a5c4f8738db1842b3f`.

After correcting search assist to its actual POST contract, the final worktree fixture was copied to `/tmp/ag-button-preview-web-20260913`. Worktree and staged SHA-256 are both `37ee40c1c04b78c9d07830013023f1f16583677f0d11585dee0fe16db249492e`. The 251-file final staging manifest is `frontend-fast-r7-stage-manifest.json`, SHA-256 `b3e9f12a8d199a59facc7b715844d239cfac1ebb3761c41e6f29971bf32973d3`; the independently versioned post-R6 manifest has the same SHA-256.

R7 used the same command with the fresh output path `/tmp/auto-gallery-playwright/r7-all`. Result: all 5 passed in 21.7 seconds; runner duration 28.515 seconds and exit code 0.

- PASS: Import from disk links its accepted transport to the canonical task.
- PASS: Re-enrich creators links its accepted transport to the canonical task.
- PASS: Danbooru mapping refresh exposes a canonical task action while polling its transport.
- PASS: restored operation learns its canonical task before notification navigation.
- PASS: retained legacy operation without a task UUID never builds a Jobs task link.

R7 evidence under `/volume2/docker/auto-gallery-button-audit-artifacts/20260908/incident-recovery-20260913`:

- Result JSON `frontend-fast-r7-result.json`: SHA-256 `acd29a5619f557ccf5fb3b5fe64ffdd7198632d01e6d9aedcc39207531b96a1d`.
- Fixture log `frontend-fast-r7-fixture.log`: SHA-256 `afbee2767b0af4f8d7a9b114b4bf7aefd83bc9d039302e89ae81a84ea9be4608`.
- Trace archive `frontend-fast-r7-playwright-results.tar`, containing a trace for all five cases: SHA-256 `5fda3cab4302a42ce2299d11707705a404e9c913afd61d5e9185389ea8af71f8`.
- Next log `frontend-fast-r7-next.log`: SHA-256 `39d3c1b57d34bf35152615283ba8ea9d3bd12142b08345bc46d4e827f7ccb57b`.
- Warm log `frontend-fast-r7-warm.log`: SHA-256 `bea9e6c7151024088acb65b11df7f02527bb75335c2d62770e77607d7e2fc574`.
- Resource preflight `frontend-fast-r7-resource-preflight.json`: SHA-256 `edcdde49214305c64e925bfc82ecd854adb2934606f5bbc5cefe2e209acbeb10`; it recorded 5,102,924 KiB host MemAvailable and IO PSI avg10 `some=1.04`, `full=0.41`.
- R6 runner `run_fast_frontend_fixture_r6.py`: SHA-256 `152fab5d7963a40b847c1a668ed3130d0d4962f0ba999459f644694e4e606257`.
- R7 runner `run_fast_frontend_fixture_r7.py`: SHA-256 `e50cab5f8dbf662e2897578791a5a06db090c6d19a6fd677f1df495386e0883a`.

No static suite was repeated because only the Playwright fixture changed and no types were affected. `git diff --check -- admin-web/tests/e2e/d39-operation-task-links.spec.ts` passed. The dedicated `ag-button-preview-webtools` container remained at 2.5 GiB and 2 CPUs; after R7 it contained only its `sleep infinity` keeper, with no Next, Playwright, Chromium, or child-agent process. No commit was created.

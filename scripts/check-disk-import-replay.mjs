#!/usr/bin/env node
// Run against a prepared test library. The global mode uses the actual data
// management page; --source adds an API control for a single provider.
// The test account must have subscription memberships for the fixture sources.
// AG_IMPORT_TEST_PASSWORD=... node scripts/check-disk-import-replay.mjs \
//   --url http://127.0.0.1:23001 --expected-works 0 --expected-existing 775 --expected-skipped 5
import assert from "node:assert/strict";
import { parseArgs } from "node:util";
import { chromium } from "../admin-web/node_modules/playwright/index.mjs";

const { values } = parseArgs({ options: {
  url: { type: "string" }, username: { type: "string", default: "admin" },
  source: { type: "string" }, timeout: { type: "string", default: "600" },
  "expected-works": { type: "string" }, "expected-existing": { type: "string" },
  "expected-skipped": { type: "string" }, "expected-status": { type: "string", default: "complete" }, screenshot: { type: "string" },
} });
assert(values.url && process.env.AG_IMPORT_TEST_PASSWORD, "Set --url and AG_IMPORT_TEST_PASSWORD for the test account");
const base = new URL(values.url).origin;
const deadline = Date.now() + Number(values.timeout) * 1000;
const browser = await chromium.launch({ headless: true });
const context = await browser.newContext();
let page;
const get = async path => {
  const response = await context.request.get(base + path);
  assert.equal(response.status(), 200, "GET " + path);
  return response.json();
};
async function list(path) {
  const items = [];
  for (let offset = 0; ; offset += 100) {
    const data = await get(path + (path.includes("?") ? "&" : "?") + "offset=" + offset + "&limit=100");
    items.push(...data.items);
    if (items.length >= data.total || data.items.length === 0) return items;
  }
}
async function inventory() {
  const works = await list("/api/v1/works");
  const assets = new Set();
  // Bound library inspection to four requests, regardless of its size.
  let next = 0;
  await Promise.all(Array.from({ length: 4 }, async () => {
    while (next < works.length) {
      const work = works[next++];
      for (const asset of await get("/api/v1/works/" + work.id + "/assets")) assets.add(asset.id);
    }
  }));
  return { works: works.map(work => work.id).sort(), assets: [...assets].sort() };
}
const settled = new Set(["complete", "failed", "cancelled", "stale"]);
async function until(check, label) {
  while (Date.now() < deadline) {
    const value = await check();
    if (value) return value;
    await new Promise(resolve => setTimeout(resolve, 1000));
  }
  throw Error("Timed out waiting for " + label);
}
try {
  const login = await context.request.post(base + "/api/v1/auth/browser/login", {
    headers: { origin: base }, data: { username: values.username, password: process.env.AG_IMPORT_TEST_PASSWORD },
  });
  assert.equal(login.status(), 200, "Test-account login failed");
  assert.equal((await login.json()).must_change_password, false, "Prepare the test account password before running");
  const before = await inventory();
  let receipt, request;
  if (values.source) {
    const token = (await context.cookies()).find(cookie => cookie.name === "ag_csrf")?.value;
    request = { reset_ledger: true, source: values.source };
    const response = await context.request.post(base + "/api/v1/admin/library/import-from-disk", {
      headers: { origin: base, "x-csrf-token": token }, data: request,
    });
    assert.equal(response.status(), 202);
    receipt = await response.json();
  } else {
    page = await context.newPage();
    await page.goto(base + "/admin/data-mgmt");
    await page.getByRole("checkbox", { name: /重置台账|Reset ledger/ }).check();
    const response = page.waitForResponse(reply =>
      new URL(reply.url()).pathname === "/api/v1/admin/library/import-from-disk" && reply.request().method() === "POST");
    await page.getByRole("button", { name: /^(从盘导入|Import)$/ }).click();
    const accepted = await response;
    assert.equal(accepted.status(), 202);
    request = accepted.request().postDataJSON();
    assert.equal(request.reset_ledger, true);
    assert(!request.source && !request.repository_id, "Global replay must not filter the input");
    receipt = await accepted.json();
  }
  const publisher = await until(async () => {
    const operation = await get("/api/v1/admin/operations/" + receipt.task_id);
    return settled.has(operation.status) ? operation : null;
  }, "scan and publication");
  assert.equal(publisher.status, "complete", JSON.stringify(publisher.error));
  const publication = publisher.result?.stats || publisher.result;
  assert(publication.import_job_ids?.length, "The prepared library must publish imports");
  const firstIds = new Set(publication.import_job_ids);
  const firstTasks = (await list("/api/v1/tasks?kind=import&include_account=true"))
    .filter(task => firstIds.has(task.subject_id));
  assert.equal(firstTasks.length, firstIds.size, "Every published import needs a durable task");
  const parentIds = [...new Set(firstTasks.map(task => task.meta.download_job_id))];
  const parents = await until(async () => {
    const rows = await Promise.all(parentIds.map(id => get("/api/v1/download-jobs/" + id)));
    return rows.every(row => settled.has(row.status) && !row.manifest?.bounded_import_publication_open) ? rows : null;
  }, "all download parents and bounded imports");
  assert(parents.every(parent => ["complete", "failed"].includes(parent.status)), "Unexpected cancelled or stale parent");
  const resultStatus = parents.some(parent => parent.status === "failed") ? "failed" : "complete";
  assert.equal(resultStatus, values["expected-status"], JSON.stringify(parents.map(p => ({ id: p.id, status: p.status, error: p.error_log }))));
  const children = (await list("/api/v1/tasks?kind=import&include_account=true"))
    .filter(task => parentIds.includes(task.meta?.download_job_id));
  assert(children.length >= firstIds.size);
  for (const child of children) {
    assert(["complete", "failed"].includes(child.status), child.error_log || child.id);
    const { stats, total_groups: total } = child.result_data;
    assert.equal(child.progress_current, stats.works + stats.existing + stats.skipped, child.id);
    assert.equal(child.progress_total, total, child.id);
    assert.equal(child.progress_current, total, child.id);
    assert.equal(Boolean(child.error_log), child.status === "failed", child.id);
  }
  const stats = Object.fromEntries(["works", "assets", "existing", "skipped"].map(key =>
    [key, parents.reduce((sum, parent) => sum + parent.manifest.import_stats[key], 0)]));
  const childStats = Object.fromEntries(Object.keys(stats).map(key =>
    [key, children.reduce((sum, child) => sum + child.result_data.stats[key], 0)]));
  assert.deepEqual(childStats, stats, "Parent and child statistics must agree");
  for (const key of ["works", "existing", "skipped"]) {
    if (values["expected-" + key] !== undefined) assert.equal(stats[key], Number(values["expected-" + key]), key);
  }
  const after = await inventory();
  if (values["expected-works"] === "0") assert.deepEqual(after, before, "Replay changed work or asset identities");
  if (page) {
    const visible = children.find(child => child.result_data.stats.skipped > 0) || children[0];
    await page.goto(base + "/admin/jobs?task=" + visible.id);
    const message = page.getByText(visible.result_data.message, { exact: false }).first();
    await message.waitFor();
    await message.scrollIntoViewIfNeeded();
    if (resultStatus === "complete") assert.equal(await page.getByText(/All parsed works failed to import/).count(), 0);
    if (values.screenshot) await page.screenshot({ path: values.screenshot, fullPage: true });
  }
  process.stdout.write(JSON.stringify({
    mode: values.source ? "source-filtered API" : "global webpage",
    request, result_status: resultStatus, publisher_task_id: receipt.task_id, publication_count: publication.import_job_ids.length,
    child_count: children.length, parent_count: parents.length, stats,
    parents: parents.map(parent => ({ id: parent.id, status: parent.status, stats: parent.manifest.import_stats })),
    children: children.map(child => ({ id: child.subject_id, status: child.status,
      stats: child.result_data.stats, current: child.progress_current, total: child.progress_total,
      message: child.result_data.message })),
    before: { works: before.works.length, assets: before.assets.length },
    after: { works: after.works.length, assets: after.assets.length },
    identities_unchanged: JSON.stringify(before) === JSON.stringify(after),
  }, null, 2) + "\n");
} finally {
  await context.close();
  await browser.close();
}

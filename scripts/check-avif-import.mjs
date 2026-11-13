#!/usr/bin/env node
// Verify a prepared test library after AVIF download/import. The account needs
// access to its fixture subscriptions. Passwords are read from the environment.
// AG_IMPORT_TEST_PASSWORD=... node scripts/check-avif-import.mjs \
//   --url http://127.0.0.1:23011 --expected-avif 4 --source-work-ids 10802592,10802532,10802502,10802466
import assert from "node:assert/strict";
import { parseArgs } from "node:util";
import { chromium } from "../admin-web/node_modules/playwright/index.mjs";

const { values } = parseArgs({ options: {
  url: { type: "string" }, username: { type: "string", default: "admin" },
  "expected-avif": { type: "string", default: "4" }, "source-work-ids": { type: "string" },
  timeout: { type: "string", default: "300" }, screenshot: { type: "string" },
} });
assert(values.url && process.env.AG_IMPORT_TEST_PASSWORD, "Set --url and AG_IMPORT_TEST_PASSWORD");
const base = new URL(values.url).origin;
const expectedIds = new Set((values["source-work-ids"] || "").split(",").filter(Boolean));
const deadline = Date.now() + Number(values.timeout) * 1000;
const browser = await chromium.launch({ headless: true });
const context = await browser.newContext();
const get = async path => {
  const reply = await context.request.get(base + path);
  assert.equal(reply.status(), 200, path);
  return reply.json();
};
async function candidates() {
  const works = [];
  for (let offset = 0; ; offset += 100) {
    const data = await get("/api/v1/works?limit=100&offset=" + offset);
    works.push(...data.items);
    if (works.length >= data.total || !data.items.length) break;
  }
  const found = [];
  for (const work of works) {
    const sources = await get("/api/v1/works/" + work.id + "/sources");
    const ids = sources.map(source => source.source_work_id);
    if (expectedIds.size && !ids.some(id => expectedIds.has(id))) continue;
    const assets = await get("/api/v1/works/" + work.id + "/assets");
    for (const asset of assets.filter(asset => asset.mime_type === "image/avif")) {
      found.push({ work_id: work.id, source_work_ids: ids, ...asset });
    }
  }
  return found;
}
try {
  const login = await context.request.post(base + "/api/v1/auth/browser/login", {
    headers: { origin: base },
    data: { username: values.username, password: process.env.AG_IMPORT_TEST_PASSWORD },
  });
  assert.equal(login.status(), 200);
  assert.equal((await login.json()).must_change_password, false, "Prepare the test account password first");
  let assets;
  while (Date.now() < deadline) {
    assets = await candidates();
    if (assets.length === Number(values["expected-avif"]) && assets.every(asset => asset.derivative_status === "ready")) break;
    assert(!assets.some(asset => asset.derivative_status === "failed"), "AVIF derivative failed");
    await new Promise(resolve => setTimeout(resolve, 1000));
  }
  assert.equal(assets.length, Number(values["expected-avif"]), "Missing or extra AVIF assets");
  const actualIds = new Set(assets.flatMap(asset => asset.source_work_ids));
  for (const id of expectedIds) assert(actualIds.has(id), "Missing source work " + id);
  const page = await context.newPage();
  await page.goto(base + "/admin/works");
  const first = assets[0];
  const card = page.locator('img[src*="' + first.id + '"]').first();
  await card.waitFor();
  await card.scrollIntoViewIfNeeded();
  await page.waitForFunction(id => [...document.images].some(image =>
    image.src.includes(id) && image.complete && image.naturalWidth > 0), first.id);
  const result = [];
  for (const asset of assets) {
    assert.equal(asset.media_kind, "image");
    assert.equal(asset.derivative_status, "ready");
    assert(asset.width > 0 && asset.height > 0);
    assert(asset.thumb_url && asset.original_url);
    const thumb = await context.request.get(new URL(asset.thumb_url, base).href);
    assert.equal(thumb.status(), 200);
    assert(thumb.headers()["content-type"].startsWith("image/webp"));
    assert((await thumb.body()).length > 0);
    const original = await context.request.get(new URL(asset.original_url, base).href);
    assert.equal(original.status(), 200);
    assert(original.headers()["content-type"].startsWith("image/avif"));
    assert.equal((await original.body()).length, asset.file_size);
    await page.goto(base + "/admin/works/" + asset.work_id);
    // Select the file in the filmstrip before opening its full image. A
    // multi-page work initially renders only one page in the main viewer.
    await page.getByTitle(asset.file_name, { exact: true }).click();
    await page.getByTitle(/^(查看全图|View full)$/).click();
    const image = page.getByRole("dialog").getByRole("img", { name: asset.file_name, exact: true });
    await image.waitFor();
    await page.waitForFunction(id => [...document.querySelectorAll('[role="dialog"] img')].some(image =>
      image.src.includes(id) && image.complete && image.naturalWidth > 0), asset.id);
    const dimensions = await image.evaluate(node => ({ width: node.naturalWidth, height: node.naturalHeight }));
    result.push({ work_id: asset.work_id, asset_id: asset.id, source_work_ids: asset.source_work_ids,
      file_name: asset.file_name, mime_type: asset.mime_type, derivative_status: asset.derivative_status,
      stored_dimensions: [asset.width, asset.height], browser_dimensions: dimensions });
    if (values.screenshot && asset.id === first.id) await page.screenshot({ path: values.screenshot, fullPage: true });
  }
  process.stdout.write(JSON.stringify({ avif_count: result.length, list_thumbnail_loaded: true, assets: result }, null, 2) + "\n");
} finally {
  await context.close();
  await browser.close();
}

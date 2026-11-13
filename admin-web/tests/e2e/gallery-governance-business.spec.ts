import { expect, test, type BrowserContext, type Route } from "@playwright/test";

const origin = process.env.PLAYWRIGHT_BASE_URL || "http://127.0.0.1:13000";
const json = (route: Route, body: unknown, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
const user = { id: 7, username: "governance-ui", display_name: "Governance UI", is_admin: true, is_active: true, permissions: ["system", "curation", "tasks", "library"], modules: { system: true, curation: true }, preferences: { lang: "en", theme: "light" }, must_change_password: false };

async function install(context: BrowserContext, extra?: (route: Route, path: string) => boolean | Promise<boolean>) {
  await context.addCookies([{ name: "ag_session", value: "governance-ui", url: origin }, { name: "ag_csrf", value: "governance-csrf", url: origin }]);
  await context.addInitScript(() => { localStorage.setItem("auto-gallery-lang", "en"); localStorage.setItem("auto-gallery-theme", "light"); });
  await context.route("**/api/v1/**", async route => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/api/v1/auth/me") return void await json(route, user);
    if (path === "/api/v1/notifications") return void await json(route, { items: [], total: 0 });
    if (path === "/api/v1/auth/ws-ticket") return void await json(route, { detail: "fixture websocket unavailable" }, 503);
    if (extra && await extra(route, path)) return;
    if (path === "/api/v1/admin/system-info") return void await json(route, { archives_kb: {}, db_stats: { works: 12, assets: 22, creators: 3, subscriptions: 2, tags: 30 }, downloads_free_gb: 1, downloads_size_mb: 10, downloads_total_gb: 2, downloads_used_gb: 1, inventory_source: "storage_artifacts", library_free_gb: 1, library_size_mb: 22, library_total_gb: 2, library_used_gb: 1, pipeline_stats: { failed_artifacts: 0, orphan_pending_artifacts: 0, pending_import_works: 0 }, python: "3.12", version: "test" });
    if (path === "/api/v1/admin/storage-breakdown") return void await json(route, { creator_tree: [], creators: [], db_stats: {}, inventory_source: "storage_artifacts", inventory_updated_at: null, layers: {}, pipeline_stats: { failed_artifacts: 0, orphan_pending_artifacts: 0, pending_import_works: 0 } });
    if (path === "/api/v1/curation/commits") return void await json(route, { items: [{ id: "commit-1", message: "Fixture commit", trigger: "work_trash", occurred_at: "2026-10-24T08:00:00Z", status: "active", is_baseline: false, changes: [] }], total: 1, offset: 0, limit: 50 });
    if (path === "/api/v1/search") return void await json(route, { groups: { works: { total: 0, items: [] } }, total: 0, parsed: { tokens: [] } });
    if (path === "/api/v1/curation/backfill/status") return void await json(route, { is_complete: false, expected: {}, existing: {}, missing: {} });
    if (path === "/api/v1/curation/rule-suggestions") return void await json(route, []);
    if (path === "/api/v1/curation/purge/preview") return void await json(route, { work_count: 0, bytes_reclaimable: 0, works: [] });
    if (path === "/api/v1/admin/dedup/cases") return void await json(route, { items: [{ id: "case-1", status: "pending", revision: 2, left: { id: "asset-a", file_name: "a.png", size_bytes: 10 }, right: { id: "asset-b", file_name: "b.png", size_bytes: 10 }, evidence: { visual_score: 90, metadata_score: 5, total_score: 95, phash_distance: 1, ssim_score: 0.99, hard_gate_passed: true, facts: { metadata: {}, scope: {}, thresholds: {} } } }], total: 1, offset: 0, limit: 25 });
    if (path === "/api/v1/admin/dedup/scans/latest") return void await json(route, { status: "idle", snapshot: null });
    return void await json(route, {});
  });
}

test.describe.configure({ timeout: 60_000 });
test.use({actionTimeout:10000});

test("curation filters use URL state and revert requires confirmation with retryable failure", async ({ page, context }) => {
  let reverted = 0;
  await install(context, async (route, path) => {
    if (path === "/api/v1/curation/commits/commit-1/revert") {
      reverted += 1;
      await json(route, { detail: "revert temporarily unavailable" }, 503); return true;
    }
    return false;
  });
  await page.goto("/admin/data-mgmt/curation");
  await expect(page.getByRole("heading", { name: /Curation/i, level: 1 })).toBeVisible();
  await page.getByRole("combobox", { name: /Filtered by/i }).click();
  await page.getByRole("option", { name: /Trash/i }).click();
  await expect(page).toHaveURL(/trigger=work_trash/);
  await page.getByRole("button", { name: /Revert/i }).click();
  const dialog = page.getByRole("dialog");
  await expect(dialog).toBeVisible();
  await dialog.getByRole("button", { name: "Confirm", exact: true }).click();
  await expect.poll(() => reverted).toBe(1);
  await expect(dialog.getByRole("alert")).toContainText("revert temporarily unavailable");
});

test("dedup scan posts its payload and decisions retain revision and idempotency", async ({ page, context }) => {
  let scanBody: unknown = null;
  let decideBody: unknown = null;
  await install(context, async (route, path) => {
    if (path === "/api/v1/admin/dedup/scans") { scanBody = route.request().postDataJSON(); await json(route, { task_id: "task-scan", scan_id: "scan-1" }, 202); return true; }
    if (path === "/api/v1/admin/dedup/cases/case-1/decisions") { decideBody = route.request().postDataJSON(); await json(route, { status: "deferred" }); return true; }
    return false;
  });
  await page.goto("/admin/data-mgmt/dedup");
  await page.getByRole("button", { name: /Scan/i }).click();
  await expect.poll(() => scanBody).toEqual({ auto_apply: true, batch_size: 100 });
  await page.getByRole("button", { name: /Review later/i }).click();
  await expect.poll(() => decideBody).toMatchObject({ expected_revision: 2, action: "defer" });
  expect((decideBody as { idempotency_key: string }).idempotency_key).toMatch(/^[0-9a-f-]{36}$/);
});

test("data management integrity accepts a task and clear confirmation posts the exact entity phrase", async ({ page, context }) => {
  let integrityBody: unknown = null;
  let integrityRequests = 0;
  let clearBody: unknown = null;
  await install(context, async (route, path) => {
    if (path === "/api/v1/admin/integrity-check") { integrityRequests += 1; integrityBody = route.request().postDataJSON(); await json(route, { task_id: "task-integrity", operation_type: "integrity-check" }, 202); return true; }
    if (path === "/api/v1/admin/clear/preview/all") { await json(route, { entity: "all", counts: { works: 4, assets: 4 }, confirmation_phrase: "DELETE ALL DATA", preserves_repository_sync_receipts: true, deletes_media_files: true }); return true; }
    if (path === "/api/v1/admin/operations/clear") { clearBody = route.request().postDataJSON(); await json(route, { task_id: "task-clear" }, 202); return true; }
    return false;
  });
  await page.goto("/admin/data-mgmt");
  await page.getByRole("button", { name: /Run Check|运行检查/i }).click();
  await expect.poll(() => integrityRequests).toBe(1);
  await expect(page.getByRole("button", { name: /Run Check|运行检查/i }).first()).toBeVisible();
  await page.getByRole("button", { name: "Delete All Data", exact: true }).click();
  const dialog = page.getByRole("dialog");
  await dialog.getByRole("textbox").fill("wrong confirmation");
  await expect(dialog.getByRole("button", {name:"Confirm",exact:true})).toBeDisabled();
  await dialog.getByRole("textbox").fill("DELETE ALL DATA");
  await dialog.getByRole("button", { name: "Confirm", exact: true }).click();
  await expect.poll(() => clearBody).toEqual({ entity: "all", confirmation: "DELETE ALL DATA" });
});

test("integrity snapshots preserve heterogeneous items and empty results",async({page,context})=>{
 let empty=false;
 const result={checked_at:"2026-10-26T00:00:00Z",db_stats:{works:12},issues:[
  {type:"orphaned_download_files",severity:"warning",count:4,description:"Typed integrity fixture",items:[
   {id:"file-one",path:"/library/example.png"},{id:"file-two",file_name:"archive.zip",file_count:3},
   {id:"name-one",name:"Example collection"},{id:"asset-one",asset_id:"asset-one",source:"pixiv",source_work_id:"1200"}]}]};
 await install(context,async(route,path)=>{
  if(path==="/api/v1/admin/integrity-check/latest"){
   await json(route,{current:null,snapshot:{task_id:"integrity-fixture",operation_type:"admin-integrity-scan",status:"completed",result:empty?{...result,issues:[]}:result}});return true;
  }return false;
 });
 await page.goto("/admin/data-mgmt");
 await page.getByRole("button",{name:/View.*4|查看.*4/i}).click();
 const dialog=page.getByRole("dialog");await expect(dialog).toContainText("/library/example.png");
 await expect(dialog).toContainText("archive.zip");await expect(dialog).toContainText("Example collection");await expect(dialog).toContainText("pixiv/1200");
 await page.keyboard.press("Escape");await expect(dialog).toHaveCount(0);
 empty=true;await page.reload();
 await expect(page.getByRole("button",{name:/View.*4|查看.*4/i})).toHaveCount(0);
 await expect(page.getByRole("heading",{level:1})).toBeVisible();
});

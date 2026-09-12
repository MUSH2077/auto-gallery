import { expect, test, type BrowserContext, type Route } from "@playwright/test";

test.describe.configure({ timeout: 90_000 });

type Principal = { id: number; username: string; display_name: string; is_admin: boolean; is_active: boolean; permissions: string[]; modules: Record<string, boolean>; preferences: Record<string, never>; nsfw_visible: boolean; upload_used_bytes: number; must_change_password: boolean };
const principal = (permissions: string[], isAdmin = false): Principal => ({
  id: isAdmin ? 1 : permissions.length + 2,
  username: isAdmin ? "admin" : permissions.join("-") || "denied",
  display_name: isAdmin ? "Admin" : "Operator",
  is_admin: isAdmin,
  is_active: true,
  permissions,
  modules: Object.fromEntries(permissions.map((permission) => [permission, true])),
  preferences: {},
  nsfw_visible: true,
  upload_used_bytes: 0,
  must_change_password: false,
});
const workbench = { updated_at: "2026-09-12T00:00:00Z", queue: { default: 0, scheduled: 0, failed: 0, active_download_count: 0, active_import_count: 0, failed_download_count: 1, failed_import_count: 0, stale_download_count: 0, stale_import_count: 0, stale_count: 0 }, scheduler: { enabled: true, mode: "interval", timezone: "UTC", scan_interval_minutes: 60 }, storage: { disk_total_bytes: 1, disk_free_bytes: 1, disk_used_bytes: 0, risk_level: "ok" }, health: {}, attention: { auth_unhealthy_count: 0, failed_download_count: 1, failed_import_count: 0, stale_job_count: 0, low_disk_warning: false, scheduler_disabled_warning: false }, recent: { download_jobs: [], import_jobs: [], works: [], successful_syncs: [] } };
const json = (route: Route, body: unknown, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });

async function installJobsFixture(context: BrowserContext, me: Principal, handler: (route: Route, path: string) => Promise<boolean>) {
  const unhandled: string[] = [];
  await context.addCookies([{ name: "ag_token", value: "fixture", domain: "127.0.0.1", path: "/" }]);
  await context.addInitScript(() => { localStorage.setItem("ag_token", "fixture"); localStorage.setItem("auto-gallery-lang", "en"); });
  await context.route("**/api/v1/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (await handler(route, path)) return;
    if (path === "/api/v1/auth/me") return json(route, me);
    if (path === "/api/v1/auth/ws-ticket") return json(route, { detail: "fixture websocket unavailable" }, 503);
    if (path === "/api/v1/system/workbench") return json(route, workbench);
    if (path === "/api/v1/tasks") return json(route, { total: 0, items: [] });
    if (path === "/api/v1/download-jobs") return json(route, []);
    if (path === "/api/v1/import-jobs") return json(route, { total: 0, items: [] });
    if (path === "/api/v1/search/assist") return json(route, { query: "", canonical_query: "", parsed: { tokens: [] }, diagnostics: [], suggestions: [] });
    if (path === "/api/v1/system/scheduler-decisions") return json(route, { total: 0, items: [], summary: { blocked_count: 0 } });
    if (path === "/api/v1/operations/overview") return json(route, { summary: { attention: 0 }, items: [] });
    unhandled.push(`${route.request().method()} ${path}`);
    return json(route, { detail: `Unhandled fixture request: ${path}` }, 501);
  });
  return unhandled;
}

test("transport-uncertain jobs batch keeps the missing filtered row selected with its reason", async ({ context, page }) => {
  const id = "44444444-0000-4000-8000-000000000004";
  let committed = false;
  const task = { id, task_type: "admin", operation_type: "admin-integrity-check", title: "Uncertain retry", status: "failed", created_at: "2026-09-12T00:00:00Z", updated_at: "2026-09-12T00:00:00Z", available_actions: ["retry"], disabled_reasons: {} };
  const unhandled = await installJobsFixture(context, principal(["tasks"]), async (route, path) => {
    if (path === "/api/v1/tasks") { await json(route, { total: committed ? 0 : 1, items: committed ? [] : [task] }); return true; }
    if (path === "/api/v1/search/assist") { await json(route, { query: "status:failed", canonical_query: "status:failed", parsed: { tokens: [{ kind: "qualifier", key: "status", value: "failed", negated: false }] }, diagnostics: [], suggestions: [] }); return true; }
    if (path === `/api/v1/tasks/${id}/retry`) { committed = true; await route.abort("connectionreset"); return true; }
    return false;
  });
  page.on("dialog", (dialog) => void dialog.accept());
  await page.goto("/admin/jobs?tab=admin&q=status%3Afailed");
  await page.getByRole("button", { name: "Batch actions" }).click();
  await page.getByRole("checkbox", { name: /44444444/ }).check();
  await page.getByRole("combobox", { name: "Batch action" }).selectOption("retry");
  await page.getByRole("button", { name: "Apply" }).click();
  await expect.poll(() => committed).toBe(true);
  await expect(page.getByRole("checkbox", { name: /44444444/ })).toHaveCount(0);
  await expect(page.getByText("1 selected", { exact: true })).toBeVisible();
  await expect(page.getByRole("alert").filter({ hasText: "Some tasks were not completed" })).toContainText(/Failed to fetch|Network error/);
  await page.getByRole("tab", { name: "Downloads" }).click();
  await expect(page.getByText("1 selected", { exact: true })).toHaveCount(0);
  await expect(page.getByRole("alert").filter({ hasText: "Some tasks were not completed" })).toHaveCount(0);
  expect(unhandled).toEqual([]);
});

test("clear and retry-all render committed typed counts, retain failures, cancel, and suppress pending duplicates", async ({ context, page }) => {
  let clearCalls = 0; let retryCalls = 0; let releaseClear!: () => void;
  const heldClear = new Promise<void>((resolve) => { releaseClear = resolve; });
  const failedId = "55555555-0000-4000-8000-000000000005";
  const unhandled = await installJobsFixture(context, principal(["tasks"]), async (route, path) => {
    if (path === "/api/v1/download-jobs/clear") {
      clearCalls += 1; await heldClear;
      await json(route, { status: "ok", action: "delete", task_type: "download", filters: { statuses: ["complete"] }, total_matched: 3, succeeded: 2, failed: 1, deleted: 2, errors: [{ id: failedId, error: { reason: "execution_unsettled" } }] });
      return true;
    }
    if (path === "/api/v1/download-jobs/retry-all") {
      retryCalls += 1;
      if (retryCalls === 1) await json(route, { status: "ok", action: "retry", task_type: "download", filters: { statuses: ["failed", "stale"] }, total_matched: 4, succeeded: 4, failed: 0, errors: [] });
      else await json(route, { detail: "retry service unavailable" }, 503);
      return true;
    }
    return false;
  });
  await page.goto("/admin/jobs?tab=downloads");
  await page.getByText("Batch and queue utilities", { exact: true }).click();
  page.once("dialog", (dialog) => dialog.dismiss());
  await page.getByRole("button", { name: "Clear Complete" }).click();
  expect(clearCalls).toBe(0);
  page.once("dialog", (dialog) => dialog.accept());
  await page.getByRole("button", { name: "Clear Complete" }).click();
  await expect.poll(() => clearCalls).toBe(1);
  await expect(page.getByRole("button", { name: "Clear Complete" })).toBeDisabled();
  await page.getByRole("button", { name: "Clear Complete" }).click({ force: true });
  expect(clearCalls).toBe(1);
  releaseClear();
  await expect(page.getByRole("status", { name: "Queue utility result" })).toContainText("Matched 3; deleted 2; failed 1");
  await expect(page.getByRole("alert").filter({ hasText: "Some tasks were not completed" })).toContainText("execution_unsettled");
  await page.getByRole("button", { name: "Retry All Failed" }).click();
  await expect(page.getByRole("status", { name: "Queue utility result" })).toContainText("Matched 4; queued 4; failed 0");
  await page.getByRole("button", { name: "Retry All Failed" }).click();
  await expect(page.getByRole("alert").filter({ hasText: "retry service unavailable" })).toContainText("retry service unavailable");
  expect(unhandled).toEqual([]);
});

test("accepted admin clear retains current data until terminal completion and keeps failed confirmation reviewable", async ({ context, page }) => {
  let clearAttempts = 0;
  let operationComplete = false;
  const sentinel = { id: "creator-before-clear", name: "clear_sentinel", display_name: "Clear Sentinel", description: null, is_active: true, is_favorite: false, created_at: "2026-09-12T00:00:00Z", updated_at: "2026-09-12T00:00:00Z", repository_count: 0, source_count: 0, subscription_count: 0, last_synced_at: null };
  const unhandled = await installJobsFixture(context, principal(["system", "library"]), async (route, path) => {
    if (path === "/api/v1/search/name-anchors") { await json(route, { scope: "creators", direction: "asc", total: operationComplete ? 0 : 1, items: [] }); return true; }
    if (path === "/api/v1/search") {
      const items = operationComplete ? [] : [sentinel];
      await json(route, { query: "", canonical_query: "", parsed: { raw: "", canonical: "", scope: "creators", targets: ["creators"], tokens: [] }, groups: { creators: { total: items.length, items } }, total: items.length, results: [], creators: items, tags: [], repositories: [], subscriptions: [] });
      return true;
    }
    if (path === "/api/v1/admin/system-info") { await json(route, { version: "fixture", downloads_size_mb: 0, library_size_mb: 0, downloads_free_gb: 1, archives_kb: {}, db_stats: { works: 0, assets: 0, creators: 1, subscriptions: 0, tags: 0 } }); return true; }
    if (path === "/api/v1/admin/storage-breakdown") { await json(route, { sources: {}, creator_tree: [], unlinked_repositories: [], db_stats: { works: 0, assets: 0, creators: 1, subscriptions: 0, tags: 0 } }); return true; }
    if (path === "/api/v1/admin/backup/list") { await json(route, { backups: [] }); return true; }
    if (path === "/api/v1/admin/backup/latest" || path === "/api/v1/admin/integrity-check/latest") { await json(route, { current: null, snapshot: null }); return true; }
    if (path === "/api/v1/admin/clear/preview/all") { await json(route, { entity: "all", confirmation_phrase: "DELETE-ALL-DATA", counts: { creators: 1 }, preserves_repository_sync_receipts: true, deletes_media_files: true }); return true; }
    if (path === "/api/v1/admin/operations/clear") {
      clearAttempts += 1;
      if (clearAttempts === 1) await json(route, { detail: "clear queue unavailable" }, 503);
      else await json(route, { job_id: "clear-job-1", status: "enqueued" }, 202);
      return true;
    }
    if (path === "/api/v1/admin/operations/clear-job-1") {
      await json(route, operationComplete
        ? { job_id: "clear-job-1", status: "complete", operation_type: "admin-clear", result: { message: "Clear completed" } }
        : { job_id: "clear-job-1", status: "running", operation_type: "admin-clear", progress: { phase: "clearing", label: "Clearing data" } });
      return true;
    }
    return false;
  });

  await page.goto("/admin/creators");
  await expect(page.getByText("Clear Sentinel", { exact: true })).toBeVisible();
  await page.getByRole("link", { name: "Data Mgmt" }).click();
  await page.getByRole("button", { name: "Delete All Data" }).click();
  const dialog = page.getByRole("dialog");
  const confirmation = dialog.getByRole("textbox", { name: "Type DELETE-ALL-DATA to confirm" });
  await confirmation.fill("DELETE-ALL-DATA");
  await dialog.getByRole("button", { name: "Confirm" }).click();
  await expect(dialog).toBeVisible();
  await expect(confirmation).toHaveValue("DELETE-ALL-DATA");
  await expect(page.getByText("clear queue unavailable", { exact: true })).toBeVisible();
  await dialog.getByRole("button", { name: "Confirm" }).click();
  await expect(dialog).toBeHidden();
  await expect(page.getByText("Delete All Data queued", { exact: true })).toBeVisible();

  await page.getByRole("link", { name: "Creators" }).click();
  await expect(page.getByText("Clear Sentinel", { exact: true })).toBeVisible();
  operationComplete = true;
  await page.getByRole("link", { name: "Data Mgmt" }).click();
  await expect(page.getByText("Clear completed", { exact: true })).toBeVisible({ timeout: 10_000 });
  await page.getByRole("link", { name: "Creators" }).click();
  await expect(page.getByText("No creators", { exact: true })).toBeVisible();
  expect(clearAttempts).toBe(2);
  expect(unhandled).toEqual([]);
});

for (const role of [
  { name: "system", me: principal(["system"]), visible: true },
  { name: "tasks-only", me: principal(["tasks"]), visible: false },
  { name: "denied", me: principal([]), visible: false },
]) {
  test(`${role.name} principal sees only its permitted compaction controls`, async ({ context, page }) => {
    const unhandled = await installJobsFixture(context, role.me, async () => false);
    await page.goto("/admin/jobs");
    const dangerZone = page.locator("details").filter({ hasText: "Danger zone" });
    await expect(dangerZone).toHaveCount(role.visible ? 1 : 0);
    if (role.visible) {
      await dangerZone.locator("summary").click();
      await expect(dangerZone.locator("button", { hasText: "Preview compaction" })).toBeVisible();
    }
    expect(unhandled).toEqual([]);
  });
}

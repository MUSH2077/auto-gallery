import { expect, test, type BrowserContext, type Route } from "@playwright/test";

test.describe.configure({ timeout: 90_000 });

type Principal = { id: number; username: string; display_name: string; is_admin: boolean; is_active: boolean; permissions: string[]; modules: Record<string, boolean>; preferences: Record<string, never>; nsfw_visible: boolean; upload_used_bytes: number; must_change_password: boolean };
const principal = (permissions: string[], isAdmin = false): Principal => ({
  id: isAdmin ? 1 : permissions.length + 10,
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
const workbench = { updated_at: "2026-09-12T00:00:00Z", queue: { default: 0, scheduled: 0, failed: 0, active_download_count: 0, active_import_count: 0, failed_download_count: 0, failed_import_count: 0, stale_download_count: 0, stale_import_count: 0, stale_count: 0 }, scheduler: { enabled: true, mode: "interval", timezone: "UTC", scan_interval_minutes: 60 }, storage: { disk_total_bytes: 1, disk_free_bytes: 1, disk_used_bytes: 0, risk_level: "ok" }, health: {}, attention: { auth_unhealthy_count: 0, failed_download_count: 0, failed_import_count: 0, stale_job_count: 0, low_disk_warning: false, scheduler_disabled_warning: false }, recent: { download_jobs: [], import_jobs: [], works: [], successful_syncs: [] } };
const json = (route: Route, body: unknown, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });

async function installFixture(context: BrowserContext, me: Principal, handler: (route: Route, path: string) => Promise<boolean>) {
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
    if (path === "/api/v1/system/scheduler-decisions") return json(route, { total: 0, items: [], summary: { blocked_count: 0 } });
    if (path === "/api/v1/operations/overview") return json(route, { summary: { attention: 0 }, items: [] });
    unhandled.push(`${route.request().method()} ${path}`);
    return json(route, { detail: `Unhandled fixture request: ${path}` }, 501);
  });
  return unhandled;
}

for (const role of [
  { name: "denied", me: principal([]), allowed: false },
  { name: "system", me: principal(["system"]), allowed: true },
  { name: "admin", me: principal([], true), allowed: true },
]) {
  test(`${role.name} direct backup route starts hooks only when authorized`, async ({ context, page }) => {
    const backupRequests: string[] = [];
    const unhandled = await installFixture(context, role.me, async (route, path) => {
      if (!path.startsWith("/api/v1/admin/backup")) return false;
      backupRequests.push(`${route.request().method()} ${path}`);
      if (path === "/api/v1/admin/backup/list") await json(route, { backups: [] });
      else if (path === "/api/v1/admin/backup/latest" || path === "/api/v1/admin/backup/estimate/latest") await json(route, { current: null, snapshot: null });
      else await json(route, { detail: "Unexpected backup mutation" }, 501);
      return true;
    });

    await page.goto("/admin/settings/backup");
    if (!role.allowed) {
      await expect(page.getByRole("heading", { name: "You don't have permission to access this page" })).toBeVisible();
      await expect(page.getByRole("button", { name: "Create Backup" })).toHaveCount(0);
      expect(backupRequests).toEqual([]);
    } else {
      await expect(page.getByRole("heading", { level: 1, name: "Backup & Restore" })).toBeVisible();
      await expect(page.getByRole("button", { name: "Create Backup" })).toBeVisible();
      expect(backupRequests).toEqual(expect.arrayContaining([
        "GET /api/v1/admin/backup/list",
        "GET /api/v1/admin/backup/latest",
        "GET /api/v1/admin/backup/estimate/latest",
      ]));
    }
    expect(unhandled).toEqual([]);
  });
}

test("reindex rejection retains confirmation and suppresses duplicates before successful retry", async ({ context, page }) => {
  let reindexCalls = 0;
  let releaseFailure!: () => void;
  const heldFailure = new Promise<void>((resolve) => { releaseFailure = resolve; });
  const unhandled = await installFixture(context, principal(["system"]), async (route, path) => {
    if (path === "/api/v1/admin/settings") { await json(route, { dedup: {} }); return true; }
    if (path === "/api/v1/admin/search/reindex") {
      reindexCalls += 1;
      if (reindexCalls === 1) {
        await heldFailure;
        await json(route, { detail: "search reindex queue unavailable" }, 503);
      } else {
        await json(route, { status: "enqueued", job_id: "reindex-job-1", message: "queued" }, 202);
      }
      return true;
    }
    return false;
  });

  await page.goto("/admin/settings");
  await page.getByRole("button", { name: "Reindex Now" }).click();
  const dialog = page.getByRole("dialog", { name: "Reindex Search" });
  await dialog.getByRole("button", { name: "Confirm" }).click();
  await expect.poll(() => reindexCalls).toBe(1);
  await expect(dialog.getByRole("button", { name: "Processing..." })).toBeDisabled();
  await dialog.getByRole("button", { name: "Processing..." }).click({ force: true });
  expect(reindexCalls).toBe(1);
  releaseFailure();
  await expect(dialog).toBeVisible();
  await expect(dialog.getByRole("alert")).toContainText("search reindex queue unavailable");
  await dialog.getByRole("button", { name: "Confirm" }).click();
  await expect.poll(() => reindexCalls).toBe(2);
  await expect(dialog).toBeHidden();
  await expect(page.getByText("Search reindex started.", { exact: true })).toBeVisible();
  expect(unhandled).toEqual([]);
});

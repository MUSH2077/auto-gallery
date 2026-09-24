import { expect, test, type Route } from "@playwright/test";
test.describe.configure({ timeout: 60_000 });

const me = { id: 1, username: "admin", display_name: "Admin", is_admin: true, is_active: true, permissions: ["system", "tasks"], modules: {}, preferences: {}, nsfw_visible: true, upload_used_bytes: 0, must_change_password: false };
const workbench = { updated_at: "2026-09-08T00:00:00Z", queue: { default: 0, scheduled: 0, failed: 0, active_download_count: 0, active_import_count: 0, failed_download_count: 0, failed_import_count: 0, stale_download_count: 0, stale_import_count: 0, stale_count: 0 }, scheduler: { enabled: true, mode: "interval", timezone: "UTC", scan_interval_minutes: 60 }, storage: { disk_total_bytes: 1, disk_free_bytes: 1, disk_used_bytes: 0, risk_level: "ok" }, health: {}, attention: { auth_unhealthy_count: 0, failed_download_count: 0, failed_import_count: 0, stale_job_count: 0, low_disk_warning: false, scheduler_disabled_warning: false }, recent: { download_jobs: [], import_jobs: [], works: [], successful_syncs: [] } };
const json = (route: Route, body: unknown, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });

test("backup delete retains its dialog on failure and download sends bearer", async ({ context, page }) => {
  await context.addCookies([{ name: "ag_token", value: "fixture", domain: "127.0.0.1", path: "/" }]);
  await context.addInitScript(() => { localStorage.setItem("ag_token", "fixture"); localStorage.setItem("auto-gallery-lang", "en"); });
  let deletes = 0; let downloadAuth = ""; let downloads = 0; const unhandled: string[] = [];
  await context.route("**/api/v1/**", async (route) => {
    const req = route.request(); const url = new URL(req.url()); const path = url.pathname;
    if (path === "/api/v1/auth/me") return json(route, me);
    if (path === "/api/v1/system/workbench") return json(route, workbench);
    if (path === "/api/v1/admin/backup/list") return json(route, { backups: [{ filename: "safe.tar.gz", size_mb: 1, size_bytes: 1024, created_at: "2026-09-08T00:00:00Z", contents: ["database"], restorable: true, component_sizes: {} }] });
    if (path.includes("/latest")) return json(route, { current: null, snapshot: null });
    if (path === "/api/v1/admin/backup/safe.tar.gz" && req.method() === "DELETE") { deletes += 1; return deletes === 1 ? json(route, { detail: "Backup is in use" }, 409) : json(route, { status: "ok", message: "deleted" }); }
    if (path === "/api/v1/admin/backup/download") {
      downloadAuth = req.headers()["authorization"] || ""; downloads += 1;
      if (downloads === 1) return route.fulfill({ status: 200, headers: { "Content-Type": "application/gzip", "Content-Disposition": "attachment; filename=server-safe.tar.gz" }, body: "bytes" });
      if (downloads === 2) return json(route, { status: "error", message: "No backups available" });
      if (downloads === 3) return route.fulfill({ status: 200, headers: { "Content-Type": "text/html" }, body: "<h1>gateway</h1>" });
      return route.fulfill({ status: 200, body: "headerless" });
    }
    if (path === "/api/v1/auth/ws-ticket") return json(route, { detail: "fixture websocket unavailable" }, 503);
    if (path === "/api/v1/tasks") return json(route, { total: 0, items: [] });
    if (path === "/api/v1/system/scheduler-decisions") return json(route, { total: 0, items: [], summary: { blocked_count: 0 } });
    if (path === "/api/v1/operations/overview") return json(route, { summary: { attention: 0 }, items: [] });
    unhandled.push(`${req.method()} ${path}`);
    return json(route, { detail: `Unhandled fixture request: ${path}` }, 501);
  });
  await page.goto("/admin/settings/backup");
  const menu = page.getByRole("button", { name: "More actions" });
  await menu.focus();
  await page.keyboard.press("ArrowDown");
  await expect(page.getByRole("menuitem", { name: "Delete" })).toBeFocused();
  await page.keyboard.press("End");
  await page.keyboard.press("Enter");
  await page.getByRole("button", { name: "Confirm" }).click();
  await expect(page.getByRole("dialog")).toContainText("Backup is in use");
  await page.getByRole("button", { name: "Confirm" }).click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await page.evaluate(() => {
    (window as any).__downloads = [];
    URL.createObjectURL = (blob: Blob) => { (window as any).__downloads.push({ type: blob.type, size: blob.size }); return "blob:fixture"; };
    URL.revokeObjectURL = () => undefined;
    HTMLAnchorElement.prototype.click = function click() { (window as any).__downloads.push({ filename: this.download, href: this.href }); };
  });
  await page.getByRole("button", { name: "Download" }).click();
  await expect.poll(() => downloadAuth).toBe("Bearer fixture");
  await expect.poll(() => page.evaluate(() => (window as any).__downloads)).toEqual([{ type: "application/gzip", size: 5 }, { filename: "server-safe.tar.gz", href: "blob:fixture" }]);
  await page.getByRole("button", { name: "Download" }).click();
  await expect(page.getByText("No backups available")).toBeVisible();
  await page.getByRole("button", { name: "Download" }).click();
  await expect(page.getByText(/unexpected backup archive content type: text\/html/i)).toBeVisible();
  await page.getByRole("button", { name: "Download" }).click();
  await expect(page.getByText(/unexpected backup archive content type: missing/i)).toBeVisible();
  expect(await page.evaluate(() => (window as any).__downloads)).toHaveLength(2);
  expect(deletes).toBe(2);
  expect(unhandled).toEqual([]);
});

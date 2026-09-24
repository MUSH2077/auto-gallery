import { expect, test, type BrowserContext, type Page, type Route } from "@playwright/test";

test.describe.configure({ timeout: 60_000 });
test.use({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true });

const me = {
  id: 1,
  username: "admin",
  display_name: "Admin",
  is_admin: true,
  is_active: true,
  permissions: ["system", "tasks"],
  modules: {},
  preferences: {},
  nsfw_visible: true,
  upload_used_bytes: 0,
  must_change_password: false,
};

const workbench = {
  updated_at: "2026-09-13T00:00:00Z",
  queue: {
    default: 0,
    scheduled: 0,
    failed: 0,
    active_download_count: 0,
    active_import_count: 0,
    failed_download_count: 0,
    failed_import_count: 0,
    stale_download_count: 0,
    stale_import_count: 0,
    stale_count: 0,
  },
  scheduler: { enabled: true, mode: "interval", timezone: "UTC", scan_interval_minutes: 60 },
  storage: { disk_total_bytes: 1, disk_free_bytes: 1, disk_used_bytes: 0, risk_level: "ok" },
  health: {},
  attention: {
    auth_unhealthy_count: 0,
    failed_download_count: 0,
    failed_import_count: 0,
    stale_job_count: 0,
    low_disk_warning: false,
    scheduler_disabled_warning: false,
  },
  recent: { download_jobs: [], import_jobs: [], works: [], successful_syncs: [] },
};

const backup = {
  filename: "auto-gallery-backup_20260912_165814.tar.gz",
  size_mb: 0.5,
  size_bytes: 512_000,
  created_at: "2026-09-12T16:58:14Z",
  contents: ["database"],
  restorable: true,
  component_sizes: { database: 0 },
};

const json = (route: Route, body: unknown, status = 200) => route.fulfill({
  status,
  contentType: "application/json",
  body: JSON.stringify(body),
});

async function mountBackupPage(page: Page, context: BrowserContext) {
  await context.addCookies([{ name: "ag_session", value: "fixture", domain: "127.0.0.1", path: "/" }, { name: "ag_csrf", value: "fixture-csrf", url: process.env.PLAYWRIGHT_BASE_URL || "http://127.0.0.1:13000" }]);
  await context.addInitScript(() => {
    localStorage.setItem("ag_token", "fixture");
    localStorage.setItem("auto-gallery-lang", "zh");
    localStorage.setItem("auto-gallery-restore-upload-v1", JSON.stringify({
      token: "restore-fixture",
      session: {
        upload_id: "restore-upload-fixture",
        filename: "auto-gallery-backup_20260912_165814.tar.gz",
        size_bytes: 512000,
        sha256: "a".repeat(64),
        chunk_size: 1048576,
        total_chunks: 1,
        received_chunks: 1,
        received_bytes: 512000,
        next_chunk: 1,
        state: "ready",
        validation_task_id: "validation-task-fixture",
        request_id: "restore-request-fixture",
        created_at: "2026-09-13T00:00:00Z",
        updated_at: "2026-09-13T00:00:01Z",
      },
    }));
  });
  const writes: string[] = [];
  const unhandled: string[] = [];
  await context.route("**/api/v1/**", async (route: Route) => {
    const request = route.request();
    const pathname = new URL(request.url()).pathname;
    if (pathname === "/api/v1/auth/me") return json(route, me);
    if (pathname === "/api/v1/system/workbench") return json(route, workbench);
    if (pathname === "/api/v1/admin/backup/list") return json(route, { backups: [backup] });
    if (pathname === "/api/v1/admin/backup/latest") return json(route, {
      current: null,
      snapshot: {
        task_id: "backup-task-fixture",
        job_id: "backup-task-fixture",
        status: "complete",
        operation_type: "admin-backup-create",
        progress: { phase: "complete", label: "Backup created" },
        result: { ...backup, status: "ok", message: "Backup created" },
        completed_at: "2026-09-13T00:00:02Z",
      },
    });
    if (pathname === "/api/v1/admin/backup/estimate/latest") return json(route, {
      current: null,
      snapshot: {
        task_id: "estimate-task-fixture",
        job_id: "estimate-task-fixture",
        status: "complete",
        operation_type: "admin-backup-estimate",
        progress: { phase: "complete", label: "Backup estimate complete" },
        result: { components: { database: 0, "gallerydl-config": 2, "app-config": 4500, "download-archives": 0, "library-metadata": 3 } },
        completed_at: "2026-09-13T00:00:01Z",
      },
    });
    if (pathname === "/api/v1/admin/backup/restore/uploads/restore-upload-fixture/validation/latest") return json(route, {
      current: null,
      snapshot: {
        task_id: "validation-task-fixture",
        job_id: "validation-task-fixture",
        status: "complete",
        operation_type: "admin-restore-validate",
        progress: { phase: "complete", label: "Restore request ready" },
        result: { state: "ready", request_id: "restore-request-fixture", host_command: "./scripts/offline-restore.py --request fixture" },
        completed_at: "2026-09-13T00:00:03Z",
      },
    });
    if (pathname === "/api/v1/admin/backup/restore/receipts/restore-request-fixture") return json(route, {
      request_id: "restore-request-fixture", status: "pending", phase: "handoff",
    });
    if (pathname === "/api/v1/auth/ws-ticket") return json(route, { detail: "fixture websocket unavailable" }, 503);
    if (pathname === "/api/v1/tasks") return json(route, { total: 0, items: [] });
    if (pathname === "/api/v1/system/scheduler-decisions") return json(route, { total: 0, items: [], summary: { blocked_count: 0 } });
    if (pathname === "/api/v1/operations/overview") return json(route, { summary: { attention: 0 }, items: [] });
    if (!["GET", "HEAD", "OPTIONS"].includes(request.method())) writes.push(`${request.method()} ${pathname}`);
    unhandled.push(`${request.method()} ${pathname}`);
    return json(route, { detail: `Unhandled fixture request: ${pathname}` }, 501);
  });
  await page.goto("/admin/settings/backup");
  await expect(page.getByRole("heading", { name: "已准备好进行离线主机执行", exact: true })).toBeVisible();
  await expect(page.locator("span", { hasText: backup.filename }).filter({ hasText: backup.filename })).toBeVisible();
  return { writes, unhandled };
}

test("mobile backup row menu activates Delete after the trigger scrolls into view", async ({ context, page }) => {
  const { writes, unhandled } = await mountBackupPage(page, context);
  const trigger = page.getByRole("button", { name: "更多操作", exact: true });

  await trigger.click();
  expect(await page.evaluate(() => window.scrollY)).toBeGreaterThan(0);
  await page.getByRole("menuitem", { name: "删除", exact: true }).click();

  await expect(page.getByRole("dialog")).toContainText(backup.filename);
  await page.getByRole("button", { name: "取消", exact: true }).click();
  expect(writes).toEqual([]);
  expect(unhandled).toEqual([]);
});

test("row menu still dismisses on outside pointer and real scroll, with keyboard focus restored", async ({ context, page }) => {
  const { writes, unhandled } = await mountBackupPage(page, context);
  const trigger = page.getByRole("button", { name: "更多操作", exact: true });
  const item = page.getByRole("menuitem", { name: "删除", exact: true });

  await trigger.click();
  await expect(item).toBeVisible();
  await page.locator("span", { hasText: backup.filename }).filter({ hasText: backup.filename }).click();
  await expect(item).toHaveCount(0);

  await trigger.click();
  await expect(item).toBeVisible();
  const beforeScroll = await page.evaluate(() => window.scrollY);
  await page.evaluate(() => window.scrollBy(0, -100));
  await expect.poll(() => page.evaluate(() => window.scrollY)).toBeLessThan(beforeScroll);
  await expect(item).toHaveCount(0);

  await trigger.focus();
  await page.keyboard.press("ArrowDown");
  await expect(item).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(item).toHaveCount(0);
  await expect(trigger).toBeFocused();

  await trigger.click();
  await expect(item).toBeVisible();
  await page.setViewportSize({ width: 391, height: 844 });
  await expect(item).toHaveCount(0);
  expect(writes).toEqual([]);
  expect(unhandled).toEqual([]);
});

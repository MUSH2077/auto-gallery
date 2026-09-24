import { expect, test } from "@playwright/test";

const USER = {
  id: 1,
  username: "performance-review",
  display_name: "Performance Review",
  is_admin: true,
  is_active: true,
  permissions: [],
  modules: {},
  preferences: {},
  nsfw_visible: true,
  upload_quota_bytes: null,
  upload_used_bytes: 0,
  must_change_password: false,
};

const WORKBENCH = {
  updated_at: "2026-09-21T08:00:00Z",
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
  scheduler: {
    enabled: true,
    mode: "interval",
    timezone: "Asia/Shanghai",
    scan_interval_minutes: 15,
  },
  storage: {
    disk_total_bytes: 1_000_000,
    disk_free_bytes: 800_000,
    disk_used_bytes: 200_000,
    disk_used_percent: 20,
    disk_free_percent: 80,
    risk_level: "ok",
  },
  health: {},
  attention: {
    auth_unhealthy_count: 0,
    auth_actionable_count: 0,
    auth_disabled_or_unchecked_count: 0,
    credential_issue_count: 0,
    failed_download_count: 0,
    failed_import_count: 0,
    stale_job_count: 0,
    low_disk_warning: false,
    scheduler_disabled_warning: false,
  },
  recent: { download_jobs: [], import_jobs: [], works: [], successful_syncs: [] },
};

test("an idle admin tab makes no more than four recurring API requests per minute", async ({ context, page }) => {
  let recurringRequests = 0;
  await context.routeWebSocket("**/api/v1/ws*", (webSocket) => {
    webSocket.send(JSON.stringify({ type: "connected" }));
  });
  await context.addCookies([
    {
      name: "ag_session",
      value: "performance-token",
      domain: "127.0.0.1",
      path: "/",
    },
  { name: "ag_csrf", value: "fixture-csrf", url: process.env.PLAYWRIGHT_BASE_URL || "http://127.0.0.1:13000" }]);
  await context.addInitScript(() => {
    localStorage.setItem("ag_token", "performance-token");
    localStorage.setItem("auto-gallery-lang", "en");
  });
  await context.route("**/api/v1/**", async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname === "/api/v1/auth/me") return route.fulfill({ json: USER });
    if (url.pathname === "/api/v1/system/workbench") {
      recurringRequests += 1;
      return route.fulfill({ json: WORKBENCH });
    }
    if (url.pathname === "/api/v1/operations/overview") {
      recurringRequests += 1;
      return route.fulfill({ json: { summary: { active: 0, attention: 0 }, items: [] } });
    }
    return route.fulfill({ json: {} });
  });
  await page.clock.install();

  await page.goto("/admin");
  await expect(page.getByRole("heading", { name: "System Dashboard" })).toBeVisible();
  const initialRequests = recurringRequests;

  await page.clock.runFor(61_000);
  await expect.poll(() => recurringRequests).toBeGreaterThan(initialRequests);

  expect(recurringRequests - initialRequests).toBeLessThanOrEqual(4);
});

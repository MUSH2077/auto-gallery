import { expect, test, type BrowserContext, type Route } from "@playwright/test";

test.describe.configure({ timeout: 60_000 });

const user = {
  id: 7,
  username: "operations-fixture",
  display_name: "Operations Fixture",
  is_admin: false,
  is_active: true,
  permissions: ["system", "tasks", "subscriptions"],
  modules: { system: true, tasks: true, subscriptions: true },
  preferences: {},
  nsfw_visible: true,
  upload_used_bytes: 0,
  must_change_password: false,
};

const json = (route: Route, body: unknown, status = 200) => route.fulfill({
  status,
  contentType: "application/json",
  body: JSON.stringify(body),
});

async function install(context: BrowserContext, options: { failUntilRetry?: boolean } = {}) {
  const taskRequests: URL[] = [];
  let taskReads = 0;
  let releaseTaskFailures = false;
  await context.addCookies([{ name: "ag_session", value: "operations-fixture", domain: "127.0.0.1", path: "/" }]);
  await context.addInitScript(() => {
    localStorage.setItem("ag_token", "operations-fixture");
    localStorage.setItem("auto-gallery-lang", "en");
    localStorage.setItem("auto-gallery-theme", "light");
  });
  await context.route("**/api/v1/**", async (route) => {
    const url = new URL(route.request().url());
    const path = url.pathname;
    if (path === "/api/v1/auth/me") return json(route, user);
    if (path === "/api/v1/auth/ws-ticket") return json(route, { detail: "fixture websocket unavailable" }, 503);
    if (path === "/api/v1/tasks") {
      taskReads += 1;
      taskRequests.push(url);
      if (options.failUntilRetry && !releaseTaskFailures) return json(route, { detail: "fixture notification read failed" }, 503);
      const offset = Number(url.searchParams.get("offset") || 0);
      return json(route, {
        total: 51,
        items: offset === 0 ? [{
          id: "task-operation",
          kind: "download",
          operation_type: "download",
          status: "running",
          title: "Fixture task",
          source: "pixiv",
          progress_current: 2,
          progress_total: 4,
          created_at: "2026-10-24T00:00:00Z",
        }] : [],
      });
    }
    if (path === "/api/v1/system/health") return json(route, {
      services: { redis: "up", database: "up" },
      resource_pressure: { status: "normal", controller_mode: "normal", memory: {}, swap: {}, psi: {}, redis: {}, download_concurrency: {} },
      business: { outboxes: {} }, version: "fixture", build_revision: "fixture",
    });
    if (path === "/api/v1/sources") return json(route, { sources: [] });
    return json(route, { total: 0, items: [], sources: [] });
  });
  return { taskRequests, taskReads: () => taskReads, releaseTaskFailures: () => { releaseTaskFailures = true; } };
}

test("notifications keeps filters, accessible progress, paging and task destination", async ({ context, page }) => {
  const fixture = await install(context);
  await page.goto("/admin/notifications");
  await expect(page.getByRole("heading", { level: 1, name: "Notifications" })).toBeVisible();
  const allTab = page.getByRole("tab", { name: "All" });
  await expect(allTab).toHaveAttribute("aria-selected", "true");
  await expect(allTab).toHaveAttribute("aria-controls", "notifications-panel");
  await expect(page.getByText("Fixture task", { exact: true })).toBeVisible();
  const progress = page.getByRole("progressbar", { name: "Fixture task" });
  await expect(progress).toHaveAttribute("aria-valuenow", "50");
  await expect(progress).toHaveAttribute("aria-valuemax", "100");

  await page.getByRole("tab", { name: "Tasks" }).click();
  await expect.poll(() => fixture.taskRequests.some((url) => !url.searchParams.has("kind") && !url.searchParams.has("include_account"))).toBeTruthy();
  await page.getByRole("tab", { name: "Account" }).click();
  await expect.poll(() => fixture.taskRequests.some((url) => url.searchParams.get("kind") === "account" && url.searchParams.get("include_account") === "true")).toBeTruthy();
  await page.getByRole("tab", { name: "All" }).click();

  const loadMore = page.getByRole("button", { name: "Load more" });
  await expect(loadMore).toBeVisible();
  await loadMore.click();
  await expect.poll(() => fixture.taskRequests.filter((url) => url.searchParams.get("offset") === "1").length).toBeGreaterThan(0);

  const taskLink = page.getByRole("link", { name: /Fixture task/ });
  await expect(taskLink).toHaveAttribute("href", /\/admin\/jobs\?tab=downloads&task=task-operation/);
  await taskLink.click();
  await expect(page).toHaveURL(/\/admin\/jobs\?tab=downloads&task=task-operation/);
});

test("notifications exposes a failed read and recovers through retry", async ({ context, page }) => {
  const fixture = await install(context, { failUntilRetry: true });
  await page.goto("/admin/notifications");
  await expect(page.getByText("Failed to load notifications", { exact: true })).toBeVisible({ timeout: 20_000 });
  const failedReads = fixture.taskReads();
  expect(failedReads).toBeGreaterThan(0);
  fixture.releaseTaskFailures();
  await page.getByRole("button", { name: /Retry/i }).click();
  await expect.poll(() => fixture.taskReads()).toBeGreaterThan(failedReads);
  await expect(page.getByText("Fixture task", { exact: true })).toBeVisible();
});

test("system preserves services and source registry tabs with refresh action", async ({ context, page }) => {
  await install(context);
  await page.goto("/admin/system?tab=services");
  await expect(page.getByRole("heading", { level: 1, name: "System & Sources" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Refresh" })).toBeVisible();
  await expect(page.getByRole("tab", { name: "Service Status" })).toHaveAttribute("aria-selected", "true");
  await page.getByRole("tab", { name: "Sources" }).click();
  await expect(page).toHaveURL(/tab=sources/);
  await expect(page.getByRole("tab", { name: "Sources" })).toHaveAttribute("aria-selected", "true");
});
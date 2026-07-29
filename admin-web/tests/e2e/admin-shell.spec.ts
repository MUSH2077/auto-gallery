import { expect, test, type BrowserContext, type Page } from "@playwright/test";

const me = {
  id: 1,
  username: "ui-review",
  display_name: "UI Review",
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

const workbench = {
  updated_at: "2026-07-27T12:00:00Z",
  queue: {
    default: 3,
    scheduled: 1,
    failed: 1,
    active_download_count: 1,
    active_import_count: 0,
    failed_download_count: 1,
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
    disk_total_bytes: 1000000000,
    disk_free_bytes: 600000000,
    disk_used_bytes: 400000000,
    disk_used_percent: 40,
    disk_free_percent: 60,
    risk_level: "ok",
  },
  health: {},
  attention: {
    auth_unhealthy_count: 0,
    failed_download_count: 1,
    failed_import_count: 0,
    stale_job_count: 0,
    low_disk_warning: false,
    scheduler_disabled_warning: false,
  },
  recent: { download_jobs: [], import_jobs: [], tasks: [] },
};

const longDownloadJob = {
  id: "download-job-0123456789",
  subscription_id: "subscription-0123456789",
  subscription_source_id: "source-0123456789",
  subscription_name: "A deliberately long subscription name for responsive overflow validation",
  creator_name: "xianyuliangryo-with-a-very-long-creator-name",
  source: "x",
  source_url: "https://x.com/xianyuliangryo/status/123456789012345678901234567890",
  status: "complete",
  operation_type: "download",
  retry_count: 0,
  created_at: "2026-07-27T11:40:00Z",
  updated_at: "2026-07-27T11:45:00Z",
  progress_data: null,
  pipeline_stage: "complete",
  error_log: null,
};

async function installFixtureRoutes(context: BrowserContext) {
  await context.addCookies([{
    name: "ag_token",
    value: "ui-test-token",
    domain: "127.0.0.1",
    path: "/",
  }]);
  await context.addInitScript(() => {
    window.localStorage.setItem("ag_token", "ui-test-token");
    window.localStorage.setItem("auto-gallery-lang", "en");
    window.localStorage.setItem("auto-gallery-theme", "dark");
  });
  await context.route("**/api/v1/**", async (route) => {
    const url = new URL(route.request().url());
    const path = url.pathname;
    if (path === "/api/v1/auth/me") {
      await route.fulfill({ json: me });
    } else if (path === "/api/v1/system/workbench") {
      await route.fulfill({ json: workbench });
    } else if (path === "/api/v1/download-jobs") {
      await route.fulfill({ json: [longDownloadJob] });
    } else if (path === "/api/v1/tasks") {
      await route.fulfill({ json: { items: [], total: 0, offset: 0, limit: 50 } });
    } else if (path === "/api/v1/import-jobs") {
      await route.fulfill({ json: { items: [], total: 0, offset: 0, limit: 50 } });
    } else if (path === "/api/v1/works") {
      await route.fulfill({ json: { items: [], total: 0 } });
    } else if (path === "/api/v1/tags") {
      await route.fulfill({ json: [] });
    } else if (path === "/api/v1/sources") {
      await route.fulfill({ json: { sources: [] } });
    } else if (path.includes("/notifications")) {
      await route.fulfill({ json: { items: [], total: 0, unread_count: 0 } });
    } else {
      await route.fulfill({ json: {} });
    }
  });
}

async function expectNoPageOverflow(page: Page) {
  await expect.poll(() => page.evaluate(() => {
    const root = document.documentElement;
    return root.scrollWidth <= root.clientWidth;
  })).toBe(true);
}

test.beforeEach(async ({ context }) => {
  await installFixtureRoutes(context);
});

test("desktop sidebar, contextual navigation, and command palette remain usable", async ({ page }) => {
  const consoleErrors: string[] = [];
  const pageErrors: string[] = [];
  page.on("console", (message) => {
    if (message.type() === "error") consoleErrors.push(message.text());
  });
  page.on("pageerror", (error) => pageErrors.push(error.message));
  await page.setViewportSize({ width: 1440, height: 960 });
  await page.goto("/admin/jobs?tab=downloads");
  await expect(page).toHaveURL(/\/admin\/jobs\?tab=downloads/);
  expect(await page.title()).not.toBe("");
  await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
  await expect(page.locator("body")).not.toHaveText("");
  await expect(page.locator("[data-nextjs-dialog-overlay]")).toHaveCount(0);
  await expect(page.locator("aside").first()).toHaveCSS("width", "248px");
  const sidebar = page.locator("#admin-sidebar");
  await expect(sidebar.locator("nav a")).toHaveCount(12);
  await expect(sidebar.getByRole("heading", { name: "Upload & Import" })).toBeVisible();
  await expect(sidebar.getByRole("link", { name: "Upload" })).toBeVisible();
  await expect(sidebar.getByRole("link", { name: "Danbooru" })).toBeVisible();
  await expect(sidebar.getByRole("link", { name: "Notifications" })).toHaveCount(0);
  const contextNav = page.locator("#main-content header nav");
  await expect(contextNav.getByRole("link", { name: "Jobs" })).toBeVisible();
  await expect(contextNav.getByRole("link", { name: "Scheduler" })).toBeVisible();
  await expect(contextNav.getByRole("link", { name: "Notifications" })).toHaveCount(0);
  await expect(page.getByTestId("source-code-link")).toHaveAttribute(
    "href",
    "https://github.com/MUSH2077/auto-gallery",
  );
  await expectNoPageOverflow(page);

  await page.keyboard.press("Control+k");
  const dialog = page.getByRole("dialog");
  await expect(dialog).toBeVisible();
  await dialog.getByRole("textbox").fill("creator");
  await expect(dialog.getByRole("option").first()).toBeVisible();
  await dialog.getByRole("textbox").fill("notifications");
  await expect(dialog.getByRole("option", { name: /^Notifications\b/ })).toHaveCount(0);
  await page.screenshot({ path: "/tmp/auto-gallery-command-palette.png", fullPage: false });
  await page.keyboard.press("Escape");
  await expect(dialog).toBeHidden();

  const notificationBell = page.locator('header button[aria-label="Notifications"]');
  await notificationBell.click();
  await page.getByRole("button", { name: /Notifications\s*→/ }).click();
  await expect(page).toHaveURL(/\/admin\/notifications$/);
  await expect(page.locator("#main-content").getByRole("heading", { level: 1, name: "Notifications" })).toBeVisible();
  await expect(page.locator("#main-content header nav")).toHaveCount(0);
  expect(pageErrors).toEqual([]);
  expect(consoleErrors.filter((message) => !message.includes("WebSocket"))).toEqual([]);
});

test("top-level page headers share the task page alignment and works has no creator picker", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 960 });

  const headingX = async (path: string, name: string) => {
    await page.goto(path);
    const heading = page.locator("#main-content").getByRole("heading", { level: 1, name });
    await expect(heading).toBeVisible();
    const box = await heading.boundingBox();
    expect(box).not.toBeNull();
    return box!.x;
  };

  const taskX = await headingX("/admin/jobs?tab=downloads", "Jobs");
  const pages = [
    ["/admin/works", "Works"],
    ["/admin/tags", "Tags"],
    ["/admin/upload", "Upload"],
    ["/admin/reference/danbooru", "Danbooru Reference Mapping"],
    ["/admin/notifications", "Notifications"],
  ] as const;

  for (const [path, name] of pages) {
    const x = await headingX(path, name);
    expect(Math.abs(x - taskX), `${name} heading should align with Jobs`).toBeLessThanOrEqual(1);
  }

  await page.goto("/admin/works?creator=creator-atlas");
  await expect(page.getByRole("combobox", { name: "Filter creator" })).toHaveCount(0);
  await expectNoPageOverflow(page);
  await page.screenshot({ path: "/tmp/auto-gallery-page-alignment.png", fullPage: false });
});

test("compact tablet sidebar and long job metadata do not create root overflow", async ({ page }) => {
  await page.setViewportSize({ width: 768, height: 1024 });
  await page.goto("/admin/jobs?tab=downloads");
  await expect(page.locator("aside").first()).toHaveCSS("width", "64px");
  await expect(page.getByText("xianyuliangryo-with-a-very-long-creator-name")).toBeVisible();
  await expectNoPageOverflow(page);
  await page.screenshot({ path: "/tmp/auto-gallery-jobs-tablet.png", fullPage: true });
});

test("mobile drawer is discoverable, dismissible, and the task page stays in bounds", async ({ page }) => {
  await page.setViewportSize({ width: 320, height: 800 });
  await page.goto("/admin/jobs?tab=downloads");
  const trigger = page.locator("header button[aria-controls]").first();
  await expect(trigger).toHaveAttribute("aria-expanded", "false");
  await trigger.click();
  await expect(trigger).toHaveAttribute("aria-expanded", "true");
  await expect(page.locator("#admin-mobile-sidebar")).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(page.locator("#admin-mobile-sidebar")).toBeHidden();
  await expectNoPageOverflow(page);
  await page.screenshot({ path: "/tmp/auto-gallery-jobs-mobile.png", fullPage: true });
});

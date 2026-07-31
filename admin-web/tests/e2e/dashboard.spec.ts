import { expect, test, type BrowserContext, type Page, type Route } from "@playwright/test";

const ADMIN = {
  id: 1,
  username: "dashboard-review",
  display_name: "Dashboard Review",
  is_admin: true,
  is_active: true,
  permissions: [] as string[],
  modules: {},
  preferences: {},
  nsfw_visible: true,
  upload_quota_bytes: null,
  upload_used_bytes: 0,
  must_change_password: false,
};

const VIEWER = {
  ...ADMIN,
  is_admin: false,
  permissions: ["system"],
};

const WORKBENCH = {
  updated_at: "2026-07-28T08:30:00Z",
  queue: {
    default: 4,
    scheduled: 2,
    failed: 2,
    started: 2,
    active_download_count: 1,
    active_import_count: 1,
    failed_download_count: 1,
    failed_import_count: 1,
    stale_download_count: 0,
    stale_import_count: 0,
    stale_count: 0,
  },
  scheduler: {
    enabled: true,
    mode: "fixed_time",
    timezone: "Asia/Shanghai",
    scheduled_times: "02:00,14:00",
    scan_interval_minutes: 15,
    next_scan_at: "2026-07-28T14:00:00Z",
  },
  storage: {
    disk_total_bytes: 2_000_000_000_000,
    disk_free_bytes: 1_240_000_000_000,
    disk_used_bytes: 760_000_000_000,
    disk_used_percent: 38,
    disk_free_percent: 62,
    risk_level: "ok",
  },
  health: {
    backend: "up",
    postgres: "up",
    redis: "up",
    meilisearch: "up",
    "gallery-dl": "up",
  },
  attention: {
    auth_unhealthy_count: 0,
    failed_download_count: 1,
    failed_import_count: 1,
    stale_job_count: 0,
    low_disk_warning: false,
    scheduler_disabled_warning: false,
  },
  recent: {
    download_jobs: [
      {
        id: "download-failed",
        subscription_id: "subscription-1",
        source: "pixiv",
        source_url: "https://www.pixiv.net/users/10000001",
        creator_name: "Atlas Ink",
        subscription_name: "Atlas archive",
        status: "failed",
        pipeline_stage: "download",
        created_at: "2026-07-28T08:20:00Z",
        updated_at: "2026-07-28T08:24:00Z",
      },
      {
        id: "download-running",
        subscription_id: "subscription-2",
        source: "x",
        source_url: "https://x.com/northwind_studio",
        creator_name: "Northwind Studio",
        status: "downloading",
        pipeline_stage: "download",
        progress_data: { current: 48, total: 132, percent: 36.4 },
        created_at: "2026-07-28T08:25:00Z",
        updated_at: "2026-07-28T08:29:00Z",
      },
      {
        id: "download-no-changes",
        subscription_id: "subscription-3",
        subscription_source_id: "source-3",
        source: "pixiv",
        source_url: "https://www.pixiv.net/users/10000003",
        creator_name: "Quiet Archive",
        status: "complete",
        pipeline_stage: "post_download",
        progress_data: { stage: "post_download", percent: 90 },
        outcome: {
          code: "no_changes",
          metadata_count: 0,
          media_count: 0,
          completed_at: "2026-07-28T08:27:00Z",
        },
        created_at: "2026-07-28T08:26:00Z",
        updated_at: "2026-07-28T08:27:00Z",
      },
    ],
    import_jobs: [
      {
        id: "import-failed",
        download_job_id: "download-complete",
        source: "danbooru",
        creator_name: "Harbor Archive",
        status: "failed",
        progress_stage: "indexing",
        created_at: "2026-07-28T08:18:00Z",
        updated_at: "2026-07-28T08:21:00Z",
      },
    ],
    works: [
      {
        id: "work-harbor",
        title: "Harbor Light Study",
        thumbnail_asset_id: "asset-harbor",
        source: "pixiv",
        creator_name: "Atlas Ink",
        created_at: "2026-07-28T08:16:00Z",
      },
      {
        id: "work-summer",
        title: "Summer Color Notes",
        thumbnail_asset_id: "asset-summer",
        source: "x",
        creator_name: "Northwind Studio",
        created_at: "2026-07-28T08:18:00Z",
      },
      {
        id: "work-rain",
        title: "Rainy Evening Drive",
        thumbnail_asset_id: null,
        source: "danbooru",
        creator_name: "Harbor Archive",
        created_at: "2026-07-28T08:19:00Z",
      },
    ],
    successful_syncs: [
      {
        source_id: "source-1",
        subscription_id: "subscription-1",
        creator_id: "creator-atlas",
        creator_name: "Atlas Ink",
        source: "pixiv",
        last_synced_at: "2026-07-28T08:14:00Z",
      },
    ],
  },
};

const PIXEL = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=",
  "base64",
);

async function installDashboardRoutes(
  context: BrowserContext,
  options: {
    me?: typeof ADMIN;
    failRefresh?: boolean;
    initialDelayMs?: number;
    empty?: boolean;
    calls?: string[];
  } = {},
) {
  await context.addCookies([{
    name: "ag_token",
    value: "dashboard-test-token",
    domain: "127.0.0.1",
    path: "/",
  }]);
  await context.addInitScript(() => {
    window.localStorage.setItem("ag_token", "dashboard-test-token");
    window.localStorage.setItem("auto-gallery-lang", "en");
    window.localStorage.setItem("auto-gallery-theme", "dark");
  });
  await context.route("**/media/**", async (route) => {
    await route.fulfill({ body: PIXEL, contentType: "image/png" });
  });
  let workbenchRequests = 0;
  await context.route("**/api/v1/**", async (route: Route) => {
    const url = new URL(route.request().url());
    const path = url.pathname;
    options.calls?.push(`${route.request().method()} ${path}${url.search}`);
    if (path === "/api/v1/auth/me") {
      return route.fulfill({ json: options.me || ADMIN });
    }
    if (path === "/api/v1/system/workbench") {
      workbenchRequests += 1;
      if (url.searchParams.get("refresh") === "true" && options.failRefresh) {
        return route.fulfill({ status: 503, json: { detail: "Refresh unavailable" } });
      }
      if (workbenchRequests === 1 && options.initialDelayMs) {
        await new Promise((resolve) => setTimeout(resolve, options.initialDelayMs));
      }
      const payload = options.empty
        ? {
            ...WORKBENCH,
            queue: {
              ...WORKBENCH.queue,
              active_download_count: 0,
              active_import_count: 0,
              failed_download_count: 0,
              failed_import_count: 0,
              failed: 0,
            },
            attention: {
              ...WORKBENCH.attention,
              failed_download_count: 0,
              failed_import_count: 0,
            },
            recent: { download_jobs: [], import_jobs: [], works: [], successful_syncs: [] },
          }
        : WORKBENCH;
      return route.fulfill({ json: payload });
    }
    if (/^\/api\/v1\/download-jobs\/[^/]+\/retry$/.test(path)) {
      return route.fulfill({ json: { job_id: path.split("/")[4], status: "enqueued" } });
    }
    if (/^\/api\/v1\/import-jobs\/[^/]+\/retry$/.test(path)) {
      return route.fulfill({ json: { status: "enqueued", message: "Queued" } });
    }
    if (path === "/api/v1/download-jobs/retry-all") {
      return route.fulfill({ json: { status: "ok", succeeded: 1, failed: 0 } });
    }
    if (path.includes("/notifications")) {
      return route.fulfill({ json: { items: [], total: 0, unread_count: 0 } });
    }
    return route.fulfill({ status: 501, json: { detail: `Unhandled fixture route: ${path}` } });
  });
}

async function expectNoPageOverflow(page: Page) {
  await expect.poll(() => page.evaluate(() => {
    const root = document.documentElement;
    return root.scrollWidth <= root.clientWidth;
  })).toBe(true);
}

test("dashboard links, refresh, job navigation, and retry controls work", async ({ context, page }) => {
  const calls: string[] = [];
  await installDashboardRoutes(context, { calls });
  await page.setViewportSize({ width: 1440, height: 1024 });
  await page.goto("/admin");

  await expect(page.getByRole("heading", { name: "Recently added works" })).toBeVisible();
  await expect(page.getByTestId("dashboard-status-scheduler")).toHaveAttribute("href", "/admin/scheduler");
  await expect(page.getByTestId("dashboard-status-failed")).toHaveAttribute("href", "/admin/jobs?q=status%3Afailed");
  await expect(page.getByRole("link", { name: "Open Harbor Light Study" })).toHaveAttribute("href", "/admin/works/work-harbor");
  await expect(page.locator('a[href*="job=download-failed"]').first()).toHaveAttribute("href", /job=download-failed/);
  await expect(page.getByText("No new works", { exact: true })).toBeVisible();
  await expect(page.getByText("Sync completed; no new works were found to import.")).toBeVisible();

  await page.getByRole("button", { name: "Refresh" }).click();
  await expect.poll(() => calls.some((call) => call === "GET /api/v1/system/workbench?refresh=true")).toBe(true);
  await expect(page.getByText("Dashboard status refreshed.")).toBeVisible();

  await page.getByRole("button", { name: "Retry" }).first().click();
  await expect.poll(() => calls.some((call) => call === "POST /api/v1/download-jobs/download-failed/retry")).toBe(true);
  await expect(page.getByText("Retry job submitted.")).toBeVisible();

  await page.getByRole("button", { name: "Retry failed downloads" }).click();
  await expect.poll(() => calls.some((call) => call === "POST /api/v1/download-jobs/retry-all")).toBe(true);
  await expect(page.getByText("1 failed downloads queued for retry.")).toBeVisible();
});

test("refresh failure preserves the last dashboard and reports the error", async ({ context, page }) => {
  await installDashboardRoutes(context, { failRefresh: true });
  await page.goto("/admin");
  await expect(page.getByText("Harbor Light Study")).toBeVisible();
  await page.getByRole("button", { name: "Refresh" }).click();
  await expect(page.getByText("Dashboard refresh failed")).toBeVisible();
  await expect(page.getByText("Harbor Light Study")).toBeVisible();
});

test("viewer permissions hide single and bulk retry actions", async ({ context, page }) => {
  await installDashboardRoutes(context, { me: VIEWER });
  await page.goto("/admin");
  await expect(page.getByRole("heading", { name: "Live activity" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Retry" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Retry failed downloads" })).toHaveCount(0);
  await expect(page.getByRole("link", { name: "View details" })).toBeVisible();
});

test("loading and empty dashboard states remain useful", async ({ context, page }) => {
  await installDashboardRoutes(context, { initialDelayMs: 500, empty: true });
  await page.goto("/admin");
  await expect(page.locator("[aria-hidden='true']").first()).toBeVisible();
  await expect(page.getByText("No recent works yet")).toBeVisible();
  await expect(page.getByText("No recent job activity.")).toBeVisible();
  await expect(page.getByText("Attention required")).toHaveCount(0);
});

for (const viewport of [
  { name: "tablet", width: 768, height: 1024 },
  { name: "mobile", width: 390, height: 844 },
]) {
  test(`${viewport.name} dashboard has no page overflow and supports keyboard navigation`, async ({ context, page }) => {
    await installDashboardRoutes(context);
    await page.emulateMedia({ reducedMotion: "reduce" });
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    await page.goto("/admin");
    await expect(page.getByRole("heading", { name: "Recently added works" })).toBeVisible();
    await expectNoPageOverflow(page);

    const scheduler = page.getByTestId("dashboard-status-scheduler");
    await scheduler.focus();
    await expect(scheduler).toBeFocused();
    await expect(scheduler).toHaveCSS("min-height", viewport.width === 390 ? "108px" : "112px");

    const firstWork = page.getByRole("link", { name: "Open Harbor Light Study" });
    await firstWork.focus();
    await expect(firstWork).toBeFocused();
  });
}

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
        available_actions: ["retry", "repeat_sync"],
        disabled_reasons: {},
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
        available_actions: ["retry"],
        disabled_reasons: {},
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
  await context.routeWebSocket("**/api/v1/ws*", (webSocket) => {
    webSocket.send(JSON.stringify({ type: "connected" }));
  });
  await context.addCookies([{
    name: "ag_session",
    value: "dashboard-test-token",
    domain: new URL(process.env.PLAYWRIGHT_BASE_URL || "http://127.0.0.1:13000").hostname,
    path: "/",
  }, { name: "ag_csrf", value: "fixture-csrf", url: process.env.PLAYWRIGHT_BASE_URL || "http://127.0.0.1:13000" }]);
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

test("expired browser session stays on login without an automatic logout request", async ({ context, page }) => {
  const origin = process.env.PLAYWRIGHT_BASE_URL || "http://127.0.0.1:13000";
  await context.addCookies([
    { name: "ag_session", value: "expired-session", url: origin },
    { name: "ag_csrf", value: "expired-csrf", url: origin },
  ]);
  let logoutCalls = 0;
  await context.route("**/api/v1/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/api/v1/auth/me") return route.fulfill({ status: 401, json: { detail: "Expired" } });
    if (path === "/api/v1/auth/browser/logout") {
      logoutCalls += 1;
      return route.fulfill({ json: { ok: true } });
    }
    return route.fulfill({ status: 501, json: { detail: "Unhandled fixture" } });
  });
  await page.goto("/admin");
  await expect(page).toHaveURL(/\/admin\/login/);
  expect((await context.cookies()).some((cookie) => cookie.name === "ag_session")).toBe(true);
  expect(logoutCalls).toBe(0);
});

test("session store outage keeps the cookie and displays a retryable error", async ({ context, page }) => {
  const origin = process.env.PLAYWRIGHT_BASE_URL || "http://127.0.0.1:13000";
  await context.addCookies([{ name: "ag_session", value: "still-present", url: origin }]);
  await context.addInitScript(() => localStorage.setItem("auto-gallery-lang", "en"));
  await context.route("**/api/v1/auth/me", (route) => route.fulfill({
    status: 503, json: { detail: "Browser session service unavailable" },
  }));
  await page.goto("/admin");
  await expect(page).toHaveURL(/\/admin$/);
  await expect(page.getByRole("alert").filter({ hasText: "Sign-in service is temporarily unavailable" })).toBeVisible();
  expect((await context.cookies()).some((cookie) => cookie.name === "ag_session")).toBe(true);
});

test("root route redirects authenticated users to the dashboard", async ({ context, page }) => {
  await installDashboardRoutes(context);
  await page.goto("/");
  await expect(page).toHaveURL(/\/admin$/);
  await expect(page.getByRole("heading", { name: "System Dashboard", exact: true })).toBeVisible();
});

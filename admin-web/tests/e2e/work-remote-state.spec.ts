import { expect, test, type BrowserContext, type Route } from "@playwright/test";

const host = new URL(process.env.PLAYWRIGHT_BASE_URL || "http://127.0.0.1:13000").hostname;
const workId = "work-pixiv-live";

const work = {
  id: workId,
  title: "Pixiv live fixture",
  description: "Local work details remain available while Pixiv state loads.",
  posted_at: "2026-08-30T00:00:00Z",
  is_nsfw: false,
  is_ai_generated: false,
  thumbnail_asset_id: "asset-pixiv-live",
  asset_count: 1,
  is_favorite: false,
  creator_id: null,
  creator_name: null,
  curation_state: { visibility: "visible" },
  created_at: "2026-08-30T00:00:00Z",
  updated_at: "2026-08-30T00:00:00Z",
};

const asset = {
  id: "asset-pixiv-live",
  file_name: "pixiv-live-fixture.png",
  file_path: "fixture/pixiv-live-fixture.png",
  file_size: 68,
  width: 1,
  height: 1,
  duration: null,
  mime_type: "image/png",
  media_kind: "image",
  thumb_url: "/media/thumb/asset-pixiv-live",
  preview_url: "/media/preview/asset-pixiv-live",
  original_url: "/media/original/asset-pixiv-live",
  created_at: "2026-08-30T00:00:00Z",
};

const pixel = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9WlBCe0AAAAASUVORK5CYII=",
  "base64",
);

type RemoteState = {
  source: "pixiv";
  source_work_id: string;
  fetched_at: string;
  total_views: number;
  total_bookmarks: number;
  is_bookmarked: boolean;
};

type WorkFixtureOptions = {
  rawMetadata?: Record<string, unknown>;
  remoteState?: RemoteState;
  remoteStatus?: number;
  remoteDetail?: string;
  pendingRemoteState?: boolean;
  onRemoteState?: () => void;
};

async function json(route: Route, value: unknown, status = 200) {
  await route.fulfill({ status, contentType: "application/json", body: JSON.stringify(value) });
}

async function installWorkFixtures(context: BrowserContext, options: WorkFixtureOptions = {}) {
  await context.addCookies([{ name: "ag_session", value: "pixiv-live-test-token", domain: host, path: "/" }, { name: "ag_csrf", value: "fixture-csrf", url: process.env.PLAYWRIGHT_BASE_URL || "http://127.0.0.1:13000" }]);
  await context.addInitScript(() => {
    localStorage.setItem("ag_token", "pixiv-live-test-token");
    localStorage.setItem("auto-gallery-lang", "en");
    localStorage.setItem("auto-gallery-theme", "light");
  });
  await context.route("**/media/**", (route) => route.fulfill({ body: pixel, contentType: "image/png" }));
  await context.route("**/api/v1/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/api/v1/auth/me") return json(route, {
      id: 1, username: "pixiv-live", display_name: "Pixiv Live", is_admin: true, is_active: true,
      permissions: ["curation"], modules: {}, preferences: {}, nsfw_visible: true,
      upload_quota_bytes: null, upload_used_bytes: 0, must_change_password: false,
    });
    if (path === "/api/v1/system/workbench") return json(route, {
      updated_at: "2026-08-30T00:00:00Z",
      queue: { default: 0, scheduled: 0, failed: 0, active_download_count: 0, active_import_count: 0, failed_download_count: 0, failed_import_count: 0, stale_download_count: 0, stale_import_count: 0, stale_count: 0 },
      scheduler: { enabled: true, mode: "interval", timezone: "Asia/Shanghai", scan_interval_minutes: 15 },
      storage: { disk_total_bytes: 1_000_000, disk_free_bytes: 500_000, disk_used_bytes: 500_000, disk_used_percent: 50, disk_free_percent: 50, risk_level: "ok" },
      health: {},
      attention: { auth_unhealthy_count: 0, failed_download_count: 0, failed_import_count: 0, stale_job_count: 0, low_disk_warning: false, scheduler_disabled_warning: false },
      recent: { download_jobs: [], import_jobs: [], works: [], successful_syncs: [] },
    });
    if (path === `/api/v1/works/${workId}`) return json(route, work);
    if (path === `/api/v1/works/${workId}/assets`) return json(route, [asset]);
    if (path === `/api/v1/works/${workId}/sources`) return json(route, [{
      id: "source-pixiv-live", source: "pixiv", source_work_id: "38362603", source_creator_id: "12345",
      source_url: "https://www.pixiv.net/artworks/38362603", raw_metadata: options.rawMetadata || {},
    }]);
    if (path === `/api/v1/works/${workId}/tags`) return json(route, []);
    if (path === `/api/v1/works/${workId}/remote-state`) {
      options.onRemoteState?.();
      if (options.pendingRemoteState) return new Promise<void>(() => {});
      if (options.remoteStatus) return json(route, { detail: options.remoteDetail || "remote_state_unavailable" }, options.remoteStatus);
      return json(route, options.remoteState || {
        source: "pixiv", source_work_id: "38362603", fetched_at: "2026-08-30T00:00:00Z",
        total_views: 1, total_bookmarks: 1, is_bookmarked: false,
      });
    }
    if (path === "/api/v1/curation/commits") return json(route, { items: [], total: 0 });
    if (path === "/api/v1/operations/overview") return json(route, {
      view: "attention", total: 0,
      summary: { attention: 0, critical: 0, warning: 0, resolved: 0, active: 0, resource_limited: 0 },
      items: [],
    });
    if (path === "/api/v1/sources") return json(route, { sources: [] });
    if (path.includes("/notifications")) return json(route, { items: [], total: 0, unread_count: 0 });
    return json(route, {});
  });
}

test("work detail renders live Pixiv state and never renders stale metadata stats", async ({ page }) => {
  let remoteCalls = 0;
  const pageErrors: string[] = [];
  page.on("pageerror", (error) => pageErrors.push(error.message));
  await installWorkFixtures(page.context(), {
    rawMetadata: {
      total_view: 110011,
      total_bookmarks: 120012,
      is_bookmarked: false,
      raw_caption: "preserved root metadata",
      nested: {
        total_view: 130013,
        total_views: 140014,
        total_bookmarks: 150015,
        is_bookmarked: true,
        favorite_context: "preserved nested metadata",
      },
      pages: [{
        total_view: 160016,
        total_bookmarks: 170017,
        is_bookmarked: true,
        page_label: "preserved array metadata",
      }],
    },
    remoteState: {
      source: "pixiv", source_work_id: "38362603", fetched_at: "2026-08-30T00:00:00Z",
      total_views: 987654, total_bookmarks: 4321, is_bookmarked: true,
    },
    onRemoteState: () => { remoteCalls += 1; },
  });

  await page.goto(`/admin/works/${workId}`);
  await expect(page.getByText("987,654")).toBeVisible();
  await expect(page.getByText("4,321")).toBeVisible();
  await expect(page.getByText("Pixiv bookmarked")).toBeVisible();
  await page.getByRole("button", { name: /Source Records/ }).click();
  await page.getByRole("button", { name: "Show raw metadata" }).click();
  const rawMetadata = page.locator("pre");
  await expect(rawMetadata).toContainText('"raw_caption": "preserved root metadata"');
  await expect(rawMetadata).toContainText('"favorite_context": "preserved nested metadata"');
  await expect(rawMetadata).toContainText('"page_label": "preserved array metadata"');
  for (const forbiddenKey of ["total_view", "total_views", "total_bookmarks", "is_bookmarked"]) {
    await expect(rawMetadata).not.toContainText(`"${forbiddenKey}"`);
  }
  for (const forbiddenValue of ["110011", "120012", "130013", "140014", "150015", "160016", "170017"]) {
    await expect(rawMetadata).not.toContainText(forbiddenValue);
  }
  await expect(page.getByRole("button", { name: "Local library favorite" })).toBeVisible();
  await expect.poll(() => remoteCalls).toBe(1);
  await expect(page.getByText("Something went wrong")).toHaveCount(0);
  expect(pageErrors).toEqual([]);
  await page.screenshot({ path: "/tmp/auto-gallery-pixiv-live-desktop.png", fullPage: false });
});

test("live Pixiv state refetches after remount but not window focus", async ({ page }) => {
  let remoteCalls = 0;
  await installWorkFixtures(page.context(), { onRemoteState: () => { remoteCalls += 1; } });

  await page.goto(`/admin/works/${workId}`);
  await expect(page.getByText("Pixiv not bookmarked")).toBeVisible();
  await expect.poll(() => remoteCalls).toBe(1);
  await page.goto("/admin/works");
  await page.goBack();
  await expect(page.getByText("Pixiv not bookmarked")).toBeVisible();
  await expect.poll(() => remoteCalls).toBe(2);
  await page.evaluate(() => window.dispatchEvent(new Event("focus")));
  await page.waitForTimeout(200);
  expect(remoteCalls).toBe(2);
});

for (const errorCase of [
  { status: 409, detail: "remote_account_required", hint: "Connect a Pixiv account" },
  { status: 409, detail: "remote_account_reauthentication_required", hint: "Reconnect your Pixiv account" },
  { status: 429, detail: "remote_rate_limited", hint: "Pixiv is rate limited" },
  { status: 502, detail: "remote_account_unhealthy", hint: "Pixiv live state is unavailable" },
]) {
  test(`remote state ${errorCase.detail} keeps local work visible`, async ({ page }) => {
    await installWorkFixtures(page.context(), { remoteStatus: errorCase.status, remoteDetail: errorCase.detail });

    await page.goto(`/admin/works/${workId}`);
    await expect(page.getByText(errorCase.hint)).toBeVisible();
    await expect(page.getByRole("heading", { level: 1, name: "Pixiv live fixture" })).toBeVisible();
    await expect(page.getByTitle("pixiv-live-fixture.png")).toBeVisible();
    await expect(page.getByRole("button", { name: "Local library favorite" })).toBeVisible();
  });
}

test("pending live state uses a skeleton without blocking the work", async ({ page }) => {
  await installWorkFixtures(page.context(), { pendingRemoteState: true });

  await page.goto(`/admin/works/${workId}`);
  await expect(page.getByLabel("Loading Pixiv live state")).toBeVisible();
  await expect(page.getByRole("heading", { level: 1, name: "Pixiv live fixture" })).toBeVisible();
  await expect(page.getByTitle("pixiv-live-fixture.png")).toBeVisible();
});

test("false remote bookmark stays distinct from the local favorite", async ({ page }) => {
  await installWorkFixtures(page.context(), {
    remoteState: {
      source: "pixiv", source_work_id: "38362603", fetched_at: "2026-08-30T00:00:00Z",
      total_views: 10, total_bookmarks: 20, is_bookmarked: false,
    },
  });

  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto(`/admin/works/${workId}`);
  await expect(page.getByText("Pixiv not bookmarked")).toBeVisible();
  await expect(page.getByRole("button", { name: "Local library favorite" })).toBeVisible();
  await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth)).toBe(true);
  await expect(page.getByText("Something went wrong")).toHaveCount(0);
  await page.getByText("Pixiv not bookmarked").scrollIntoViewIfNeeded();
  await page.screenshot({ path: "/tmp/auto-gallery-pixiv-live-mobile.png", fullPage: false });
});

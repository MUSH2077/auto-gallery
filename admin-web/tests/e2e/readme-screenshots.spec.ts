import fs from "node:fs/promises";
import path from "node:path";
import { expect, test, type BrowserContext, type Page, type Route } from "@playwright/test";

test.skip(!process.env.UPDATE_README_SCREENSHOTS, "README screenshots are generated only by npm run screenshots:readme");

const PROJECT_ROOT = path.resolve(__dirname, "../../..");
const OUTPUT_ROOT = path.join(PROJECT_ROOT, "docs/assets");
const FIXTURE_ROOT = path.join(OUTPUT_ROOT, "fixtures");
const FIXED_TIME = new Date("2026-07-28T08:30:00Z");

const MEDIA = {
  "asset-harbor": "harbor-light-study.webp",
  "asset-summer": "summer-color-notes.webp",
  "asset-rain": "rainy-evening-drive.webp",
  "asset-dedup-left": "harbor-light-study.webp",
  "asset-dedup-right": "harbor-light-study.webp",
} as const;

const me = {
  id: 1,
  username: "readme-review",
  display_name: "README Review",
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

const works = [
  {
    id: "work-harbor",
    title: "Harbor Light Study",
    posted_at: "2026-07-27T12:00:00Z",
    is_nsfw: false,
    is_ai_generated: false,
    thumbnail_asset_id: "asset-harbor",
    preview_asset_ids: ["asset-harbor"],
    asset_count: 3,
    created_at: "2026-07-28T08:16:00Z",
    source: "pixiv",
    creator_name: "Atlas Ink",
    creator_id: "creator-atlas",
    is_favorite: true,
    curation_visibility: "visible",
  },
  {
    id: "work-summer",
    title: "Summer Color Notes",
    posted_at: "2026-07-26T09:30:00Z",
    is_nsfw: false,
    is_ai_generated: false,
    thumbnail_asset_id: "asset-summer",
    preview_asset_ids: ["asset-summer"],
    asset_count: 5,
    created_at: "2026-07-28T08:18:00Z",
    source: "x",
    creator_name: "Northwind Studio",
    creator_id: "creator-northwind",
    is_favorite: false,
    curation_visibility: "visible",
  },
  {
    id: "work-rain",
    title: "Rainy Evening Drive",
    posted_at: "2026-07-25T18:45:00Z",
    is_nsfw: false,
    is_ai_generated: false,
    thumbnail_asset_id: "asset-rain",
    preview_asset_ids: ["asset-rain"],
    asset_count: 1,
    created_at: "2026-07-28T08:19:00Z",
    source: "danbooru",
    creator_name: "Harbor Archive",
    creator_id: "creator-harbor",
    is_favorite: false,
    curation_visibility: "visible",
  },
];

const creators = [
  {
    id: "creator-atlas",
    name: "atlas_ink",
    display_name: "Atlas Ink",
    description: "Environmental studies, quiet coasts, and light-driven color scripts.",
    is_active: true,
    is_favorite: true,
    subscription_count: 1,
    source_count: 2,
    repository_count: 2,
    last_synced_at: "2026-07-28T08:14:00Z",
    created_at: "2026-06-01T00:00:00Z",
    updated_at: "2026-07-28T08:14:00Z",
  },
  {
    id: "creator-northwind",
    name: "northwind_studio",
    display_name: "Northwind Studio",
    description: "Color studies and playful editorial illustration.",
    is_active: true,
    is_favorite: false,
    subscription_count: 1,
    source_count: 1,
    repository_count: 1,
    last_synced_at: "2026-07-28T08:10:00Z",
    created_at: "2026-06-04T00:00:00Z",
    updated_at: "2026-07-28T08:10:00Z",
  },
  {
    id: "creator-harbor",
    name: "harbor_archive",
    display_name: "Harbor Archive",
    description: "A curated index of cinematic streets and travel concepts.",
    is_active: true,
    is_favorite: false,
    subscription_count: 1,
    source_count: 1,
    repository_count: 1,
    last_synced_at: "2026-07-28T07:45:00Z",
    created_at: "2026-06-12T00:00:00Z",
    updated_at: "2026-07-28T07:45:00Z",
  },
];

const downloadJobs = [
  {
    id: "download-running",
    subscription_id: "subscription-atlas",
    subscription_source_id: "repo-atlas-pixiv",
    creator_id: "creator-atlas",
    creator_name: "Atlas Ink",
    subscription_name: "Atlas archive",
    source: "pixiv",
    source_url: "https://www.pixiv.net/users/10000001",
    status: "downloading",
    retry_count: 0,
    pipeline_stage: "download",
    progress_data: { stage: "download", current: 48, total: 132, percent: 36.4 },
    created_at: "2026-07-28T08:25:00Z",
    updated_at: "2026-07-28T08:29:00Z",
  },
  {
    id: "download-complete",
    subscription_id: "subscription-northwind",
    subscription_source_id: "repo-northwind-x",
    creator_id: "creator-northwind",
    creator_name: "Northwind Studio",
    subscription_name: "Northwind updates",
    source: "x",
    source_url: "https://x.com/northwind_studio",
    status: "complete",
    retry_count: 0,
    pipeline_stage: "complete",
    progress_data: { stage: "complete", current: 96, total: 96, percent: 100 },
    created_at: "2026-07-28T08:05:00Z",
    updated_at: "2026-07-28T08:12:00Z",
  },
  {
    id: "download-failed",
    subscription_id: "subscription-harbor",
    subscription_source_id: "repo-harbor-danbooru",
    creator_id: "creator-harbor",
    creator_name: "Harbor Archive",
    subscription_name: "Harbor archive",
    source: "danbooru",
    source_url: "https://danbooru.donmai.us/posts?tags=harbor_archive",
    status: "failed",
    retry_count: 1,
    pipeline_stage: "download",
    error_log: "Remote source temporarily unavailable; retry is safe.",
    created_at: "2026-07-28T07:50:00Z",
    updated_at: "2026-07-28T07:55:00Z",
  },
];

const importJobs = [
  {
    id: "import-running",
    download_job_id: "download-running",
    status: "running",
    priority: 0,
    import_retry_count: 0,
    max_import_retries: 3,
    progress_stage: "works",
    progress_works_done: 8,
    progress_works_total: 12,
    progress_data: { stage: "works", current: 8, total: 12, percent: 66.7 },
    source: "pixiv",
    creator_name: "Atlas Ink",
    subscription_name: "Atlas archive",
    created_at: "2026-07-28T08:20:00Z",
    updated_at: "2026-07-28T08:29:00Z",
  },
];

const workbench = {
  updated_at: "2026-07-28T08:30:00Z",
  queue: {
    default: 4,
    scheduled: 2,
    failed: 1,
    started: 2,
    active_download_count: 1,
    active_import_count: 1,
    failed_download_count: 1,
    failed_import_count: 0,
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
    failed_import_count: 0,
    stale_job_count: 0,
    low_disk_warning: false,
    scheduler_disabled_warning: false,
  },
  recent: {
    download_jobs: downloadJobs.slice(0, 3),
    import_jobs: importJobs,
    works,
    successful_syncs: [
      {
        source_id: "repo-atlas-pixiv",
        subscription_id: "subscription-atlas",
        creator_id: "creator-atlas",
        creator_name: "Atlas Ink",
        source: "pixiv",
        source_url: "https://www.pixiv.net/users/10000001",
        last_synced_at: "2026-07-28T08:14:00Z",
      },
    ],
  },
};

const tags = [
  ["landscape", "general", 482],
  ["original", "general", 364],
  ["portrait", "general", 248],
  ["color_script", "meta", 196],
  ["summer", "general", 152],
  ["illustration", "general", 131],
  ["city_lights", "general", 107],
  ["travel_notes", "general", 89],
  ["night", "general", 72],
  ["concept_art", "general", 58],
  ["watercolor", "meta", 41],
  ["sketch", "meta", 27],
].map(([normalized_name, category, usage_count], index) => ({
  id: `tag-${index + 1}`,
  normalized_name,
  category,
  usage_count,
  created_at: "2026-07-01T00:00:00Z",
}));

const repository = {
  repository: {
    id: "repo-atlas-pixiv",
    subscription_id: "subscription-atlas",
    source: "pixiv",
    source_display_name: "Pixiv",
    source_creator_id: "10000001",
    source_url: "https://www.pixiv.net/users/10000001",
    is_enabled: true,
    auth_healthy: true,
    last_synced_at: "2026-07-28T08:14:00Z",
    last_attempted_at: "2026-07-28T08:14:00Z",
    auth_status: "healthy",
    last_auth_checked_at: "2026-07-28T08:12:00Z",
    can_download: true,
    supports_gallerydl: true,
    url_valid: true,
    is_repository: true,
    latest_job: {
      id: "download-running",
      status: "complete",
      created_at: "2026-07-28T08:05:00Z",
      updated_at: "2026-07-28T08:14:00Z",
    },
    created_at: "2026-06-01T00:00:00Z",
    updated_at: "2026-07-28T08:14:00Z",
  },
  creator: {
    id: "creator-atlas",
    name: "atlas_ink",
    display_name: "Atlas Ink",
    is_favorite: true,
  },
  subscription: {
    id: "subscription-atlas",
    name: "Atlas archive",
    is_active: true,
    sync_enabled: true,
    sync_interval_hours: 12,
    schedule_mode: "fixed_time",
    scheduled_times: "02:00,14:00",
    last_synced_at: "2026-07-28T08:14:00Z",
  },
  provider: {
    source: "pixiv",
    display_name: "Pixiv",
    normalized_url: "https://www.pixiv.net/users/10000001",
    url_valid: true,
    capabilities: {
      can_download: true,
      can_import_local: true,
      supports_gallerydl: true,
      supports_tags: true,
      is_reference_only: false,
    },
  },
  recent_jobs: downloadJobs.slice(0, 2),
  recent_works: works,
};

const schedulerDecisions = {
  updated_at: "2026-07-28T08:30:00Z",
  scheduler_enabled: true,
  timezone: "Asia/Shanghai",
  items: [
    {
      source_id: "repo-atlas-pixiv",
      subscription_id: "subscription-atlas",
      subscription_name: "Atlas archive",
      subscription_active: true,
      subscription_sync_enabled: true,
      creator_id: "creator-atlas",
      creator_name: "Atlas Ink",
      source: "pixiv",
      source_display_name: "Pixiv",
      source_url: "https://www.pixiv.net/users/10000001",
      source_enabled: true,
      effective_mode: "fixed_time",
      timezone: "Asia/Shanghai",
      scheduled_times: "02:00,14:00",
      sync_interval_hours: 12,
      last_synced_at: "2026-07-28T08:14:00Z",
      due: false,
      decision: "outside_window",
      reason: "outside_window",
      next_due_at: "2026-07-28T14:00:00Z",
      auth_healthy: true,
      url_valid: true,
      can_download: true,
    },
  ],
};

const taskRuns = [
  {
    id: "task-download-running",
    kind: "download",
    operation_type: "download",
    subject_id: "download-running",
    status: "running",
    queue_name: "downloads:pixiv",
    title: "Download Atlas archive",
    source: "pixiv",
    progress_stage: "download",
    progress_current: 48,
    progress_total: 132,
    created_at: "2026-07-28T08:25:00Z",
    updated_at: "2026-07-28T08:29:00Z",
  },
  {
    id: "task-import-running",
    kind: "import",
    operation_type: "import",
    subject_id: "import-running",
    status: "running",
    queue_name: "imports",
    title: "Import Atlas archive",
    source: "pixiv",
    progress_stage: "works",
    progress_current: 8,
    progress_total: 12,
    created_at: "2026-07-28T08:20:00Z",
    updated_at: "2026-07-28T08:29:00Z",
  },
];

const dedupCase = {
  id: "dedup-case-1",
  status: "pending",
  revision: 3,
  left: {
    id: "asset-dedup-left",
    file_name: "harbor-light-master.webp",
    file_size: 8_420_000,
    mime_type: "image/webp",
    width: 3200,
    height: 2400,
    sha256: "fixture-left",
    phash: "a1b2c3d4",
    source: "pixiv",
    source_work_id: "100000001",
    source_url: "https://www.pixiv.net/artworks/100000001",
    work_id: "work-harbor-pixiv",
    work_title: "Harbor Light Study",
    creator_id: "creator-atlas",
    creator_name: "Atlas Ink",
    posted_at: "2026-07-02T12:00:00Z",
    thumb_url: "/media/thumb/asset-dedup-left",
    preview_url: "/media/preview/asset-dedup-left",
    group_id: null,
    is_representative: false,
  },
  right: {
    id: "asset-dedup-right",
    file_name: "harbor-light-social.webp",
    file_size: 2_180_000,
    mime_type: "image/webp",
    width: 2048,
    height: 1536,
    sha256: "fixture-right",
    phash: "a1b2c3e4",
    source: "x",
    source_work_id: "190000000000000001",
    source_url: "https://x.com/atlas_ink/status/190000000000000001",
    work_id: "work-harbor-x",
    work_title: "Harbor Light Study",
    creator_id: "creator-atlas",
    creator_name: "Atlas Ink",
    posted_at: "2026-07-03T09:30:00Z",
    thumb_url: "/media/thumb/asset-dedup-right",
    preview_url: "/media/preview/asset-dedup-right",
    group_id: null,
    is_representative: false,
  },
  evidence: {
    id: "evidence-1",
    algorithm_version: "asset-dedup-v1",
    sha256_equal: false,
    phash_distance: 1,
    ssim_score: 0.9924,
    aspect_ratio_delta: 0,
    visual_score: 94,
    metadata_score: 10,
    total_score: 104,
    hard_gate_passed: true,
    facts: {
      metadata: {
        same_canonical_creator: true,
        min_posted_delta_hours: 21.5,
        creator_bonus: 6,
        time_bonus: 4,
        left_sources: ["pixiv"],
        right_sources: ["x"],
      },
      scope: { eligible: true, reason: "different_source_and_work" },
    },
  },
  suggested_representative_asset_id: "asset-dedup-left",
  created_at: "2026-07-28T08:20:00Z",
  decided_at: null,
  decided_by: null,
  decision_reason: null,
};

async function installRoutes(context: BrowserContext, unknownRequests: string[]) {
  await context.addCookies([{
    name: "ag_token",
    value: "readme-fixture-token",
    domain: "127.0.0.1",
    path: "/",
  }]);
  await context.addInitScript(() => {
    window.localStorage.setItem("ag_token", "readme-fixture-token");
    window.localStorage.setItem("auto-gallery-lang", "en");
    window.localStorage.setItem("auto-gallery-theme", "dark");
  });

  await context.route("**/media/**", async (route) => {
    const assetId = new URL(route.request().url()).pathname.split("/").at(-1) || "";
    const fixtureName = MEDIA[assetId as keyof typeof MEDIA];
    if (!fixtureName) {
      unknownRequests.push(`MEDIA ${assetId}`);
      return route.fulfill({ status: 404, body: "Unknown media fixture" });
    }
    const body = await fs.readFile(path.join(FIXTURE_ROOT, fixtureName));
    return route.fulfill({ body, contentType: "image/webp" });
  });

  await context.route("**/api/v1/**", async (route: Route) => {
    const request = route.request();
    const url = new URL(request.url());
    const routeKey = `${request.method()} ${url.pathname}`;

    if (url.pathname === "/api/v1/auth/me") return route.fulfill({ json: me });
    if (url.pathname === "/api/v1/auth/ws-ticket") {
      return route.fulfill({
        json: {
          ticket: "readme-fixture-ws-ticket",
          expires_at: "2026-07-28T08:35:00Z",
        },
      });
    }
    if (url.pathname === "/api/v1/system/workbench") return route.fulfill({ json: workbench });
    if (url.pathname === "/api/v1/system/scheduler-decisions") return route.fulfill({ json: schedulerDecisions });
    if (url.pathname === "/api/v1/search/assist") {
      const body = request.postDataJSON() as { before_cursor?: string; after_cursor?: string; scope?: string };
      const query = `${body.before_cursor || ""}${body.after_cursor || ""}`.trim();
      return route.fulfill({
        json: {
          query,
          canonical_query: query,
          parsed: {
            raw: query,
            canonical: query,
            scope: body.scope || "global",
            targets: [],
            tokens: [],
          },
          diagnostics: [],
          suggestions: [],
          catalog: [],
        },
      });
    }
    if (url.pathname === "/api/v1/search") {
      const scope = url.searchParams.get("scope") || "global";
      const byScope: Record<string, unknown[]> = {
        works,
        creators,
        tags,
        repositories: [repository.repository],
        subscriptions: [],
        tasks: taskRuns,
        scheduler: schedulerDecisions.items,
        "creator-picker": creators,
      };
      const target = scope === "creator-picker" ? "creators" : scope;
      const groups = scope === "global"
        ? {
            works: { total: works.length, items: works },
            creators: { total: creators.length, items: creators },
            tags: { total: tags.length, items: tags },
            repositories: { total: 1, items: [repository.repository] },
            subscriptions: { total: 0, items: [] },
          }
        : { [target]: { total: byScope[scope]?.length || 0, items: byScope[scope] || [] } };
      const query = url.searchParams.get("q") || "";
      return route.fulfill({
        json: {
          query,
          canonical_query: query,
          parsed: {
            raw: query,
            canonical: query,
            scope,
            targets: Object.keys(groups),
            tokens: [],
          },
          groups,
          total: Object.values(groups).reduce((sum, group) => sum + group.total, 0),
          results: [],
          creators,
          tags,
          repositories: [repository.repository],
          subscriptions: [],
        },
      });
    }
    if (url.pathname === "/api/v1/works") return route.fulfill({ json: { total: works.length, items: works } });
    if (url.pathname === "/api/v1/creators/count") return route.fulfill({ json: { count: creators.length } });
    if (url.pathname === "/api/v1/creators") return route.fulfill({ json: { total: creators.length, items: creators } });
    if (url.pathname === "/api/v1/repositories/repo-atlas-pixiv") return route.fulfill({ json: repository });
    if (url.pathname === "/api/v1/repositories/repo-atlas-pixiv/tags") {
      return route.fulfill({ json: { total: tags.length, items: tags } });
    }
    if (url.pathname === "/api/v1/tags") return route.fulfill({ json: tags });
    if (url.pathname === "/api/v1/admin/dedup/cases") {
      return route.fulfill({ json: { items: [dedupCase], total: 1, offset: 0, limit: 25 } });
    }
    if (url.pathname === "/api/v1/download-jobs") return route.fulfill({ json: downloadJobs });
    if (url.pathname === "/api/v1/import-jobs") {
      return route.fulfill({ json: { total: importJobs.length, items: importJobs } });
    }
    if (url.pathname === "/api/v1/tasks") {
      return route.fulfill({
        json: {
          total: taskRuns.length,
          items: taskRuns,
        },
      });
    }
    if (url.pathname.includes("/notifications")) {
      return route.fulfill({ json: { items: [], total: 0, unread_count: 0 } });
    }

    unknownRequests.push(routeKey + url.search);
    return route.fulfill({
      status: 501,
      json: { detail: `README fixture does not permit ${routeKey}${url.search}` },
    });
  });
}

async function preparePage(page: Page) {
  await page.clock.install({ time: FIXED_TIME });
  await page.emulateMedia({ colorScheme: "dark", reducedMotion: "reduce" });
  await page.setViewportSize({ width: 1440, height: 1024 });
}

async function capture(
  page: Page,
  unknownRequests: string[],
  route: string,
  filename: string,
  ready: () => Promise<unknown>,
  fullPage = false,
) {
  unknownRequests.length = 0;
  await page.goto(route);
  await ready();
  await page.evaluate(() => document.fonts.ready);
  await expect(page.locator("[data-nextjs-dialog-overlay]")).toHaveCount(0);
  await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth)).toBe(true);
  await expect.poll(() => unknownRequests).toEqual([]);
  await page.screenshot({
    path: path.join(OUTPUT_ROOT, filename),
    fullPage,
    animations: "disabled",
  });
}

test("generate sanitized README screenshots from strict fictional fixtures", async ({ context, page }) => {
  test.setTimeout(90_000);
  const unknownRequests: string[] = [];
  const consoleErrors: string[] = [];
  const pageErrors: string[] = [];
  await installRoutes(context, unknownRequests);
  await preparePage(page);
  page.on("console", (message) => {
    if (message.type() === "error") consoleErrors.push(message.text());
  });
  page.on("pageerror", (error) => pageErrors.push(error.message));

  await capture(page, unknownRequests, "/admin", "admin-dashboard.png", async () => {
    await expect(page.getByRole("heading", { name: "Recently added works" })).toBeVisible();
    await expect(page.getByText("Harbor Light Study")).toBeVisible();
  }, true);
  await capture(page, unknownRequests, "/admin/works", "works-library.png", async () => {
    await expect(page.getByRole("heading", { level: 1, name: "Works" })).toBeVisible();
    await expect(page.getByText("Summer Color Notes")).toBeVisible();
  });
  await capture(page, unknownRequests, "/admin/creators", "creators.png", async () => {
    await expect(page.getByRole("heading", { level: 1, name: "Creators" })).toBeVisible();
    await expect(page.getByText("Northwind Studio")).toBeVisible();
  });
  await capture(page, unknownRequests, "/admin/subscriptions/repositories/repo-atlas-pixiv", "repository-detail.png", async () => {
    await expect(page.getByRole("heading", { name: /pixiv\/10000001/i })).toBeVisible();
    await expect(page.getByText("Recent works")).toBeVisible();
  });
  await capture(page, unknownRequests, "/admin/tags", "tag-bubbles.png", async () => {
    await expect(page.getByRole("heading", { level: 1, name: "Tags" })).toBeVisible();
    await expect(page.getByRole("link", { name: /landscape/i })).toBeVisible();
  });
  await capture(page, unknownRequests, "/admin/data-mgmt/dedup", "asset-dedup-review.png", async () => {
    await expect(page.getByText("Harbor Light Study").first()).toBeVisible();
    await expect(page.getByText("104").first()).toBeVisible();
  });
  await capture(page, unknownRequests, "/admin/jobs?tab=downloads", "jobs-operations.png", async () => {
    await expect(page.getByRole("heading", { level: 1, name: "Jobs" })).toBeVisible();
    await expect(page.getByText("Atlas Ink").first()).toBeVisible();
  });

  unknownRequests.length = 0;
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/admin");
  await expect(page.getByRole("heading", { name: "Recently added works" })).toBeVisible();
  await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth)).toBe(true);
  await expect.poll(() => unknownRequests).toEqual([]);
  await page.screenshot({
    path: "/tmp/auto-gallery-dashboard-mobile.png",
    fullPage: true,
    animations: "disabled",
  });

  expect(pageErrors).toEqual([]);
  expect(consoleErrors.filter((message) => !message.includes("WebSocket"))).toEqual([]);
});

import { expect, test, type BrowserContext, type Page } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";

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
  recent: { download_jobs: [], import_jobs: [], tasks: [], works: [], successful_syncs: [] },
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

const providerFixtures = [
  {
    source_name: "pixiv",
    display_name: "Pixiv",
    capabilities: {
      can_download: true,
      can_import_local: false,
      supports_gallerydl: true,
      supports_tags: true,
      is_reference_only: false,
    },
  },
  {
    source_name: "danbooru_reference",
    display_name: "Danbooru Reference",
    capabilities: {
      can_download: false,
      can_import_local: false,
      supports_gallerydl: false,
      supports_tags: false,
      is_reference_only: true,
    },
  },
] as const;

const REPRESENTATIVE_ROUTES = [
  "/admin",
  "/admin/creators",
  "/admin/subscriptions",
  "/admin/jobs?tab=downloads",
  "/admin/scheduler",
  "/admin/notifications",
  "/admin/works",
  "/admin/tags",
  "/admin/upload",
  "/admin/upload/danbooru",
  "/admin/data-mgmt",
  "/admin/data-mgmt/curation",
  "/admin/data-mgmt/dedup",
  "/admin/system",
  "/admin/sources",
  "/admin/search",
  "/admin/settings/users",
  "/admin/settings",
  "/admin/settings/appearance",
  "/admin/settings/auth-status",
  "/admin/settings/backup",
  "/admin/settings/dedup",
  "/admin/settings/download-defaults",
  "/admin/settings/gallerydl",
  "/admin/settings/logs",
  "/admin/settings/profile",
  "/admin/settings/proxy",
  "/admin/settings/scheduler-defaults",
  "/admin/settings/showcase",
  "/admin/settings/subscription-defaults",
] as const;

const DYNAMIC_ROUTES = [
  "/admin/works/fixture-work",
  "/admin/creators/fixture-creator",
  "/admin/creators/fixture-creator/mapping",
  "/admin/creators/duplicates",
  "/admin/subscriptions/fixture-subscription",
  "/admin/subscriptions/repositories/fixture-repository",
  "/admin/tags/fixture-tag",
  "/admin/settings/users/1",
] as const;

const QUALITY_ROUTES = [...REPRESENTATIVE_ROUTES, ...DYNAMIC_ROUTES] as const;

const PRIMARY_ADMIN_ROUTES = [
  "/admin/works",
  "/admin/tags",
  "/admin/upload",
  "/admin/upload/danbooru",
  "/admin/creators",
  "/admin/subscriptions",
  "/admin/jobs?tab=downloads",
  "/admin/scheduler",
  "/admin/data-mgmt",
  "/admin/system",
  "/admin/settings",
] as const;

async function installFixtureRoutes(context: BrowserContext) {
  await context.addCookies([{
    name: "ag_token",
    value: "ui-test-token",
    domain: "127.0.0.1",
    path: "/",
  }]);
  await context.addInitScript(() => {
    window.localStorage.setItem("ag_token", "ui-test-token");
    if (!window.localStorage.getItem("auto-gallery-lang")) {
      window.localStorage.setItem("auto-gallery-lang", "en");
    }
    if (!window.localStorage.getItem("auto-gallery-theme")) {
      window.localStorage.setItem("auto-gallery-theme", "dark");
    }
  });
  await context.route("**/api/v1/**", async (route) => {
    const url = new URL(route.request().url());
    const path = url.pathname;
    if (path === "/api/v1/auth/me") {
      await route.fulfill({ json: me });
    } else if (path === "/api/v1/works/fixture-work") {
      await route.fulfill({
        json: {
          id: "fixture-work",
          title: "Fixture Work",
          description: "Fixture description",
          posted_at: "2026-07-27T10:00:00Z",
          is_nsfw: false,
          is_ai_generated: false,
          asset_count: 0,
          is_favorite: false,
          creator_id: "fixture-creator",
          creator_name: "fixture-creator",
          created_at: "2026-07-27T10:00:00Z",
          updated_at: "2026-07-27T10:00:00Z",
        },
      });
    } else if (/^\/api\/v1\/works\/fixture-work\/(assets|sources|tags)$/.test(path)) {
      await route.fulfill({ json: [] });
    } else if (path === "/api/v1/creators/fixture-creator") {
      await route.fulfill({
        json: {
          id: "fixture-creator",
          name: "fixture-creator",
          display_name: "Fixture Creator",
          description: "Fixture description",
          is_active: true,
          is_favorite: false,
          created_at: "2026-07-27T10:00:00Z",
          updated_at: "2026-07-27T10:00:00Z",
        },
      });
    } else if (path === "/api/v1/creators/fixture-creator/links") {
      await route.fulfill({ json: [] });
    } else if (path === "/api/v1/creators/fixture-creator/timeline") {
      const year = Number((url.searchParams.get("from_date") || "2026").slice(0, 4));
      const days = year === 2025
        ? [
            { date: "2025-02-04", total: 2, pixiv: 2, pixiv_ids: ["work-2025-1", "work-2025-2"] },
            { date: "2025-09-18", total: 1, x: 1, x_ids: ["work-2025-3"] },
          ]
        : [
            { date: "2026-01-03", total: 2, pixiv: 2, pixiv_ids: ["work-2026-1", "work-2026-2"] },
            { date: "2026-04-12", total: 5, pixiv: 3, x: 2, pixiv_ids: ["work-2026-3"], x_ids: ["work-2026-4"] },
            { date: "2026-07-27", total: 1, x: 1, x_ids: ["work-2026-5"] },
          ];
      await route.fulfill({
        json: {
          creator_id: "fixture-creator",
          sources: ["pixiv", "x"],
          days,
          total: days.reduce((sum, day) => sum + day.total, 0),
        },
      });
    } else if (path === "/api/v1/creators/fixture-creator/stats") {
      await route.fulfill({
        json: {
          creator_id: "fixture-creator",
          total_works: 120,
          total_assets: 168,
          total_tags: 8,
          source_breakdown: [
            { source: "pixiv", count: 82 },
            { source: "x", count: 38 },
          ],
          tag_distribution: [
            { tag: "architectural-light", count: 67 },
            { tag: "long-label-for-responsive-validation", count: 52 },
            { tag: "night", count: 31 },
            { tag: "water", count: 23 },
            { tag: "city", count: 19 },
            { tag: "blue", count: 12 },
            { tag: "seventh-is-not-charted", count: 8 },
          ],
          monthly_frequency: [
            { month: "2025-01", count: 1 },
            { month: "2025-02", count: 3 },
            { month: "2025-09", count: 2 },
            { month: "2026-01", count: 8 },
            { month: "2026-04", count: 24 },
            { month: "2026-07", count: 7 },
          ],
        },
      });
    } else if (path === "/api/v1/creators/fixture-creator/subscription-overview") {
      await route.fulfill({
        json: {
          creator_id: "fixture-creator",
          subscriptions: [],
          repositories: [],
          summary: {
            subscription_count: 0,
            repository_count: 0,
            enabled_repository_count: 0,
            running_job_count: 0,
          },
        },
      });
    } else if (path === "/api/v1/creators/duplicates") {
      await route.fulfill({ json: { duplicates: [], total: 0 } });
    } else if (path === "/api/v1/subscriptions/fixture-subscription") {
      await route.fulfill({
        json: {
          id: "fixture-subscription",
          creator_id: "fixture-creator",
          name: "Fixture Subscription",
          creator_name: "fixture-creator",
          creator_display_name: "Fixture Creator",
          is_active: true,
          sync_enabled: true,
          sync_interval_hours: 6,
          source_count: 0,
          enabled_source_count: 0,
          running_job_count: 0,
          failed_job_count: 0,
          created_at: "2026-07-27T10:00:00Z",
          updated_at: "2026-07-27T10:00:00Z",
        },
      });
    } else if (path === "/api/v1/subscriptions/fixture-subscription/sources") {
      await route.fulfill({ json: [] });
    } else if (path === "/api/v1/repositories/fixture-repository") {
      await route.fulfill({
        json: {
          repository: {
            id: "fixture-repository",
            subscription_id: "fixture-subscription",
            source: "pixiv",
            source_display_name: "Pixiv",
            source_creator_id: "fixture-source",
            source_url: "https://www.pixiv.net/users/1",
            is_enabled: true,
            auth_healthy: true,
            can_download: true,
            supports_gallerydl: true,
            url_valid: true,
            is_repository: true,
          },
          creator: {
            id: "fixture-creator",
            name: "fixture-creator",
            display_name: "Fixture Creator",
            is_favorite: false,
          },
          subscription: {
            id: "fixture-subscription",
            name: "Fixture Subscription",
            is_active: true,
            sync_enabled: true,
            sync_interval_hours: 6,
          },
          provider: {
            source: "pixiv",
            display_name: "Pixiv",
            normalized_url: "https://www.pixiv.net/users/1",
            url_valid: true,
            capabilities: {
              can_download: true,
              can_import_local: true,
              supports_gallerydl: true,
              supports_tags: true,
              is_reference_only: false,
            },
          },
          recent_jobs: [],
          recent_works: [],
        },
      });
    } else if (path === "/api/v1/repositories/fixture-repository/tags") {
      await route.fulfill({ json: { items: [], total: 0 } });
    } else if (path === "/api/v1/repositories/fixture-repository/curation-graph") {
      await route.fulfill({
        json: {
          repository_id: "fixture-repository",
          nodes: [],
          edges: [],
          total: 0,
          offset: 0,
          limit: 100,
        },
      });
    } else if (path === "/api/v1/tags/fixture-tag") {
      await route.fulfill({
        json: {
          id: "fixture-tag",
          normalized_name: "fixture-tag",
          category: "general",
          usage_count: 0,
          top_creators: [],
          created_at: "2026-07-27T10:00:00Z",
        },
      });
    } else if (path === "/api/v1/users/1") {
      await route.fulfill({ json: { ...me, created_at: "2026-07-27T10:00:00Z" } });
    } else if (path === "/api/v1/system/workbench") {
      await route.fulfill({ json: workbench });
    } else if (path === "/api/v1/system/scheduler-decisions") {
      await route.fulfill({
        json: {
          updated_at: "2026-07-27T12:00:00Z",
          scheduler_enabled: true,
          timezone: "UTC",
          items: [],
        },
      });
    } else if (path === "/api/v1/download-jobs") {
      await route.fulfill({ json: [longDownloadJob] });
    } else if (path === "/api/v1/tasks") {
      await route.fulfill({ json: { items: [], total: 0, offset: 0, limit: 50 } });
    } else if (path === "/api/v1/import-jobs") {
      await route.fulfill({ json: { items: [], total: 0, offset: 0, limit: 50 } });
    } else if (path === "/api/v1/search/assist") {
      const body = route.request().postDataJSON() as {
        before_cursor?: string;
        after_cursor?: string;
      };
      const query = `${body.before_cursor || ""}${body.after_cursor || ""}`.trim();
      await route.fulfill({
        json: {
          query,
          canonical_query: query,
          parsed: {
            raw: query,
            canonical: query,
            scope: "global",
            targets: ["works", "creators", "tags", "repositories", "subscriptions"],
            tokens: [],
          },
          diagnostics: [],
          suggestions: [],
          catalog: [],
        },
      });
    } else if (path === "/api/v1/search") {
      const scope = url.searchParams.get("scope") || "global";
      const target = scope === "works" || scope === "creator-picker"
        ? (scope === "creator-picker" ? "creators" : "works")
        : scope;
      const groups = scope === "global"
        ? {
            works: { total: 0, items: [] },
            creators: { total: 0, items: [] },
            tags: { total: 0, items: [] },
            repositories: { total: 0, items: [] },
            subscriptions: { total: 0, items: [] },
          }
        : { [target]: { total: 0, items: [] } };
      const query = url.searchParams.get("q") || "";
      await route.fulfill({
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
          total: 0,
          results: [],
          creators: [],
          tags: [],
          repositories: [],
          subscriptions: [],
        },
      });
    } else if (path === "/api/v1/works") {
      await route.fulfill({ json: { items: [], total: 0 } });
    } else if (path === "/api/v1/creators") {
      await route.fulfill({ json: { items: [], total: 0 } });
    } else if (path === "/api/v1/creators/count") {
      await route.fulfill({ json: { count: 0 } });
    } else if (path === "/api/v1/subscriptions") {
      await route.fulfill({ json: [] });
    } else if (path === "/api/v1/subscriptions/count") {
      await route.fulfill({ json: { count: 0 } });
    } else if (path === "/api/v1/users") {
      await route.fulfill({ json: [] });
    } else if (path === "/api/v1/system/health") {
      await route.fulfill({
        json: {
          status: "ok",
          services: { postgres: "up", redis: "up", meilisearch: "up" },
          version: "test",
          business: {},
        },
      });
    } else if (path === "/api/v1/admin/auth-status") {
      await route.fulfill({ json: { summary: { total: 0, healthy: 0, unhealthy: 0, unknown: 0 }, sources: [] } });
    } else if (path === "/api/v1/admin/settings") {
      await route.fulfill({
        json: {
          dedup: {},
          subscription_defaults: {
            default_sync_interval_hours: 6,
            scheduler_scan_interval_minutes: 60,
            scheduler_enabled: true,
            schedule_mode: "interval",
            scheduled_times: "",
            timezone: "UTC",
          },
          download_defaults: {
            timeout_seconds: 1800,
            stall_timeout_seconds: 300,
            max_retries: 3,
            retry_backoff_base_seconds: 60,
            max_posts: 0,
            skip_ai_generated: false,
            gallerydl_retries: 3,
            gallerydl_timeout: 30,
            gallerydl_abort: 300,
            download_concurrency: 2,
          },
          proxy: { http_proxy: "", https_proxy: "", no_proxy: "", enabled: false },
        },
      });
    } else if (path === "/api/v1/admin/gallerydl-config") {
      await route.fulfill({
        json: {
          pixiv: {}, twitter: {}, iwara: {}, danbooru: {},
          pinterest: {}, lofter: {}, weibo: {}, bilibili: {}, sources: {},
        },
      });
    } else if (path === "/api/v1/admin/dedup/cases") {
      await route.fulfill({
        json: { items: [], total: 0, offset: 0, limit: 25 },
      });
    } else if (path === "/api/v1/curation/commits") {
      await route.fulfill({ json: { items: [], total: 0 } });
    } else if (path === "/api/v1/curation/purge/preview") {
      await route.fulfill({ json: { work_count: 0, asset_count: 0, bytes_reclaimable: 0, works: [], assets: [] } });
    } else if (path === "/api/v1/curation/rule-suggestions") {
      await route.fulfill({ json: [] });
    } else if (path === "/api/v1/curation/backfill/status") {
      await route.fulfill({ json: { is_complete: false, expected: {}, existing: {}, missing: {} } });
    } else if (path === "/api/v1/curation/gitllery/status") {
      await route.fulfill({
        json: {
          repositories: [],
          missing_repos: 0,
          behind_total: 0,
          needs_reconcile: false,
        },
      });
    } else if (path === "/api/v1/admin/backup/list") {
      await route.fulfill({ json: { backups: [] } });
    } else if (path === "/api/v1/admin/backup/estimate") {
      await route.fulfill({ json: { components: {} } });
    } else if (path === "/api/v1/tags") {
      await route.fulfill({ json: [] });
    } else if (path === "/api/v1/sources") {
      await route.fulfill({ json: { sources: providerFixtures } });
    } else if (path === "/api/v1/system/logs") {
      await route.fulfill({ json: { entries: [], total: 0, levels: ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] } });
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

test("stored compact sidebar keeps primary page shells stable and aligned from first paint", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 960 });
  await page.addInitScript(() => {
    window.localStorage.setItem("auto-gallery-sidebar-wide-v2", "compact");
    const samples: Array<{ x: number; width: number }> = [];
    const debugWindow = window as typeof window & {
      __adminShellSamples?: Array<{ x: number; width: number }>;
      __adminLayoutShift?: number;
      __adminLayoutShiftSources?: Array<{
        value: number;
        targets: string[];
        rects: Array<{ previousX: number; previousWidth: number; currentX: number; currentWidth: number }>;
      }>;
    };
    debugWindow.__adminShellSamples = samples;
    debugWindow.__adminLayoutShift = 0;
    debugWindow.__adminLayoutShiftSources = [];
    new PerformanceObserver((list) => {
      for (const entry of list.getEntries()) {
        const shift = entry as PerformanceEntry & {
          hadRecentInput?: boolean;
          value?: number;
          sources?: Array<{
            node?: Node | null;
            previousRect?: DOMRectReadOnly;
            currentRect?: DOMRectReadOnly;
          }>;
        };
        if (!shift.hadRecentInput) {
          debugWindow.__adminLayoutShift! += shift.value || 0;
          debugWindow.__adminLayoutShiftSources!.push({
            value: shift.value || 0,
            targets: (shift.sources || []).map((source) => {
              const element = source.node instanceof Element ? source.node : null;
              if (!element) return "unknown";
              return `${element.tagName.toLowerCase()}${element.id ? `#${element.id}` : ""}${element.className ? `.${String(element.className).split(" ").join(".")}` : ""}`;
            }),
            rects: (shift.sources || []).map((source) => ({
              previousX: source.previousRect?.x || 0,
              previousWidth: source.previousRect?.width || 0,
              currentX: source.currentRect?.x || 0,
              currentWidth: source.currentRect?.width || 0,
            })),
          });
        }
      }
    }).observe({ type: "layout-shift", buffered: true });
    let frames = 0;
    const sample = () => {
      const shell = document.querySelector<HTMLElement>("[data-page-shell]");
      if (shell) {
        const box = shell.getBoundingClientRect();
        samples.push({ x: box.x, width: box.width });
      }
      frames += 1;
      if (frames < 180) window.requestAnimationFrame(sample);
    };
    window.requestAnimationFrame(sample);
  });

  const inspectFirstPaint = async (route: string, heading: string) => {
    await page.goto(route);
    await expect(page.getByRole("heading", { level: 1, name: heading })).toBeVisible();
    await page.waitForTimeout(750);

    const result = await page.evaluate(() => {
      const debugWindow = window as typeof window & {
        __adminShellSamples?: Array<{ x: number; width: number }>;
        __adminLayoutShift?: number;
        __adminLayoutShiftSources?: Array<{
          value: number;
          targets: string[];
          rects: Array<{ previousX: number; previousWidth: number; currentX: number; currentWidth: number }>;
        }>;
      };
      const shell = document.querySelector<HTMLElement>("[data-page-shell]")!.getBoundingClientRect();
      return {
        samples: debugWindow.__adminShellSamples || [],
        layoutShift: debugWindow.__adminLayoutShift || 0,
        layoutShiftSources: debugWindow.__adminLayoutShiftSources || [],
        shell: { x: shell.x, width: shell.width },
      };
    });

    expect(result.samples.length).toBeGreaterThan(1);
    const xValues = result.samples.map((sample) => sample.x);
    const widthValues = result.samples.map((sample) => sample.width);
    const mainColumnShifts = result.layoutShiftSources.filter((shift) => (
      shift.targets.some((target) => target.includes("div.flex.min-w-0.flex-1.flex-col"))
      && shift.rects.some((rect) => (
        Math.abs(rect.currentX - rect.previousX) > 1
        || Math.abs(rect.currentWidth - rect.previousWidth) > 1
      ))
    ));
    expect(mainColumnShifts, `${route}: total CLS ${result.layoutShift}`).toEqual([]);
    expect(Math.max(...xValues) - Math.min(...xValues), `${route} shell x`).toBeLessThanOrEqual(1);
    expect(Math.max(...widthValues) - Math.min(...widthValues), `${route} shell width`).toBeLessThanOrEqual(1);
    return result.shell;
  };

  const creatorsShell = await inspectFirstPaint("/admin/creators", "Creators");
  await page.screenshot({ path: "/tmp/auto-gallery-creators-stable-shell.png", fullPage: false });
  const subscriptionsShell = await inspectFirstPaint("/admin/subscriptions", "Subscriptions");
  await page.screenshot({ path: "/tmp/auto-gallery-subscriptions-stable-shell.png", fullPage: false });
  const systemShell = await inspectFirstPaint("/admin/system", "System & Sources");
  const serviceCards = page.locator("#system-panel-services article");
  await expect(serviceCards).toHaveCount(4);
  const serviceCardY = await serviceCards.evaluateAll((cards) => (
    cards.map((card) => card.getBoundingClientRect().y)
  ));
  expect(Math.max(...serviceCardY) - Math.min(...serviceCardY)).toBeLessThanOrEqual(1);
  await page.screenshot({ path: "/tmp/auto-gallery-system-balanced-grid.png", fullPage: false });
  const jobsShell = await inspectFirstPaint("/admin/jobs?tab=downloads", "Jobs");

  for (const [name, shell] of [
    ["creators", creatorsShell],
    ["subscriptions", subscriptionsShell],
    ["system", systemShell],
  ] as const) {
    expect(Math.abs(shell.x - jobsShell.x), `${name} shell x should match Jobs`).toBeLessThanOrEqual(1);
    expect(Math.abs(shell.width - jobsShell.width), `${name} shell width should match Jobs`).toBeLessThanOrEqual(1);
  }
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
  await expect(sidebar.locator("nav a")).toHaveCount(11);
  await expect(sidebar.locator("nav").getByRole("link", { name: "Dashboard", exact: true })).toHaveCount(0);
  await expect(sidebar.locator("[data-sidebar-brand]")).toHaveAttribute("href", "/admin");
  await expect(sidebar.locator("[data-sidebar-brand]")).toHaveAccessibleName("Go to dashboard");
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
  const commandSearch = dialog.getByRole("combobox", { name: "Search works..." });
  await commandSearch.fill("creator");
  await expect(dialog.getByRole("option").first()).toBeVisible();
  await commandSearch.fill("merge candidate");
  await expect(dialog.getByRole("option", { name: /^Asset Deduplication\b/ })).toBeVisible();
  await expect(dialog.getByRole("option", { name: /^Merge Candidates\b/ })).toHaveCount(0);
  await commandSearch.fill("source provider");
  await expect(dialog.getByRole("option", { name: /^System & Sources\b/ })).toBeVisible();
  await commandSearch.fill("notifications");
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
  test.setTimeout(60_000);
  await page.setViewportSize({ width: 1440, height: 960 });

  const shellBox = async (path: string) => {
    await page.goto(path);
    const heading = page.locator("#main-content").getByRole("heading", { level: 1 });
    await expect(heading).toBeVisible();
    const shell = await page.locator("[data-page-shell]").first().boundingBox();
    const header = await page.locator("[data-page-header]").first().boundingBox();
    const primary = await page.locator("[data-page-primary-content]:visible").first().boundingBox();
    const headingBox = await heading.boundingBox();
    expect(shell).not.toBeNull();
    expect(header).not.toBeNull();
    expect(primary).not.toBeNull();
    expect(headingBox).not.toBeNull();
    expect(
      Math.abs(primary!.y - (header!.y + header!.height)),
      `${path} primary content should follow the standard 24px header margin`,
    ).toBeGreaterThanOrEqual(23);
    expect(Math.abs(primary!.y - (header!.y + header!.height))).toBeLessThanOrEqual(25);
    return { shell: shell!, heading: headingBox! };
  };

  const taskPage = await shellBox("/admin/jobs?tab=downloads");
  for (const path of PRIMARY_ADMIN_ROUTES.filter((route) => !route.startsWith("/admin/jobs"))) {
    const current = await shellBox(path);
    expect(Math.abs(current.shell.x - taskPage.shell.x), `${path} shell should align with Jobs`).toBeLessThanOrEqual(1);
    expect(Math.abs(current.shell.width - taskPage.shell.width), `${path} shell should match Jobs width`).toBeLessThanOrEqual(1);
    expect(Math.abs(current.heading.y - taskPage.heading.y), `${path} heading should share the Jobs baseline`).toBeLessThanOrEqual(1);
  }

  await page.goto("/admin/works?creator=creator-atlas");
  await expect(page.getByRole("combobox", { name: "Filter creator" })).toHaveCount(0);
  await expectNoPageOverflow(page);
  await page.screenshot({ path: "/tmp/auto-gallery-page-alignment.png", fullPage: false });
});

for (const viewport of [
  { name: "tablet", width: 768, height: 1024 },
  { name: "mobile", width: 390, height: 844 },
] as const) {
  test(`primary admin routes keep their hierarchy and reflow at ${viewport.name} width`, async ({ page }) => {
    test.setTimeout(60_000);
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    for (const route of PRIMARY_ADMIN_ROUTES) {
      await page.goto(route);
      const main = page.locator("#main-content");
      await expect(main.getByRole("heading", { level: 1 })).toBeVisible();
      await expect(page.locator("[data-page-shell]").first()).toBeVisible();
      await expect(page.locator("[data-page-header]").first()).toBeVisible();
      await expect(page.locator("[data-page-primary-content]:visible").first()).toBeVisible();
      await expect.poll(() => page.evaluate(() => {
        const heading = document.querySelector("#main-content h1")?.getBoundingClientRect();
        const topbar = document.querySelector("header.sticky")?.getBoundingClientRect();
        return Boolean(heading && topbar && heading.top >= topbar.bottom);
      })).toBe(true);
      await expectNoPageOverflow(page);
    }
  });
}

test("creator charts share the data contract and year selection reloads the requested range", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 960 });
  await page.goto("/admin/creators/fixture-creator");

  await expect(page.getByTestId("creator-activity-chart")).toBeVisible();
  await expect(page.getByTestId("creator-source-chart")).toBeVisible();
  await expect(page.getByTestId("creator-tag-chart")).toBeVisible();
  await expect(page.getByTestId("creator-monthly-chart")).toBeVisible();
  await expect(page.locator('[data-chart-kind="tick-rows"]')).toHaveAttribute("data-chart-unit", "5");
  await expect(page.locator('[data-chart-kind="ballot-tally"] a')).toHaveCount(6);
  await page.screenshot({ path: "/tmp/auto-gallery-creator-charts-desktop.png", fullPage: true });

  const firstDay = page.locator('[data-chart-kind="activity-dot-matrix"] [role="group"] button').first();
  await firstDay.focus();
  const firstLabel = await firstDay.getAttribute("aria-label");
  await page.keyboard.press("ArrowRight");
  const nextDay = page.locator('[data-chart-kind="activity-dot-matrix"] button:focus');
  const focusedLabel = await page.evaluate(() => document.activeElement?.getAttribute("aria-label"));
  expect(focusedLabel).not.toBe(firstLabel);
  await expect(nextDay).toHaveCount(1);
  await page.keyboard.press("Escape");

  const request2025 = page.waitForRequest((request) => {
    const url = new URL(request.url());
    return url.pathname === "/api/v1/creators/fixture-creator/timeline"
      && url.searchParams.get("from_date") === "2025-01-01";
  });
  await page.getByLabel("Year", { exact: true }).selectOption("2025");
  const request = await request2025;
  const requestUrl = new URL(request.url());
  expect(requestUrl.searchParams.get("to_date")).toBe("2026-01-01");
  await expect(page.getByTestId("creator-activity-chart")).toContainText("Activity peaked on");

  for (const frame of await page.locator("[data-chart-frame]").all()) {
    const details = frame.locator("details");
    if (await details.count()) {
      await details.locator("summary").click();
      await expect(details.getByRole("table")).toBeVisible();
    }
  }
  for (const viewport of [
    { width: 768, height: 1024 },
    { width: 390, height: 844 },
    { width: 320, height: 720 },
  ]) {
    await page.setViewportSize(viewport);
    await expectNoPageOverflow(page);
  }
  await page.screenshot({ path: "/tmp/auto-gallery-creator-charts-mobile.png", fullPage: true });
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.reload();
  await expect(page.getByTestId("creator-activity-chart")).toBeVisible();
  await expect(page.locator(".chart-dot-enter")).toHaveCount(0);
  await expectNoPageOverflow(page);
});

test("data management charts preserve 100 ticks, exact values, hierarchy, and diagnostics", async ({ page }) => {
  await page.route("**/api/v1/admin/system-info", (route) => route.fulfill({
    json: {
      version: "fixture",
      downloads_size_mb: 1000,
      library_size_mb: 75,
      downloads_free_gb: 500,
      archives_kb: {},
    },
  }));
  await page.route("**/api/v1/admin/storage-breakdown", (route) => route.fulfill({
    json: {
      sources: {
        pixiv: { size_mb: 500, creator_count: 2, work_count: 80 },
        x: { size_mb: 250, creator_count: 2, work_count: 42 },
        fanbox: { size_mb: 100, creator_count: 1, work_count: 18 },
        weibo: { size_mb: 50, creator_count: 1, work_count: 9 },
        iwara: { size_mb: 40, creator_count: 1, work_count: 7 },
        tumblr: { size_mb: 30, creator_count: 1, work_count: 6 },
        local: { size_mb: 30, creator_count: 1, work_count: 4 },
      },
      creator_tree: [
        {
          creator_id: "fixture-creator",
          display_name: "Fixture Creator",
          size_mb: 600,
          work_count: 92,
          repository_count: 2,
          repositories: [
            {
              repository_id: "fixture-repository",
              source: "pixiv",
              source_display_name: "Pixiv",
              disk_source: "pixiv",
              directory_name: "fixture-pixiv-repository",
              size_mb: 400,
              work_count: 62,
            },
            {
              repository_id: "fixture-x-repository",
              source: "x",
              source_display_name: "X",
              disk_source: "twitter",
              directory_name: "fixture-x-repository",
              size_mb: 200,
              work_count: 30,
            },
          ],
        },
        {
          creator_id: "fixture-creator-2",
          display_name: "Second Fixture",
          size_mb: 300,
          work_count: 48,
          repository_count: 1,
          repositories: [
            {
              repository_id: "fixture-repository-2",
              source: "fanbox",
              source_display_name: "Fanbox",
              disk_source: "fanbox",
              directory_name: "fixture-fanbox-repository",
              size_mb: 300,
              work_count: 48,
            },
          ],
        },
      ],
      unlinked_repositories: [
        {
          repository_id: null,
          source: "local",
          source_display_name: "Local",
          disk_source: "local",
          directory_name: "unlinked-fixture",
          size_mb: 100,
          work_count: 3,
        },
      ],
      db_stats: { works: 140, assets: 221, tags: 57 },
      creators: [],
    },
  }));

  await page.setViewportSize({ width: 1440, height: 960 });
  await page.goto("/admin/data-mgmt");
  await expect(page.getByTestId("storage-source-chart")).toBeVisible();
  await expect(page.getByTestId("creator-storage-chart")).toBeVisible();
  await expect(page.locator('[data-chart-kind="tick-donut"] svg line')).toHaveCount(100);
  await expect(page.getByTestId("storage-source-chart")).toContainText("Other");
  await expect(page.getByRole("heading", { name: "Unlinked repositories" })).toBeVisible();

  await page.getByRole("button", { name: "Expand Fixture Creator" }).click();
  await expect(page.getByRole("button", { name: "Collapse Fixture Creator" })).toBeVisible();
  await expect(page.getByRole("link", { name: /fixture-pixiv-repository/ })).toHaveAttribute(
    "href",
    "/admin/subscriptions/repositories/fixture-repository",
  );

  const sourceData = page.getByTestId("storage-source-chart").locator("details");
  await sourceData.locator("summary").click();
  await expect(sourceData.getByRole("table")).toContainText("500.0 MB");
  await page.screenshot({ path: "/tmp/auto-gallery-data-charts-desktop.png", fullPage: true });
  const axe = await new AxeBuilder({ page })
    .include("#main-content")
    .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"])
    .analyze();
  expect(axe.violations).toEqual([]);
  for (const viewport of [
    { width: 768, height: 1024 },
    { width: 390, height: 844 },
    { width: 320, height: 720 },
  ]) {
    await page.setViewportSize(viewport);
    await expectNoPageOverflow(page);
  }
  await page.screenshot({ path: "/tmp/auto-gallery-data-charts-mobile.png", fullPage: true });
  await expectNoPageOverflow(page);
});

for (const route of QUALITY_ROUTES) {
  test(`route quality: ${route}`, async ({ page }) => {
    const pageErrors: string[] = [];
    page.on("pageerror", (error) => pageErrors.push(error.stack || error.message));
    await page.setViewportSize({ width: 1440, height: 960 });
    await page.goto(route);
    await expect(page.locator("#main-content")).toBeVisible();
    await expect(page.getByRole("main")).toHaveCount(1);
    await expect(page.locator("[data-nextjs-dialog-overlay]")).toHaveCount(0);
    if (route === "/admin/sources") {
      await expect(page.getByRole("heading", { level: 3, name: "Pixiv" })).toBeVisible();
      await expect(page.locator("#main-content .page-item").last()).toHaveCSS("opacity", "1");
    }
    expect(pageErrors, `${route} should not throw a framework error`).toEqual([]);
    const mainText = (await page.locator("#main-content").innerText()).replaceAll("中文", "");
    expect(mainText, `${route} should not leak Chinese copy in English mode`).not.toMatch(/[\u3400-\u9fff]/u);
    const results = await new AxeBuilder({ page })
      .include("#main-content")
      .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"])
      .analyze();
    expect(
      results.violations.map((violation) => ({
        id: violation.id,
        targets: violation.nodes.map((node) => node.target.join(" ")),
      })),
      `${route} should have no axe violations`,
    ).toEqual([]);
  });
}

for (const route of QUALITY_ROUTES) {
  test(`Chinese light route quality: ${route}`, async ({ page }) => {
    const missingTranslations: string[] = [];
    const pageErrors: string[] = [];
    page.on("pageerror", (error) => pageErrors.push(error.stack || error.message));
    page.on("console", (message) => {
      if (message.type() === "error" && message.text().includes("[i18n] Missing")) {
        missingTranslations.push(message.text());
      }
    });
    await page.addInitScript(() => {
      window.localStorage.setItem("auto-gallery-lang", "zh");
      window.localStorage.setItem("auto-gallery-theme", "light");
    });
    await page.setViewportSize({ width: 1440, height: 960 });
    await page.goto(route);
    await expect(page.locator("html")).not.toHaveClass(/dark/);
    await expect(page.locator("#main-content")).toBeVisible();
    await expect(page.getByRole("main")).toHaveCount(1);
    await expect(page.locator("[data-nextjs-dialog-overlay]")).toHaveCount(0);
    if (route === "/admin/sources") {
      await expect(page.getByRole("heading", { level: 3, name: "Pixiv" })).toBeVisible();
      await expect(page.locator("#main-content .page-item").last()).toHaveCSS("opacity", "1");
    }
    expect(pageErrors, `${route} should not throw a framework error`).toEqual([]);
    expect(missingTranslations, `${route} should not use raw translation keys`).toEqual([]);
    if (route === "/admin/settings/logs") {
      await page.screenshot({ path: "/tmp/auto-gallery-logs-zh-light.png", fullPage: false });
    }
    const results = await new AxeBuilder({ page })
      .include("#main-content")
      .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"])
      .analyze();
    expect(
      results.violations.map((violation) => ({
        id: violation.id,
        targets: violation.nodes.map((node) => node.target.join(" ")),
      })),
      `${route} should have no axe violations in Chinese light mode`,
    ).toEqual([]);
  });
}

test("system and source tabs fetch only their active data and retain provider tools", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 960 });
  let healthRequests = 0;
  let sourceRequests = 0;
  await page.route("**/api/v1/system/health", async (route) => {
    healthRequests += 1;
    await route.fulfill({
      json: {
        status: "ok",
        services: { postgres: "up", redis: "up", meilisearch: "up" },
        version: "test",
        business: {},
      },
    });
  });
  await page.route("**/api/v1/sources", async (route) => {
    sourceRequests += 1;
    await route.fulfill({ json: { sources: providerFixtures } });
  });

  await page.goto("/admin/system");
  await expect(page.getByRole("heading", { level: 1, name: "System & Sources" })).toBeVisible();
  await expect(page.getByRole("tab", { name: "Service Status" })).toHaveAttribute("aria-selected", "true");
  await expect(page.getByRole("tab", { name: "Sources" })).toBeVisible();
  await expect.poll(() => healthRequests).toBeGreaterThan(0);
  expect(sourceRequests).toBe(0);
  await page.screenshot({ path: "/tmp/auto-gallery-system-services.png", fullPage: false });

  await page.getByRole("tab", { name: "Sources" }).click();
  await expect(page).toHaveURL(/\/admin\/system\?tab=sources$/);
  await expect(page.getByRole("tab", { name: "Sources" })).toHaveAttribute("aria-selected", "true");
  await expect(page.getByRole("heading", { level: 3, name: "Pixiv" })).toBeVisible();
  await expect.poll(() => sourceRequests).toBeGreaterThan(0);

  const pixivCard = page.getByRole("article").filter({ has: page.getByRole("heading", { name: "Pixiv" }) });
  await pixivCard.getByRole("button", { name: /Try default URL/ }).click();
  await pixivCard.getByRole("textbox", { name: "Test URL Validation" }).press("Enter");
  await expect(pixivCard.getByRole("status")).toContainText("matches expected Pixiv pattern");

  const healthRequestsBeforeRefresh = healthRequests;
  const sourceRequestsBeforeRefresh = sourceRequests;
  const refreshButton = page.getByRole("button", { name: "Refresh" });
  await refreshButton.click();
  await expect.poll(() => sourceRequests).toBeGreaterThan(sourceRequestsBeforeRefresh);
  await expect(refreshButton).toBeEnabled();
  expect(healthRequests).toBe(healthRequestsBeforeRefresh);

  const axe = await new AxeBuilder({ page })
    .include("#main-content")
    .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"])
    .analyze();
  expect(axe.violations).toEqual([]);
  await expectNoPageOverflow(page);
  await page.screenshot({ path: "/tmp/auto-gallery-system-sources.png", fullPage: true });
});

test("system and source tabs preserve module-level permissions", async ({ page }) => {
  let healthRequests = 0;
  let sourceRequests = 0;
  await page.route("**/api/v1/system/health", async (route) => {
    healthRequests += 1;
    await route.fulfill({ json: { status: "ok", services: {}, version: "test", business: {} } });
  });
  await page.route("**/api/v1/sources", async (route) => {
    sourceRequests += 1;
    await route.fulfill({ json: { sources: providerFixtures } });
  });

  await page.route("**/api/v1/auth/me", (route) => route.fulfill({
    json: {
      ...me,
      is_admin: false,
      permissions: ["system"],
      modules: { system: true, subscriptions: false },
    },
  }));
  await page.goto("/admin/system?tab=sources");
  await expect(page).toHaveURL(/\/admin\/system\?tab=services$/);
  await expect(page.getByRole("tab", { name: "Service Status" })).toBeVisible();
  await expect(page.getByRole("tab", { name: "Sources" })).toHaveCount(0);
  await expect.poll(() => healthRequests).toBeGreaterThan(0);
  expect(sourceRequests).toBe(0);

  await page.route("**/api/v1/auth/me", (route) => route.fulfill({
    json: {
      ...me,
      is_admin: false,
      permissions: ["subscriptions"],
      modules: { system: false, subscriptions: true },
    },
  }));
  await page.goto("/admin/sources");
  await expect(page).toHaveURL(/\/admin\/system\?tab=sources$/);
  await expect(page.getByRole("tab", { name: "Sources" })).toBeVisible();
  await expect(page.getByRole("tab", { name: "Service Status" })).toHaveCount(0);
  await expect(page.locator("#admin-sidebar").getByRole("link", { name: "System & Sources" })).toBeVisible();
  const healthRequestsBeforeSourceOnly = healthRequests;
  await expect.poll(() => sourceRequests).toBeGreaterThan(0);
  expect(healthRequests).toBe(healthRequestsBeforeSourceOnly);
});

for (const viewport of [
  { name: "tablet", width: 768, height: 1024 },
  { name: "mobile", width: 390, height: 844 },
] as const) {
  test(`source registry reflows at ${viewport.name} width`, async ({ page }) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    await page.goto("/admin/system?tab=sources");
    await expect(page.getByRole("tab", { name: "Sources" })).toHaveAttribute("aria-selected", "true");
    await expect(page.getByRole("heading", { level: 3, name: "Pixiv" })).toBeVisible();
    await expectNoPageOverflow(page);
    if (viewport.name === "mobile") {
      await page.screenshot({ path: "/tmp/auto-gallery-system-sources-mobile.png", fullPage: true });
    }
  });
}

test("dedup status is addressable and browser history restores the selected review queue", async ({ page }) => {
  await page.goto("/admin/data-mgmt/dedup?status=deferred");
  await expect(page.getByText("0 candidates in the current view")).toBeVisible();
  await expect(page.getByRole("tab", { name: "Deferred" })).toHaveAttribute("aria-selected", "true");
  await page.getByRole("tab", { name: "Pending" }).click();
  await expect(page).toHaveURL(/\/admin\/data-mgmt\/dedup\?status=pending$/);
  await expect(page.getByRole("tab", { name: "Pending" })).toHaveAttribute("aria-selected", "true");
  await page.goBack();
  await expect(page).toHaveURL(/\/admin\/data-mgmt\/dedup\?status=deferred$/);
  await expect(page.getByRole("tab", { name: "Deferred" })).toHaveAttribute("aria-selected", "true");
});

test("legacy task, source, merge, and settings data routes redirect to their maintained destinations", async ({ page }) => {
  await page.goto("/admin/import-jobs");
  await expect(page).toHaveURL(/\/admin\/jobs\?tab=imports$/);
  await expect(page.locator("[data-page-shell]")).toBeVisible();

  await page.goto("/admin/sources");
  await expect(page).toHaveURL(/\/admin\/system\?tab=sources$/);
  await expect(page.getByRole("tab", { name: "Sources" })).toHaveAttribute("aria-selected", "true");

  await page.goto("/admin/merge-candidates");
  await expect(page).toHaveURL(/\/admin\/data-mgmt\/dedup\?status=pending$/);
  await expect(page.getByRole("tab", { name: "Pending" })).toHaveAttribute("aria-selected", "true");
  await expect(page.locator("[data-page-shell]")).toBeVisible();

  await page.goto("/admin/settings/data-mgmt");
  await expect(page).toHaveURL(/\/admin\/data-mgmt$/);
  await expect(page.getByRole("heading", { level: 1, name: "Data Management" })).toBeVisible();
});

test("migrated admin URLs return permanent redirects and preserve deep-link state", async ({ page }) => {
  const cases = [
    ["/admin/reference/danbooru?artist=atlas&tag=a&tag=b", "/admin/upload/danbooru?artist=atlas&tag=a&tag=b"],
    ["/admin/repositories/repo-1?tab=tags&page=2", "/admin/subscriptions/repositories/repo-1?tab=tags&page=2"],
    ["/admin/curation?cursor=commit-1", "/admin/data-mgmt/curation?cursor=commit-1"],
    ["/admin/dedup?status=deferred&page=3", "/admin/data-mgmt/dedup?status=deferred&page=3"],
    ["/admin/users?status=inactive", "/admin/settings/users?status=inactive"],
    ["/admin/users/42?from=audit", "/admin/settings/users/42?from=audit"],
    ["/admin/merge-candidates?view=grid&status=deferred", "/admin/data-mgmt/dedup?view=grid&status=pending"],
    ["/admin/sources?from=bookmark", "/admin/system?from=bookmark&tab=sources"],
    ["/admin/import-jobs?from=bookmark", "/admin/jobs?from=bookmark&tab=imports"],
    ["/admin/settings/data-mgmt?from=bookmark", "/admin/data-mgmt?from=bookmark"],
  ] as const;

  for (const [legacy, canonical] of cases) {
    const response = await page.request.get(legacy, { maxRedirects: 0 });
    expect(response.status(), legacy).toBe(308);
    const location = new URL(response.headers().location, "http://127.0.0.1:13000");
    expect(`${location.pathname}${location.search}`, legacy).toBe(canonical);
  }
});

test("tasks and scheduler paginate the unified search contract without exceeding 100", async ({ page }) => {
  const requested = { tasks: [] as number[], scheduler: [] as number[] };
  await page.route("**/api/v1/search**", async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname !== "/api/v1/search") {
      await route.fallback();
      return;
    }
    const scope = url.searchParams.get("scope");
    if (scope !== "tasks" && scope !== "scheduler") {
      await route.fallback();
      return;
    }
    expect(url.searchParams.get("limit")).toBe("100");
    const offset = Number(url.searchParams.get("offset") || 0);
    requested[scope].push(offset);
    const task = {
      id: `task-${offset}`,
      kind: "admin",
      operation_type: "contract-check",
      status: "complete",
      title: `Task page ${offset / 100 + 1}`,
      created_at: "2026-07-27T10:00:00Z",
    };
    const decision = {
      subscription_id: `subscription-${offset}`,
      subscription_name: "Fixture subscription",
      subscription_active: true,
      subscription_sync_enabled: true,
      creator_id: `creator-${offset}`,
      creator_name: `Creator page ${offset / 100 + 1}`,
      source_id: `repository-${offset}`,
      source: "pixiv",
      source_url: "https://www.pixiv.net/users/10000001",
      source_enabled: true,
      effective_mode: "interval",
      timezone: "UTC",
      sync_interval_hours: 6,
      due: true,
      decision: "sync",
      reason: "due",
      auth_healthy: true,
      url_valid: true,
      can_download: true,
    };
    const items = scope === "tasks" ? [task] : [decision];
    await route.fulfill({
      json: {
        query: "",
        canonical_query: "",
        parsed: { raw: "", canonical: "", scope, targets: [scope], tokens: [] },
        groups: { [scope]: { total: 205, items } },
        total: 205,
        results: [],
        creators: [],
        tags: [],
        repositories: [],
        subscriptions: [],
      },
    });
  });

  await page.goto("/admin/jobs");
  await expect(page.getByText("Task page 1")).toBeVisible();
  await expect(page.getByText("Page 1 of 3")).toBeVisible();
  await page.getByRole("navigation", { name: "Pagination" }).getByRole("button", { name: "Next" }).click();
  await expect(page).toHaveURL(/\/admin\/jobs\?page=2$/);
  await expect(page.getByText("Task page 2")).toBeVisible();
  expect(requested.tasks).toContain(0);
  expect(requested.tasks).toContain(100);

  await page.goto("/admin/scheduler");
  await expect(page.getByText("Creator page 1")).toBeVisible();
  await page.getByRole("navigation", { name: "Pagination" }).getByRole("button", { name: "Next" }).click();
  await expect(page).toHaveURL(/\/admin\/scheduler\?page=2$/);
  await expect(page.getByText("Creator page 2")).toBeVisible();
  expect(requested.scheduler).toContain(0);
  expect(requested.scheduler).toContain(100);
});

test("settings no longer duplicates data management or language controls", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 960 });
  await page.goto("/admin/settings");
  const main = page.locator("#main-content");
  await expect(main.getByRole("heading", { level: 1, name: "Settings" })).toBeVisible();
  await expect(main.getByRole("link", { name: /Data Management/ })).toHaveCount(0);
  await expect(main.getByRole("heading", { name: "Language" })).toHaveCount(0);
  await page.screenshot({ path: "/tmp/auto-gallery-settings-clean.png", fullPage: false });
});

test("restricted direct access renders the standard shell permission state", async ({ page }) => {
  await page.route("**/api/v1/auth/me", (route) => route.fulfill({
    json: { ...me, is_admin: false, permissions: ["library"], modules: { library: true, system: false } },
  }));
  await page.goto("/admin/settings/logs");
  await expect(page.locator("[data-page-shell]")).toBeVisible();
  await expect(page.getByRole("heading", { name: "You don't have permission to access this page" })).toBeVisible();
  await expect(page.getByRole("main")).toHaveCount(1);

  await page.goto("/admin/system");
  await expect(page.locator("[data-page-shell]")).toBeVisible();
  await expect(page.getByRole("heading", { name: "You don't have permission to access this page" })).toBeVisible();
  await expect(page.getByRole("tab")).toHaveCount(0);
});

test("route changes focus main content and dismissible menus restore trigger focus", async ({ page }) => {
  await page.goto("/admin/jobs?tab=downloads");
  const userMenu = page.getByRole("button", { name: "User menu" });
  await userMenu.click();
  await expect(userMenu).toHaveAttribute("aria-expanded", "true");
  await page.keyboard.press("Escape");
  await expect(userMenu).toHaveAttribute("aria-expanded", "false");
  await expect(userMenu).toBeFocused();

  const notificationBell = page.locator('header button[aria-label="Notifications"]');
  await notificationBell.click();
  await page.keyboard.press("Escape");
  await expect(notificationBell).toBeFocused();

  await page.locator("#admin-sidebar").getByRole("link", { name: "Works" }).click();
  await expect(page).toHaveURL(/\/admin\/works$/);
  await expect.poll(() => page.evaluate(() => document.activeElement?.id)).toBe("main-content");
});

test("pathname navigation resets the viewport without hiding the page heading", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 960 });
  await page.goto("/admin/upload");
  await expect(page.getByRole("heading", { level: 1, name: "Upload" })).toBeVisible();
  await page.evaluate(() => window.scrollTo(0, document.documentElement.scrollHeight));
  await expect.poll(() => page.evaluate(() => window.scrollY)).toBeGreaterThan(0);

  await page.locator("#admin-sidebar").getByRole("link", { name: "Tags" }).click();
  await expect(page).toHaveURL(/\/admin\/tags$/);
  await expect.poll(() => page.evaluate(() => window.scrollY)).toBe(0);
  await expect.poll(() => page.evaluate(() => document.activeElement?.id)).toBe("main-content");

  await page.locator("#admin-sidebar").getByRole("link", { name: "Upload" }).click();
  await expect(page).toHaveURL(/\/admin\/upload$/);
  await expect.poll(() => page.evaluate(() => window.scrollY)).toBe(0);
  await expect.poll(() => page.evaluate(() => {
    const heading = document.querySelector("h1")?.getBoundingClientRect();
    const topbar = document.querySelector("header.sticky")?.getBoundingClientRect();
    return Boolean(heading && topbar && heading.top >= topbar.bottom);
  })).toBe(true);
  await expect.poll(() => page.evaluate(() => document.activeElement?.id)).toBe("main-content");
  await page.screenshot({ path: "/tmp/auto-gallery-upload-top-fixed.png", fullPage: false });
});

test("query-only task navigation preserves the current viewport", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 600 });
  await page.goto("/admin/jobs?tab=downloads");
  await expect(page.getByText("xianyuliangryo-with-a-very-long-creator-name")).toBeVisible();
  await expect.poll(() => page.evaluate(() => document.documentElement.scrollHeight > window.innerHeight)).toBe(true);
  await page.evaluate(() => window.scrollTo(0, 260));
  await expect.poll(() => page.evaluate(() => window.scrollY)).toBeGreaterThan(0);
  const retainedY = await page.evaluate(() => window.scrollY);
  await page.getByRole("tab", { name: "Import" }).evaluate((element: HTMLElement) => element.click());
  await expect(page).toHaveURL(/\/admin\/jobs\?tab=imports$/);
  await expect.poll(() => page.evaluate(() => window.scrollY)).toBe(retainedY);
});

test("shared dialogs trap focus, close with Escape, and restore their trigger", async ({ page }) => {
  await page.goto("/admin/data-mgmt");
  await page.getByPlaceholder("CONFIRM DELETE").fill("CONFIRM DELETE");
  const trigger = page.getByRole("button", { name: "Delete All Works" });
  await trigger.click();
  const dialog = page.getByRole("dialog");
  await expect(dialog).toBeVisible();
  await expect.poll(() => page.evaluate(() => Boolean(document.activeElement?.closest('[role="dialog"]')))).toBe(true);
  await page.keyboard.press("Shift+Tab");
  await expect.poll(() => page.evaluate(() => Boolean(document.activeElement?.closest('[role="dialog"]')))).toBe(true);
  await page.keyboard.press("Escape");
  await expect(dialog).toBeHidden();
  await expect(trigger).toBeFocused();
});

test("compact tablet sidebar and long job metadata do not create root overflow", async ({ page }) => {
  await page.setViewportSize({ width: 768, height: 1024 });
  await page.goto("/admin/jobs?tab=downloads");
  await expect(page.locator("aside").first()).toHaveCSS("width", "64px");
  await expect(page.getByText("xianyuliangryo-with-a-very-long-creator-name")).toBeVisible();
  await expectNoPageOverflow(page);
  await page.screenshot({ path: "/tmp/auto-gallery-jobs-tablet.png", fullPage: true });
});

test("200 percent equivalent reflow and reduced motion keep content visible", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  // A 720 CSS-pixel viewport represents a 1440-pixel desktop at 200% zoom.
  await page.setViewportSize({ width: 720, height: 900 });
  await page.goto("/admin/jobs?tab=downloads");
  await expect(page.getByRole("heading", { level: 1, name: "Jobs" })).toBeVisible();
  await expect(page.locator(".page-item").first()).toHaveCSS("opacity", "1");
  await expectNoPageOverflow(page);
});

test("390 pixel mobile layout stays within the viewport", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/admin/upload/danbooru");
  await expect(page.getByRole("heading", { level: 1, name: "Danbooru Reference Mapping" })).toBeVisible();
  await expectNoPageOverflow(page);
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
  await expect(trigger).toBeFocused();
  await trigger.click();
  await expect(page.locator("#admin-mobile-sidebar")).toBeVisible();
  await page.locator("#admin-mobile-sidebar").evaluate((element) => {
    for (const animation of element.getAnimations()) animation.finish();
  });
  const mobileBrand = page.locator("#admin-mobile-sidebar [data-sidebar-brand]");
  await expect(mobileBrand).toHaveAccessibleName("Go to dashboard");
  await page.screenshot({ path: "/tmp/auto-gallery-sidebar-brand-mobile.png", fullPage: false });
  await mobileBrand.click();
  await expect(page).toHaveURL(/\/admin$/);
  await expect(page.locator("#admin-mobile-sidebar")).toBeHidden();
  await expect.poll(() => page.evaluate(() => document.activeElement?.id)).toBe("main-content");
  await expectNoPageOverflow(page);
  await page.screenshot({ path: "/tmp/auto-gallery-jobs-mobile.png", fullPage: true });
});

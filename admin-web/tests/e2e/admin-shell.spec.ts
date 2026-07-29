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
  "/admin/reference/danbooru",
  "/admin/data-mgmt",
  "/admin/curation",
  "/admin/dedup",
  "/admin/system",
  "/admin/sources",
  "/admin/search",
  "/admin/users",
  "/admin/settings",
  "/admin/settings/appearance",
  "/admin/settings/auth-status",
  "/admin/settings/backup",
  "/admin/settings/data-mgmt",
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
  "/admin/repositories/fixture-repository",
  "/admin/tags/fixture-tag",
  "/admin/users/1",
] as const;

const QUALITY_ROUTES = [...REPRESENTATIVE_ROUTES, ...DYNAMIC_ROUTES] as const;

const PRIMARY_ADMIN_ROUTES = [
  "/admin/works",
  "/admin/tags",
  "/admin/upload",
  "/admin/reference/danbooru",
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
      await route.fulfill({ json: { creator_id: "fixture-creator", sources: [], days: [], total: 0 } });
    } else if (path === "/api/v1/creators/fixture-creator/stats") {
      await route.fulfill({
        json: {
          creator_id: "fixture-creator",
          total_works: 0,
          total_assets: 0,
          total_tags: 0,
          source_breakdown: [],
          tag_distribution: [],
          monthly_frequency: [],
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
  await dialog.getByRole("textbox").fill("creator");
  await expect(dialog.getByRole("option").first()).toBeVisible();
  await dialog.getByRole("textbox").fill("merge candidate");
  await expect(dialog.getByRole("option", { name: /^Asset Deduplication\b/ })).toBeVisible();
  await expect(dialog.getByRole("option", { name: /^Merge Candidates\b/ })).toHaveCount(0);
  await dialog.getByRole("textbox").fill("source provider");
  await expect(dialog.getByRole("option", { name: /^System & Sources\b/ })).toBeVisible();
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
  await page.goto("/admin/dedup?status=deferred");
  await expect(page.getByText("0 candidates in the current view")).toBeVisible();
  await expect(page.getByRole("tab", { name: "Deferred" })).toHaveAttribute("aria-selected", "true");
  await page.getByRole("tab", { name: "Pending" }).click();
  await expect(page).toHaveURL(/\/admin\/dedup\?status=pending$/);
  await expect(page.getByRole("tab", { name: "Pending" })).toHaveAttribute("aria-selected", "true");
  await page.goBack();
  await expect(page).toHaveURL(/\/admin\/dedup\?status=deferred$/);
  await expect(page.getByRole("tab", { name: "Deferred" })).toHaveAttribute("aria-selected", "true");
});

test("legacy task, source, and merge routes redirect to their maintained destinations", async ({ page }) => {
  await page.goto("/admin/import-jobs");
  await expect(page).toHaveURL(/\/admin\/jobs\?tab=imports$/);
  await expect(page.locator("[data-page-shell]")).toBeVisible();

  await page.goto("/admin/sources");
  await expect(page).toHaveURL(/\/admin\/system\?tab=sources$/);
  await expect(page.getByRole("tab", { name: "Sources" })).toHaveAttribute("aria-selected", "true");

  await page.goto("/admin/merge-candidates");
  await expect(page).toHaveURL(/\/admin\/dedup\?status=pending$/);
  await expect(page.getByRole("tab", { name: "Pending" })).toHaveAttribute("aria-selected", "true");
  await expect(page.locator("[data-page-shell]")).toBeVisible();
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
  await page.goto("/admin/settings/data-mgmt");
  const trigger = page.getByRole("button", { name: "Clear" }).first();
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
  await page.goto("/admin/reference/danbooru");
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

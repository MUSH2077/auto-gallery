import { expect, test, type BrowserContext, type Route } from "@playwright/test";

test.describe.configure({ timeout: 90_000 });

const json = (route: Route, body: unknown, status = 200) => route.fulfill({
  status,
  contentType: "application/json",
  body: JSON.stringify(body),
});

const workbench = {
  updated_at: "2026-09-22T00:00:00Z",
  queue: { active_download_count: 0, active_import_count: 0, failed_download_count: 0, failed_import_count: 0, stale_count: 0 },
  scheduler: {},
  storage: {},
  attention: {},
  recent: { download_jobs: [], import_jobs: [], works: [], successful_syncs: [] },
};

const works = [
  {
    id: "work-sfw",
    title: "Harbor light",
    posted_at: "2026-09-20T00:00:00Z",
    created_at: "2026-09-21T00:00:00Z",
    is_nsfw: false,
    is_ai_generated: true,
    is_favorite: true,
    asset_count: 1,
    thumbnail_asset_id: null,
    thumbnail_width: 1200,
    thumbnail_height: 800,
    source: "pixiv",
  },
  {
    id: "work-nsfw",
    title: "Portrait study",
    posted_at: "2026-09-18T00:00:00Z",
    created_at: "2026-09-19T00:00:00Z",
    is_nsfw: true,
    is_ai_generated: false,
    is_favorite: false,
    asset_count: 1,
    thumbnail_asset_id: null,
    thumbnail_width: 800,
    thumbnail_height: 1200,
    source: "x",
  },
];

async function setup(
  context: BrowserContext,
  options: { preferences?: Record<string, unknown> } = {},
) {
  let searchRequests = 0;
  const preferenceWrites: Record<string, unknown>[] = [];
  await context.addCookies([{ name: "ag_token", value: "fixture", domain: "127.0.0.1", path: "/" }]);
  await context.addInitScript(() => {
    localStorage.setItem("ag_token", "fixture");
    localStorage.setItem("auto-gallery-lang", "en");
  });
  await context.route("https://fonts.loli.net/**", (route) => route.fulfill({ status: 200, body: "" }));
  await context.route("**/api/v1/**", async (route) => {
    const url = new URL(route.request().url());
    const path = url.pathname;
    if (path === "/api/v1/auth/me" && route.request().method() === "GET") {
      return json(route, {
        id: 1,
        username: "works-controls",
        display_name: "Works Controls",
        is_admin: true,
        is_active: true,
        permissions: ["library", "curation", "system"],
        modules: { library: true, curation: true, system: true },
        preferences: options.preferences || {},
        nsfw_visible: true,
        upload_used_bytes: 0,
        must_change_password: false,
      });
    }
    if (path === "/api/v1/auth/me/preferences" && route.request().method() === "PUT") {
      const payload = route.request().postDataJSON() as { preferences: Record<string, unknown> };
      preferenceWrites.push(payload.preferences);
      return json(route, payload);
    }
    if (path === "/api/v1/auth/ws-ticket") return json(route, { detail: "offline" }, 503);
    if (path === "/api/v1/system/workbench") return json(route, workbench);
    if (path === "/api/v1/works/derivative-progress") {
      return json(route, { queued: 0, running: 0, failed: 0, complete: 0, remaining: 0, total: 0, affected_works: 0, completion_percent: 100, status: "idle" });
    }
    if (path === "/api/v1/search") {
      searchRequests += 1;
      const query = url.searchParams.get("q") || "";
      return json(route, {
        query,
        canonical_query: query,
        parsed: { raw: query, canonical: query, scope: "works", targets: ["works"], tokens: [] },
        groups: { works: { total: works.length, items: works, next_cursor: null, previous_cursor: null } },
        total: works.length,
        results: works,
        creators: [],
        tags: [],
        repositories: [],
        subscriptions: [],
      });
    }
    if (path === "/api/v1/search/assist") {
      return json(route, { query: "", canonical_query: "", parsed: { tokens: [] }, diagnostics: [], suggestions: [] });
    }
    return json(route, {});
  });
  return {
    searchRequests: () => searchRequests,
    preferenceWrites,
  };
}

test("desktop uses one anchored works panel and restores trigger focus", async ({ context, page }) => {
  await setup(context);
  await page.goto("/admin/works");

  const filter = page.getByRole("button", { name: "Filter", exact: true });
  const sort = page.getByRole("button", { name: "Sort", exact: true });
  const display = page.getByRole("button", { name: "Display", exact: true });
  await expect(filter).toBeVisible();
  await expect(sort).toBeVisible();
  await expect(display).toBeVisible();
  await expect(page.getByRole("button", { name: /Clear filters/i })).toHaveCount(0);

  await filter.click();
  await expect(page.getByRole("dialog", { name: "Filter works" })).toBeVisible();
  await sort.click();
  await expect(page.getByRole("dialog")).toHaveCount(1);
  await expect(page.getByRole("dialog", { name: "Sort works" })).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await expect(sort).toBeFocused();

  await display.click();
  await expect(page.getByRole("dialog", { name: "Display settings" })).toBeVisible();
  await page.getByRole("heading", { level: 1, name: "Works" }).click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await expect(display).toBeFocused();
});

test("mobile works panel is a focus-trapped bottom sheet", async ({ context, page }) => {
  await setup(context);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/admin/works");

  const trigger = page.getByRole("button", { name: "Display", exact: true });
  await trigger.click();
  const dialog = page.getByRole("dialog", { name: "Display settings" });
  await expect(dialog).toHaveAttribute("aria-modal", "true");
  await expect.poll(() => dialog.evaluate((node) => node.contains(document.activeElement))).toBe(true);
  await expect.poll(() => page.evaluate(() => document.body.style.overflow)).toBe("hidden");
  const box = await dialog.boundingBox();
  expect(box).not.toBeNull();
  expect(Math.abs((box!.y + box!.height) - 844)).toBeLessThan(3);

  await page.keyboard.press("Escape");
  await expect(dialog).toHaveCount(0);
  await expect.poll(() => page.evaluate(() => document.body.style.overflow)).toBe("");
  await expect(trigger).toBeFocused();
});

test("display defaults persist without re-requesting works", async ({ context, page }) => {
  const fixture = await setup(context);
  await page.goto("/admin/works");
  await expect(page.getByText("Harbor light")).toBeVisible();
  const initialSearches = fixture.searchRequests();

  await page.getByRole("button", { name: "Display", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "Display settings" });
  await expect(dialog.getByRole("button", { name: "Grid", exact: true })).toHaveAttribute("aria-pressed", "true");
  await expect(dialog.getByRole("button", { name: "Medium", exact: true })).toHaveAttribute("aria-pressed", "true");
  await expect(dialog.getByRole("checkbox", { name: "Show selection checkboxes" })).toBeChecked();
  await expect(dialog.getByRole("checkbox", { name: "Show AI badge" })).toBeChecked();
  await expect(dialog.getByRole("checkbox", { name: "Show NSFW badge" })).toBeChecked();
  await expect(dialog.getByRole("checkbox", { name: "Show favorite button" })).toBeChecked();
  await expect(dialog.getByRole("checkbox", { name: "Hover preview" })).toBeChecked();
  await expect(dialog.getByRole("checkbox", { name: "Blur NSFW thumbnails" })).toBeChecked();

  await dialog.getByRole("button", { name: "Masonry", exact: true }).click();
  await dialog.getByRole("button", { name: "Small", exact: true }).click();
  await dialog.getByRole("checkbox", { name: "Show AI badge" }).uncheck();
  await page.waitForTimeout(100);
  expect(fixture.searchRequests()).toBe(initialSearches);
  await expect.poll(() => page.evaluate(() => JSON.parse(localStorage.getItem("auto-gallery-appearance-v1") || "{}"))).toMatchObject({
    worksViewMode: "masonry",
    workCardSize: "small",
    workCardShowAi: false,
    blurNsfw: true,
  });

  await page.waitForTimeout(900);
  expect(fixture.preferenceWrites.at(-1)?.appearance).toMatchObject({
    worksViewMode: "masonry",
    workCardSize: "small",
    workCardShowAi: false,
  });
  await page.reload();
  await page.getByRole("button", { name: "Display", exact: true }).click();
  await expect(page.getByRole("dialog", { name: "Display settings" }).getByRole("button", { name: "Masonry", exact: true })).toHaveAttribute("aria-pressed", "true");
});

test("server appearance preferences hydrate older clients with safe defaults", async ({ context, page }) => {
  await setup(context, {
    preferences: {
      appearance: {
        worksViewMode: "list",
        workCardSize: "large",
        workCardShowAi: false,
      },
    },
  });
  await page.goto("/admin/works");

  await expect(page.locator('[data-works-layout="list"]')).toBeVisible();
  await expect(page.locator('[data-work-card="work-sfw"]')).toHaveAttribute("data-card-size", "large");
  await expect(page.locator('[data-work-card="work-sfw"]').getByText("AI", { exact: true })).toHaveCount(0);
  await page.getByRole("button", { name: "Display", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "Display settings" });
  await expect(dialog.getByRole("button", { name: "List", exact: true })).toHaveAttribute("aria-pressed", "true");
  await expect(dialog.getByRole("checkbox", { name: "Show NSFW badge" })).toBeChecked();
  await expect(dialog.getByRole("checkbox", { name: "Blur NSFW thumbnails" })).toBeChecked();
});

test("shared work cards apply visibility, size, and NSFW blur preferences", async ({ context, page }) => {
  const fixture = await setup(context);
  await page.goto("/admin/works");

  const sfwCard = page.locator('[data-work-card="work-sfw"]');
  const nsfwCard = page.locator('[data-work-card="work-nsfw"]');
  await expect(sfwCard).toHaveAttribute("data-card-size", "medium");
  await expect(sfwCard.getByRole("checkbox", { name: "Select work" })).toBeVisible();
  await expect(sfwCard.getByText("AI", { exact: true })).toBeVisible();
  await expect(sfwCard.getByRole("button", { name: "Unfavorite work" })).toBeVisible();
  await expect(nsfwCard.getByText("NSFW", { exact: true })).toBeVisible();
  await expect(nsfwCard.locator("[data-nsfw-blurred=true]")).toHaveCount(1);
  const initialSearches = fixture.searchRequests();

  await page.getByRole("button", { name: "Display", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "Display settings" });
  await dialog.getByRole("button", { name: "Large", exact: true }).click();
  await dialog.getByRole("checkbox", { name: "Show selection checkboxes" }).uncheck();
  await dialog.getByRole("checkbox", { name: "Show AI badge" }).uncheck();
  await dialog.getByRole("checkbox", { name: "Show NSFW badge" }).uncheck();
  await dialog.getByRole("checkbox", { name: "Show favorite button" }).uncheck();
  await dialog.getByRole("checkbox", { name: "Blur NSFW thumbnails" }).uncheck();

  await expect(sfwCard).toHaveAttribute("data-card-size", "large");
  await expect(sfwCard.getByRole("checkbox", { name: "Select work" })).toHaveCount(0);
  await expect(sfwCard.getByText("AI", { exact: true })).toHaveCount(0);
  await expect(sfwCard.getByRole("button", { name: "Unfavorite work" })).toHaveCount(0);
  await expect(nsfwCard.getByText("NSFW", { exact: true })).toHaveCount(0);
  await expect(nsfwCard.locator("[data-nsfw-blurred=true]")).toHaveCount(0);
  expect(fixture.searchRequests()).toBe(initialSearches);
});

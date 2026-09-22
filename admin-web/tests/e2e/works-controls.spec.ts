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
    thumbnail_asset_id: "asset-sfw",
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
    thumbnail_asset_id: "asset-nsfw",
    thumbnail_width: 800,
    thumbnail_height: 1200,
    source: "x",
  },
];

function queryParts(query: string): string[] {
  return [...(query.match(/(?:[^\s"]|"[^"]*")+/g) || [])];
}

function parsedTokens(query: string) {
  return queryParts(query).map((raw) => {
    const match = raw.match(/^(-?)([a-z_]+):(.*)$/i);
    if (!match) return { kind: "text", value: raw.replace(/^"|"$/g, "") };
    return {
      kind: "qualifier",
      key: match[2],
      value: match[3].replace(/^"|"$/g, ""),
      negated: match[1] === "-",
    };
  });
}

type Compose = {
  key: string;
  value?: string | null;
  operation?: "set" | "add" | "toggle" | "remove" | "replace-group";
  negated?: boolean;
  replace_values?: string[];
};

function composeQuery(query: string, operations: Compose[]) {
  let parts = queryParts(query);
  for (const compose of operations) {
    const operation = compose.operation || "set";
    const qualifier = (raw: string) => {
      const match = raw.match(/^(-?)([a-z_]+):(.*)$/i);
      return match ? { key: match[2], value: match[3].replace(/^"|"$/g, ""), negated: match[1] === "-" } : null;
    };
    const matchesKey = (raw: string) => {
      const parsed = qualifier(raw);
      return !!parsed && parsed.key === compose.key && parsed.negated === !!compose.negated;
    };
    const matchesValue = (raw: string) => {
      const parsed = qualifier(raw);
      return matchesKey(raw) && parsed?.value === compose.value;
    };
    if (operation === "set") {
      parts = parts.filter((raw) => !matchesKey(raw));
    } else if (operation === "replace-group") {
      parts = parts.filter((raw) => {
        const parsed = qualifier(raw);
        return !parsed || !matchesKey(raw) || !compose.replace_values?.includes(parsed.value);
      });
    } else if (operation === "remove") {
      parts = parts.filter((raw) => !matchesValue(raw));
    } else if (operation === "toggle" && parts.some(matchesValue)) {
      parts = parts.filter((raw) => !matchesValue(raw));
      continue;
    }
    if (compose.value && !parts.some(matchesValue)) {
      const value = /\s/.test(compose.value) ? `"${compose.value}"` : compose.value;
      parts.push(`${compose.negated ? "-" : ""}${compose.key}:${value}`);
    }
  }
  return parts.join(" ");
}

async function setup(
  context: BrowserContext,
  options: {
    preferences?: Record<string, unknown>;
    permissions?: string[];
    isAdmin?: boolean;
    nsfwVisible?: boolean;
  } = {},
) {
  let searchRequests = 0;
  const searchUrls: URL[] = [];
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
        is_admin: options.isAdmin ?? true,
        is_active: true,
        permissions: options.permissions || ["library", "curation", "system"],
        modules: { library: true, curation: options.permissions?.includes("curation") ?? true, system: options.permissions?.includes("system") ?? true },
        preferences: options.preferences || {},
        nsfw_visible: options.nsfwVisible ?? true,
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
      searchUrls.push(url);
      const query = url.searchParams.get("q") || "";
      return json(route, {
        query,
        canonical_query: query,
        parsed: { raw: query, canonical: query, scope: "works", targets: ["works"], tokens: parsedTokens(query) },
        groups: { works: { total: works.length, items: works, next_cursor: null, previous_cursor: null } },
        total: works.length,
        seed: url.searchParams.has("seed") ? Number(url.searchParams.get("seed")) : null,
        results: works,
        creators: [],
        tags: [],
        repositories: [],
        subscriptions: [],
      });
    }
    if (path === "/api/v1/search/assist") {
      const payload = route.request().postDataJSON() as { before_cursor?: string; compose?: Compose; composes?: Compose[] };
      const query = payload.before_cursor || "";
      const operations = payload.composes || (payload.compose ? [payload.compose] : []);
      const canonical = operations.length ? composeQuery(query, operations) : query;
      return json(route, { query: canonical, canonical_query: canonical, parsed: { tokens: parsedTokens(canonical) }, diagnostics: [], suggestions: [] });
    }
    if (/^\/api\/v1\/works\/[^/]+\/assets$/.test(path)) {
      const nsfw = path.includes("work-nsfw");
      return json(route, [{
        id: nsfw ? "asset-nsfw" : "asset-sfw",
        file_name: "preview.png",
        mime_type: "image/png",
        width: nsfw ? 800 : 1200,
        height: nsfw ? 1200 : 800,
        preview_url: "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw==",
        derivative_status: "ready",
      }]);
    }
    if (path.startsWith("/api/v1/media/")) {
      return route.fulfill({ status: 200, contentType: "image/gif", body: Buffer.from("R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw==", "base64") });
    }
    return json(route, {});
  });
  return {
    searchRequests: () => searchRequests,
    searchUrls,
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
  const renderPhaseWarnings: string[] = [];
  page.on("console", (message) => {
    if (message.type() === "error" && message.text().includes("Cannot update a component")) {
      renderPhaseWarnings.push(message.text());
    }
  });
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
  expect(renderPhaseWarnings).toEqual([]);
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

test("filter draft cancels cleanly and replaces only controlled qualifier groups", async ({ context, page }) => {
  await setup(context);
  const original = 'cat tag:sky creator:"Ada Lovelace" after:2025-01-01 source:x is:nsfw is:ai sort:title-asc';
  await page.goto("/admin/works?q=" + encodeURIComponent(original));

  await page.getByRole("button", { name: "Filter", exact: true }).click();
  let dialog = page.getByRole("dialog", { name: "Filter works" });
  await expect(dialog.getByRole("checkbox", { name: "X", exact: true })).toBeChecked();
  await dialog.getByRole("checkbox", { name: "Pixiv" }).check();
  await dialog.getByRole("button", { name: "Cancel" }).click();
  expect(new URL(page.url()).searchParams.get("q")).toBe(original);

  await page.getByRole("button", { name: "Filter", exact: true }).click();
  dialog = page.getByRole("dialog", { name: "Filter works" });
  await expect(dialog.getByRole("checkbox", { name: "Pixiv" })).not.toBeChecked();
  await dialog.getByRole("checkbox", { name: "Pixiv" }).check();
  await dialog.getByRole("radio", { name: "SFW", exact: true }).click();
  await dialog.getByRole("radio", { name: "Non-AI" }).click();
  await dialog.getByRole("checkbox", { name: "Favorites only" }).check();
  await dialog.getByRole("checkbox", { name: "Image", exact: true }).check();
  await dialog.getByRole("checkbox", { name: "Video" }).check();
  await dialog.getByRole("button", { name: "Apply" }).click();

  await expect.poll(() => new URL(page.url()).searchParams.get("q") || "").toContain("source:pixiv");
  const query = new URL(page.url()).searchParams.get("q") || "";
  expect(query).toContain("cat");
  expect(query).toContain("tag:sky");
  expect(query).toContain('creator:"Ada Lovelace"');
  expect(query).toContain("after:2025-01-01");
  expect(query).toContain("source:x");
  expect(query).toContain("source:pixiv");
  expect(query).toContain("is:sfw");
  expect(query).toContain("is:human");
  expect(query).toContain("is:favorite");
  expect(query).toContain("has:image");
  expect(query).toContain("has:video");
  expect(query).toContain("sort:title-asc");
  expect(query).not.toContain("is:nsfw");
  expect(query).not.toContain("is:ai");
});

test("sort options manage stable random seeds and relevance availability", async ({ context, page }) => {
  const fixture = await setup(context);
  await page.goto("/admin/works?q=" + encodeURIComponent("tag:sky"));
  await page.getByRole("button", { name: "Sort", exact: true }).click();
  await expect(page.getByRole("dialog", { name: "Sort works" }).getByRole("radio", { name: "Relevance" })).toHaveCount(0);
  await page.keyboard.press("Escape");

  await page.getByRole("combobox", { name: "Search title..." }).fill("cat tag:sky");
  await expect.poll(() => new URL(page.url()).searchParams.get("q")).toBe("cat tag:sky");
  await page.getByRole("button", { name: "Sort", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "Sort works" });
  await expect(dialog.getByRole("radio", { name: "Relevance" })).toBeVisible();
  await dialog.getByRole("radio", { name: "Popularity" }).click();
  await expect.poll(() => new URL(page.url()).searchParams.get("q") || "").toContain("sort:heat-desc");
  expect(new URL(page.url()).searchParams.has("seed")).toBe(false);

  await dialog.getByRole("radio", { name: "Random" }).click();
  await expect.poll(() => new URL(page.url()).searchParams.get("seed")).toMatch(/^\d+$/);
  const firstSeed = new URL(page.url()).searchParams.get("seed");
  expect(Number(firstSeed)).toBeLessThanOrEqual(0xffffffff);
  await expect.poll(() => fixture.searchUrls.at(-1)?.searchParams.get("seed")).toBe(firstSeed);
  await dialog.getByRole("button", { name: "Reshuffle" }).click();
  await expect.poll(() => new URL(page.url()).searchParams.get("seed")).not.toBe(firstSeed);
  const secondSeed = new URL(page.url()).searchParams.get("seed");
  await expect.poll(() => fixture.searchUrls.at(-1)?.searchParams.get("seed")).toBe(secondSeed);

  await dialog.getByRole("radio", { name: "Published · Oldest first" }).click();
  await expect.poll(() => new URL(page.url()).searchParams.get("q") || "").toContain("sort:posted-asc");
  expect(new URL(page.url()).searchParams.has("seed")).toBe(false);
});

test("search clear resets query paging and random seed but preserves display preferences", async ({ context, page }) => {
  await setup(context, { preferences: { appearance: { worksViewMode: "masonry", workCardSize: "large" } } });
  await page.goto("/admin/works?q=" + encodeURIComponent("cat sort:random") + "&p=2&seed=123&view=list");
  await expect(page.locator('[data-works-layout="list"]')).toBeVisible();
  await page.getByRole("button", { name: "Clear search" }).click();
  await expect.poll(() => new URL(page.url()).searchParams.has("q")).toBe(false);
  const params = new URL(page.url()).searchParams;
  expect(params.has("p")).toBe(false);
  expect(params.has("seed")).toBe(false);
  expect(params.get("view")).toBe("list");
  await page.getByRole("button", { name: "Display", exact: true }).click();
  await expect(page.getByRole("dialog", { name: "Display settings" }).getByRole("button", { name: "Large", exact: true })).toHaveAttribute("aria-pressed", "true");
});

test("legacy view overrides the saved layout until an explicit layout choice", async ({ context, page }) => {
  await setup(context, { preferences: { appearance: { worksViewMode: "masonry" } } });
  await page.goto("/admin/works?view=list");
  await expect(page.locator('[data-works-layout="list"]')).toBeVisible();
  await page.getByRole("button", { name: "Display", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "Display settings" });
  await expect(dialog.getByRole("button", { name: "List", exact: true })).toHaveAttribute("aria-pressed", "true");
  await dialog.getByRole("button", { name: "Grid", exact: true }).click();
  await expect(page.locator('[data-works-layout="grid"]')).toBeVisible();
  expect(new URL(page.url()).searchParams.has("view")).toBe(false);
  await expect.poll(() => page.evaluate(() => JSON.parse(localStorage.getItem("auto-gallery-appearance-v1") || "{}").worksViewMode)).toBe("grid");
});

test("hiding checkboxes clears selection while trash actions and permissions stay authoritative", async ({ context, page }) => {
  await setup(context);
  await page.goto("/admin/works");
  await page.locator('[data-work-card="work-sfw"]').getByRole("checkbox", { name: "Select work" }).check();
  await expect(page.getByText("1 works selected")).toBeVisible();
  await page.getByRole("button", { name: "Display", exact: true }).click();
  await page.getByRole("dialog", { name: "Display settings" }).getByRole("checkbox", { name: "Show selection checkboxes" }).uncheck();
  await expect(page.getByText("1 works selected")).toHaveCount(0);
  await expect(page.getByText("Selection cleared because selection checkboxes are hidden.")).toBeVisible();

  await page.goto("/admin/works?q=" + encodeURIComponent("is:trashed"));
  await expect(page.getByRole("button", { name: "Restore" }).first()).toBeVisible();
  await expect(page.getByRole("button", { name: "Purge" }).first()).toBeVisible();

  const restricted = await context.browser()!.newContext();
  await setup(restricted, { permissions: ["library"], isAdmin: false });
  const restrictedPage = await restricted.newPage();
  await restrictedPage.goto("/admin/works");
  await expect(restrictedPage.getByRole("checkbox", { name: "Select work" })).toHaveCount(0);
  await expect(restrictedPage.getByRole("button", { name: /Favorite work|Unfavorite work/ })).toHaveCount(0);
  await restricted.close();
});

test("NSFW blur also covers the desktop hover preview independently of its badge", async ({ context, page }) => {
  await setup(context);
  await page.goto("/admin/works");
  const card = page.locator('[data-work-card="work-nsfw"]');
  await card.hover();
  await expect(page.locator('.popover > [data-nsfw-blurred="true"]')).toBeVisible();

  await page.getByRole("button", { name: "Display", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "Display settings" });
  await dialog.getByRole("checkbox", { name: "Show NSFW badge" }).uncheck();
  await dialog.getByRole("checkbox", { name: "Blur NSFW thumbnails" }).uncheck();
  await page.keyboard.press("Escape");
  await card.hover();
  await expect(page.locator('.popover > [data-nsfw-blurred="true"]')).toHaveCount(0);
  await expect(card.getByText("NSFW", { exact: true })).toHaveCount(0);
});

import { expect, test, type BrowserContext, type Page } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";

const TOTAL = 812;
const acceptanceHost = new URL(
  process.env.PLAYWRIGHT_BASE_URL || "http://127.0.0.1:13000",
).hostname;

const me = {
  id: 17,
  username: "reference-list-review",
  display_name: "Reference List Review",
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

function parsedTokens(query: string) {
  return query.trim().split(/\s+/).filter(Boolean).map((value, index) => {
    const separator = value.indexOf(":");
    if (separator < 0) {
      return {
        kind: "text",
        value,
        quoted: false,
        start: index,
        end: index + value.length,
      };
    }
    const rawKey = value.slice(0, separator);
    return {
      kind: "qualifier",
      key: rawKey.replace(/^-/, ""),
      value: value.slice(separator + 1),
      negated: rawKey.startsWith("-"),
      quoted: false,
      start: index,
      end: index + value.length,
    };
  });
}

function composeQuery(current: string, composes: Array<Record<string, unknown>>) {
  let parts = current.trim().split(/\s+/).filter(Boolean);
  for (const compose of composes) {
    const key = String(compose.key || "");
    const operation = String(compose.operation || "set");
    const replaceValues = new Set(
      Array.isArray(compose.replace_values)
        ? compose.replace_values.map(String)
        : [],
    );
    if (operation === "replace-group") {
      parts = parts.filter((part) => {
        const normalized = part.replace(/^-/, "");
        const [partKey, partValue] = normalized.split(":", 2);
        return partKey !== key || !replaceValues.has(partValue);
      });
    } else {
      parts = parts.filter((part) => part.replace(/^-/, "").split(":", 1)[0] !== key);
    }
    if (compose.value) {
      const prefix = compose.negated ? "-" : "";
      parts.push(`${prefix}${key}:${String(compose.value)}`);
    }
  }
  return parts.join(" ");
}

function creatorAt(index: number) {
  return {
    id: `creator-${String(index).padStart(4, "0")}`,
    name: `creator_${String(index).padStart(4, "0")}`,
    display_name: `Creator ${String(index).padStart(3, "0")}`,
    description: `Reference creator ${index}`,
    is_active: index % 3 !== 0,
    is_favorite: false,
    subscription_count: 1,
    source_count: 2,
    repository_count: 2,
    last_synced_at: null,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-08-01T00:00:00Z",
  };
}

function subscriptionAt(index: number) {
  return {
    id: `subscription-${String(index).padStart(4, "0")}`,
    creator_id: `creator-${String(index).padStart(4, "0")}`,
    creator_name: `Creator ${String(index).padStart(3, "0")}`,
    creator_display_name: `Creator ${String(index).padStart(3, "0")}`,
    name: `Custom title ${String(TOTAL - index).padStart(3, "0")}`,
    is_active: index % 3 !== 0,
    sync_enabled: true,
    sync_interval_hours: 6,
    schedule_mode: null,
    scheduled_times: null,
    last_synced_at: null,
    source_count: 2,
    enabled_source_count: 2,
    running_job_count: 0,
    failed_job_count: 0,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-08-01T00:00:00Z",
  };
}

async function installRoutes(
  context: BrowserContext,
  observations: {
    searches: Array<{ scope: string; offset: number; limit: number; q: string }>;
    summaryBatches: string[][];
    failSearchOnce?: Set<string>;
    failSearch?: Set<string>;
    holdCompose?: () => Promise<void>;
    holdSearch?: (scope: string, offset: number) => Promise<void>;
    deletedIds?: Set<string>;
    total?: number;
  },
  theme: "dark" | "light" = "dark",
) {
  await context.addCookies([{
    name: "ag_session",
    value: "reference-list-token",
    domain: acceptanceHost,
    path: "/",
  }, { name: "ag_csrf", value: "fixture-csrf", url: process.env.PLAYWRIGHT_BASE_URL || "http://127.0.0.1:13000" }]);
  await context.addInitScript((selectedTheme) => {
    localStorage.setItem("ag_token", "reference-list-token");
    localStorage.setItem("auto-gallery-lang", "en");
    localStorage.setItem("auto-gallery-theme", selectedTheme);
  }, theme);
  await context.route("**/api/v1/**", async (route) => {
    const url = new URL(route.request().url());
    const path = url.pathname;
    if (path === "/api/v1/auth/me") {
      await route.fulfill({ json: me });
      return;
    }
    if (path === "/api/v1/search/assist") {
      const body = route.request().postDataJSON() as {
        before_cursor?: string;
        composes?: Array<Record<string, unknown>>;
        compose?: Record<string, unknown>;
        scope?: string;
      };
      const composes = body.composes || (body.compose ? [body.compose] : []);
      const query = composeQuery(body.before_cursor || "", composes);
      if (composes.length) await observations.holdCompose?.();
      await route.fulfill({ json: {
        query,
        canonical_query: query,
        parsed: {
          raw: query,
          canonical: query,
          scope: body.scope || "global",
          targets: [body.scope || "global"],
          tokens: parsedTokens(query),
        },
        diagnostics: [],
        suggestions: [],
        catalog: [],
      } });
      return;
    }
    if (path === "/api/v1/search/name-anchors") {
      const q = url.searchParams.get("q") || "";
      const direction = q.includes("sort:name-desc") ? "desc" : "asc";
      const definitions = [
        ...Array.from({ length: 26 }, (_, index) => ({
          key: String.fromCharCode(65 + index),
          label: String.fromCharCode(65 + index),
          kind: "latin",
          offset: index === 0 ? 0 : index === 12 ? 300 : null,
          count: index === 0 ? 300 : index === 12 ? 512 : 0,
        })),
        { key: "0-9", label: "0–9", kind: "digit", offset: null, count: 0 },
        { key: "kana", label: "かな", kind: "kana", offset: null, count: 0 },
        { key: "han", label: "汉", kind: "han", offset: null, count: 0 },
        { key: "other", label: "#", kind: "other", offset: null, count: 0 },
      ];
      await route.fulfill({ json: {
        scope: url.searchParams.get("scope"),
        direction,
        total: TOTAL,
        items: direction === "desc" ? definitions.toReversed() : definitions,
      } });
      return;
    }
    if (path === "/api/v1/search") {
      const scope = url.searchParams.get("scope") || "creators";
      const offset = Number(url.searchParams.get("offset") || 0);
      const limit = Number(url.searchParams.get("limit") || 20);
      const q = url.searchParams.get("q") || "";
      observations.searches.push({ scope, offset, limit, q });
      await observations.holdSearch?.(scope, offset);
      const failKey = `${scope}:${offset}`;
      if (observations.failSearch?.has(failKey) || observations.failSearchOnce?.delete(failKey)) {
        await route.fulfill({ status: 503, json: { detail: "Temporary batch failure" } });
        return;
      }
      const scopedTotal = scope === "works" ? 0 : observations.total ?? TOTAL;
      const items = scope === "works" ? [] : Array.from(
        { length: Math.max(0, Math.min(limit, scopedTotal - offset)) },
        (_, index) => scope === "subscriptions"
          ? subscriptionAt(offset + index)
          : creatorAt(offset + index),
      ).filter((item) => !observations.deletedIds?.has(item.id));
      await route.fulfill({ json: {
        query: q,
        canonical_query: q,
        parsed: {
          raw: q,
          canonical: q,
          scope,
          targets: [scope],
          tokens: parsedTokens(q),
        },
        groups: { [scope]: { total: scopedTotal, items } },
        total: scopedTotal,
        results: [],
        creators: scope === "creators" ? items : [],
        tags: [],
        repositories: [],
        subscriptions: scope === "subscriptions" ? items : [],
        execution: {
          winner: "postgresql",
          hedged: false,
          consistency: "authoritative",
          index_status: "not_needed",
          elapsed_ms: 4,
        },
      } });
      return;
    }
    if (path === "/api/v1/subscriptions/summaries") {
      const ids = (url.searchParams.get("ids") || "").split(",").filter(Boolean);
      observations.summaryBatches.push(ids);
      await route.fulfill({ json: {
        updated_at: "2026-08-01T00:00:00Z",
        items: ids.map((subscriptionId) => ({
          subscription_id: subscriptionId,
          latest_state: { state: "never_synced", status: null },
          active_count: 0,
          attention_count: 0,
          source_count: 2,
          enabled_source_count: 2,
          schedule: {
            configured_mode: "inherit",
            effective_mode: "interval",
            inherited: true,
            timezone: "UTC",
            scheduled_times: null,
            schedule_rule: null,
            sync_interval_hours: 6,
            next_due_at: null,
            oldest_due_at: null,
            due_sources: 0,
            overdue_sources: 0,
            blocked_sources: 0,
          },
        })),
      } });
      return;
    }
    if (path === "/api/v1/operations/overview") {
      await route.fulfill({ json: {
        view: "attention",
        total: 0,
        summary: {
          attention: 0,
          critical: 0,
          warning: 0,
          resolved: 0,
          active: 0,
          resource_limited: 0,
        },
        items: [],
      } });
      return;
    }
    if (path === "/api/v1/system/workbench") {
      await route.fulfill({ json: {
        updated_at: "2026-08-01T00:00:00Z",
        queue: {},
        scheduler: {},
        storage: {},
        health: {},
        attention: {},
        recent: {},
      } });
      return;
    }
    if (path === "/api/v1/system/scheduler-decisions") {
      await route.fulfill({ json: {
        updated_at: "2026-08-01T00:00:00Z",
        scheduler_enabled: true,
        timezone: "UTC",
        total: 0,
        items: [],
      } });
      return;
    }
    if (path === "/api/v1/creators/count" || path === "/api/v1/subscriptions/count") {
      await route.fulfill({ json: { count: TOTAL } });
      return;
    }
    const creatorDetail = path.match(/^\/api\/v1\/creators\/creator-(\d{4})$/);
    if (creatorDetail && route.request().method() === "GET") {
      await route.fulfill({ json: creatorAt(Number(creatorDetail[1])) });
      return;
    }
    if (/^\/api\/v1\/creators\/creator-\d{4}\/links$/.test(path)) {
      await route.fulfill({ json: [] });
      return;
    }
    if (/^\/api\/v1\/creators\/creator-\d{4}\/stats$/.test(path)) {
      await route.fulfill({ json: {
        creator_id: "creator-0050",
        total_works: 0,
        total_assets: 0,
        total_tags: 0,
        source_breakdown: [],
        tag_distribution: [],
        monthly_frequency: [],
      } });
      return;
    }
    if (/^\/api\/v1\/creators\/creator-\d{4}\/timeline$/.test(path)) {
      await route.fulfill({ json: { creator_id: "creator-0050", sources: [], days: [], total: 0 } });
      return;
    }
    if (/^\/api\/v1\/creators\/creator-\d{4}\/subscription-overview$/.test(path)) {
      await route.fulfill({ json: {
        creator_id: "creator-0050",
        subscriptions: [],
        repositories: [],
        summary: { subscription_count: 0, repository_count: 0, enabled_repository_count: 0, running_job_count: 0 },
      } });
      return;
    }
    if (/^\/api\/v1\/creators\/creator-\d{4}\/references$/.test(path)) {
      await route.fulfill({ json: { pixiv: [], danbooru: null } });
      return;
    }
    await route.fulfill({ json: {} });
  });
}

test("creator and subscription lists share virtual batches, sorting, and name anchors", async ({ context, page }) => {
  const observations = { searches: [], summaryBatches: [] } as {
    searches: Array<{ scope: string; offset: number; limit: number; q: string }>;
    summaryBatches: string[][];
  };
  await installRoutes(context, observations, "light");
  await page.setViewportSize({ width: 1440, height: 900 });

  for (const scope of ["creators", "subscriptions"] as const) {
    observations.searches.length = 0;
    await page.goto(`/admin/${scope}`);
    await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
    await expect(page.locator("[data-virtual-reference-list]")).toBeVisible();
    await expect.poll(() => observations.searches.some(
      (request) => request.scope === scope && request.offset === 0 && request.limit === 50,
    )).toBe(true);
    await expect(page.getByRole("navigation", { name: "Pagination" })).toHaveCount(0);
    await expect(page.getByRole("group", { name: "Sort" })).toBeVisible();
    await expect(page.getByRole("navigation", { name: "Name index" })).toBeVisible();

    await page.getByRole("button", { name: "M, 512 results" }).click();
    await expect.poll(() => observations.searches.some(
      (request) => request.scope === scope && request.offset === 300 && request.limit === 50,
    )).toBe(true);
    await expect(page.getByText(scope === "creators" ? "Creator 300" : "Custom title 512", { exact: true })).toBeVisible();
    expect(await page.locator("[data-virtual-index]").count()).toBeLessThan(100);

    await page.getByRole("button", { name: "Updated" }).click();
    await expect(page).toHaveURL(/q=sort%3Aupdated-desc/);
    await expect(page.getByRole("navigation", { name: "Name index" })).toHaveCount(0);
    await expect.poll(() => observations.searches.some(
      (request) => request.scope === scope && request.q === "sort:updated-desc" && request.offset === 0,
    )).toBe(true);

    await page.getByRole("button", { name: "Active", exact: true }).click();
    await expect.poll(() => new URL(page.url()).searchParams.get("q")).toContain("is:active");
    await expect.poll(() => new URL(page.url()).searchParams.get("q")).toContain("sort:updated-desc");
    await page.getByRole("button", { name: /Updated/ }).click();
    await expect.poll(() => new URL(page.url()).searchParams.get("q")).toContain("sort:updated-asc");
  }
});

test("starting to edit supersedes a pending subscription filter before the first keystroke", async ({ context, page }) => {
  let release!: () => void;
  const pending = new Promise<void>((resolve) => { release = resolve; });
  let held = false;
  await installRoutes(context, {
    searches: [], summaryBatches: [],
    holdCompose: async () => { held = true; await pending; },
  });
  await page.goto("/admin/subscriptions?q=original");
  const input = page.getByRole("combobox");
  await expect(input).toHaveValue("original");
  try {
    await page.getByRole("button", { name: "Active", exact: true }).click();
    await expect.poll(() => held).toBe(true);
    await input.focus();
    await input.press("ControlOrMeta+A");
  } finally {
    release();
  }
  await page.waitForTimeout(650);
  await page.keyboard.insertText("newer search");
  await expect(input).toHaveValue("newer search");
  await expect.poll(() => new URL(page.url()).searchParams.get("q")).toBe("newer search");
});

test("subscription typing supersedes a pending filter composition", async ({ context, page }) => {
  let release!: () => void;
  const pending = new Promise<void>((resolve) => { release = resolve; });
  let held = false;
  const observations = {
    searches: [] as Array<{ scope: string; offset: number; limit: number; q: string }>,
    summaryBatches: [] as string[][],
    holdCompose: async () => { held = true; await pending; },
  };
  await installRoutes(context, observations);
  await page.goto("/admin/subscriptions");
  const input = page.getByRole("combobox");
  await expect(input).toBeVisible();
  try {
    await page.getByRole("button", { name: "Active", exact: true }).click();
    await expect.poll(() => held).toBe(true);
    await input.fill("newer search");
    await expect.poll(() => new URL(page.url()).searchParams.get("q")).toBe("newer search");
  } finally {
    release();
  }
  // Let the released response and any incorrectly scheduled debounce finish.
  await page.waitForTimeout(650);
  await expect(input).toHaveValue("newer search");
  await expect.poll(() => new URL(page.url()).searchParams.get("q")).toBe("newer search");
  await expect(page.getByRole("button", { name: "All", exact: true })).toHaveClass(/segment-active/);
});

test("creator input survives an earlier filter navigation", async ({ context, page }) => {
  const observations = { searches: [], summaryBatches: [] } as {
    searches: Array<{ scope: string; offset: number; limit: number; q: string }>;
    summaryBatches: string[][];
  };
  await installRoutes(context, observations);
  await page.goto("/admin/creators");
  const input = page.getByRole("combobox");
  await expect(input).toBeVisible();
  let release!: () => void;
  const pending = new Promise<void>((resolve) => { release = resolve; });
  let held = false;
  await page.route((url) => url.pathname === "/admin/creators"
    && url.searchParams.has("_rsc") && url.searchParams.get("q") === "is:active", async (route) => {
    const response = await route.fetch();
    held = true;
    await pending;
    await route.fulfill({ response });
  });
  try {
    await page.getByRole("button", { name: "Active", exact: true }).click();
    await expect.poll(() => held).toBe(true);
    await input.fill("newer creator search");
  } finally {
    release();
  }
  await page.waitForTimeout(650);
  await expect(input).toHaveValue("newer creator search");
  await expect.poll(() => new URL(page.url()).searchParams.get("q")).toBe("newer creator search");
  await expect(page.getByRole("button", { name: "All", exact: true })).toHaveClass(/segment-active/);
});

for (const query of ["new search", ""]) {
  test(`subscription input survives an earlier filter navigation: ${query || "clear search"}`, async ({ context, page }) => {
    const observations = { searches: [], summaryBatches: [] } as {
      searches: Array<{ scope: string; offset: number; limit: number; q: string }>;
      summaryBatches: string[][];
    };
    await installRoutes(context, observations);
    await page.goto("/admin/subscriptions");
    const input = page.getByRole("combobox");
    await expect(input).toBeVisible();
    let release!: () => void;
    const pending = new Promise<void>((resolve) => { release = resolve; });
    let held = false;
    await page.route((url) => url.pathname === "/admin/subscriptions"
      && url.searchParams.has("_rsc") && url.searchParams.get("q") === "is:active", async (route) => {
      const response = await route.fetch();
      held = true;
      await pending;
      await route.fulfill({ response });
    });
    try {
      await page.getByRole("button", { name: "Active", exact: true }).click();
      await expect.poll(() => held).toBe(true);
      await input.fill(query);
    } finally {
      release();
    }
    // The search field writes to the URL after its 300 ms debounce. An empty
    // query can reuse the initial list cache, so an API observation alone does
    // not prove that the replacement navigation has committed.
    await page.waitForTimeout(650);
    await expect(input).toHaveValue(query);
    await expect.poll(() => new URL(page.url()).searchParams.get("q") ?? "").toBe(query);
    // Returning to the empty query can reuse the fresh virtual-list cache.
    await expect.poll(() => observations.searches.some((request) => request.q === query)).toBe(true);
    await expect(page.getByRole("button", { name: "All", exact: true })).toHaveClass(/segment-active/);
    await expect(input).toHaveValue(query);

    // Native same-document navigation is supported by Next's router. Verify
    // it still restores the input, including a back action before debounce.
    await page.evaluate(() => history.pushState(null, "", "?q=external-query"));
    await expect(input).toHaveValue("external-query");
    await input.fill("unsubmitted draft");
    await page.goBack();
    await expect(input).toHaveValue(query);
    await page.goForward();
    await expect(input).toHaveValue("external-query");
    await expect.poll(() => observations.searches.some((request) => request.q === "external-query")).toBe(true);
  });
}

test("selection only covers loaded rows and subscription summaries stay page-sized", async ({ context, page }) => {
  const observations = { searches: [], summaryBatches: [] } as {
    searches: Array<{ scope: string; offset: number; limit: number; q: string }>;
    summaryBatches: string[][];
  };
  await installRoutes(context, observations);
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto("/admin/subscriptions");
  await expect(page.locator("[data-virtual-reference-list]")).toBeVisible();
  const selectAll = page.getByRole("checkbox", { name: "Select all" });
  await selectAll.check();
  await expect(selectAll).toBeChecked();
  await expect(page.getByText("50 selected / 50 loaded")).toBeVisible();

  await page.getByRole("button", { name: "M, 512 results" }).click();
  await expect.poll(() => observations.searches.some(
    (request) => request.scope === "subscriptions" && request.offset === 300,
  )).toBe(true);
  await expect(page.getByText(/50 selected \/ (100|150) loaded/)).toBeVisible();
  expect(observations.summaryBatches.length).toBeGreaterThan(0);
  expect(observations.summaryBatches.every((batch) => batch.length <= 50)).toBe(true);
  expect(observations.summaryBatches.some((batch) => batch.includes("subscription-0300"))).toBe(true);

  const checkbox = page.getByRole("checkbox", { name: "Select all" });
  const inputBox = await checkbox.boundingBox();
  const wrapper = checkbox.locator("xpath=..");
  const hitbox = await wrapper.boundingBox();
  const visual = await wrapper.locator(".compact-selection-visual").boundingBox();
  expect(visual?.width).toBe(16);
  expect(visual?.height).toBe(16);
  expect(inputBox?.width).toBeGreaterThanOrEqual(24);
  expect(inputBox?.height).toBeGreaterThanOrEqual(24);
  expect(hitbox?.width).toBeGreaterThanOrEqual(24);
  expect(hitbox?.height).toBeGreaterThanOrEqual(24);
  await checkbox.click({ position: { x: 1, y: 1 } });
  await expect(checkbox).toBeChecked();
});

test("creator filter and sort cancel stale search debounce before selection", async ({ context, page }) => {
  test.setTimeout(60_000);
  const observations = { searches: [], summaryBatches: [] } as {
    searches: Array<{ scope: string; offset: number; limit: number; q: string }>;
    summaryBatches: string[][];
  };
  await installRoutes(context, observations);
  await context.addInitScript(() => {
    const nativeSetTimeout = window.setTimeout.bind(window);
    window.setTimeout = ((handler: TimerHandler, timeout?: number, ...args: unknown[]) =>
      nativeSetTimeout(handler, timeout === 300 ? 5_000 : timeout, ...args)) as typeof window.setTimeout;
  });
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto("/admin/creators?q=selection-race");
  await expect(page.locator("[data-virtual-reference-list]")).toBeVisible();

  const activeSearch = page.waitForResponse((response) => {
    const url = new URL(response.url());
    return url.pathname === "/api/v1/search"
      && url.searchParams.get("scope") === "creators"
      && (url.searchParams.get("q") || "").includes("is:active");
  });
  await page.getByRole("button", { name: "Active", exact: true }).click();
  await activeSearch;
  const sortAssist = page.waitForResponse((response) =>
    new URL(response.url()).pathname === "/api/v1/search/assist",
  );
  await page.getByRole("button", { name: "Name · Ascending", exact: true }).click();
  await sortAssist;
  await expect.poll(() => new URL(page.url()).searchParams.get("q") || "").toContain("sort:name-desc");

  const rowSelection = page.getByRole("checkbox", { name: "Select Creator 000" });
  await rowSelection.click({ force: true });
  await expect(rowSelection).toBeChecked();
  await page.waitForTimeout(5_500);
  await expect(rowSelection).toBeChecked();
  await expect(page.getByText(/1 selected \/ \d+ loaded/)).toBeVisible();
});

test("creator create dispatches once when the pending button is double-clicked", async ({ context, page }) => {
  test.setTimeout(60_000);
  const observations = { searches: [], summaryBatches: [] } as {
    searches: Array<{ scope: string; offset: number; limit: number; q: string }>;
    summaryBatches: string[][];
  };
  await installRoutes(context, observations);
  let createRequests = 0;
  let releaseCreate: (() => void) | undefined;
  const heldCreate = new Promise<void>((resolve) => { releaseCreate = resolve; });
  await context.route("**/api/v1/creators", async (route) => {
    if (route.request().method() !== "POST") return route.fallback();
    createRequests += 1;
    await heldCreate;
    await route.fulfill({
      status: 201,
      contentType: "application/json",
      body: JSON.stringify({
        id: "creator-created-once",
        name: "double-click-creator",
        display_name: "Double Click Creator",
        description: "",
        is_active: true,
        is_favorite: false,
        danbooru_artist_id: null,
        repository_count: 0,
        source_count: 0,
        subscription_count: 0,
      }),
    });
  });

  await page.goto("/admin/creators");
  await page.getByRole("button", { name: "+ New", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "New Creator", exact: true });
  await dialog.getByPlaceholder("Creator name", { exact: true }).fill("double-click-creator");
  await dialog.getByPlaceholder("Optional display name", { exact: true }).fill("Double Click Creator");
  const create = dialog.getByRole("button", { name: "Create", exact: true });
  const doubleClick = create.evaluate((button: HTMLButtonElement) => {
    button.click();
    button.click();
  });
  await expect.poll(() => createRequests).toBeGreaterThan(0);
  await page.waitForTimeout(150);
  expect(createRequests).toBe(1);
  releaseCreate?.();
  await doubleClick;
});

test("only the visible subscription batch keeps polling summaries", async ({ context, page }) => {
  const observations = { searches: [], summaryBatches: [] } as {
    searches: Array<{ scope: string; offset: number; limit: number; q: string }>;
    summaryBatches: string[][];
  };
  await installRoutes(context, observations);
  await page.clock.install();
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto("/admin/subscriptions");
  await expect.poll(() => observations.summaryBatches.some(
    (batch) => batch.includes("subscription-0000"),
  )).toBe(true);

  await page.getByRole("button", { name: "M, 512 results" }).click();
  await expect.poll(() => observations.summaryBatches.some(
    (batch) => batch.includes("subscription-0300"),
  )).toBe(true);
  const initialBatchPolls = observations.summaryBatches.filter(
    (batch) => batch.includes("subscription-0000"),
  ).length;
  const visibleBatchPolls = observations.summaryBatches.filter(
    (batch) => batch.includes("subscription-0300"),
  ).length;

  await page.clock.fastForward(61_000);
  await expect.poll(() => observations.summaryBatches.filter(
    (batch) => batch.includes("subscription-0300"),
  ).length).toBeGreaterThan(visibleBatchPolls);
  expect(observations.summaryBatches.filter(
    (batch) => batch.includes("subscription-0000"),
  )).toHaveLength(initialBatchPolls);
});

test("a failed offscreen batch retries in place without replacing loaded rows", async ({ context, page }) => {
  const observations = {
    searches: [],
    summaryBatches: [],
    failSearchOnce: new Set(["creators:300"]),
  } as {
    searches: Array<{ scope: string; offset: number; limit: number; q: string }>;
    summaryBatches: string[][];
    failSearchOnce: Set<string>;
  };
  await installRoutes(context, observations);
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto("/admin/creators");
  await expect(page.getByText("Creator 000", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "M, 512 results" }).click();
  await expect(page.getByText(/Temporary batch failure/)).toBeVisible();
  await page.getByRole("button", { name: "Retry" }).click();
  await expect(page.getByText("Creator 300", { exact: true })).toBeVisible();
  expect(observations.searches.filter(
    (request) => request.scope === "creators" && request.offset === 300,
  )).toHaveLength(2);
});

test("name index becomes a mobile bottom rail without covering the list", async ({ context, page }) => {
  const observations = { searches: [], summaryBatches: [] } as {
    searches: Array<{ scope: string; offset: number; limit: number; q: string }>;
    summaryBatches: string[][];
  };
  await installRoutes(context, observations);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/admin/creators");
  const rail = page.getByRole("navigation", { name: "Name index" });
  await expect(rail).toBeVisible();
  await expect(rail).toHaveCSS("position", "sticky");
  await expect(rail).toHaveCSS("flex-direction", "row");
  const railBox = await rail.boundingBox();
  const viewport = page.viewportSize();
  expect(railBox && viewport && railBox.y + railBox.height <= viewport.height + 1).toBe(true);
});

test("desktop name indexes hide their scrollbars but keep wheel scrolling", async ({ context, page }) => {
  const observations = { searches: [], summaryBatches: [] } as {
    searches: Array<{ scope: string; offset: number; limit: number; q: string }>;
    summaryBatches: string[][];
  };
  await installRoutes(context, observations);
  await page.setViewportSize({ width: 1280, height: 520 });

  for (const scope of ["creators", "subscriptions"] as const) {
    await page.goto(`/admin/${scope}`);
    const rail = page.getByRole("navigation", { name: "Name index" });
    await expect(rail).toBeVisible();
    await expect.poll(() => rail.evaluate((element) => ({
      clientHeight: element.clientHeight,
      scrollHeight: element.scrollHeight,
      scrollbarWidth: window.getComputedStyle(element).scrollbarWidth,
    }))).toMatchObject({ scrollbarWidth: "none" });
    expect(await rail.evaluate((element) => element.scrollHeight > element.clientHeight)).toBe(true);
    await rail.hover();
    await page.mouse.wheel(0, 320);
    await expect.poll(() => rail.evaluate((element) => element.scrollTop)).toBeGreaterThan(0);
  }
});

test("legacy pages become virtual offsets and list state survives a detail round trip", async ({ context, page }) => {
  const observations = { searches: [], summaryBatches: [] } as {
    searches: Array<{ scope: string; offset: number; limit: number; q: string }>;
    summaryBatches: string[][];
  };
  await installRoutes(context, observations);
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto("/admin/creators?p=2");

  await expect.poll(() => observations.searches.some(
    (request) => request.scope === "creators" && request.offset === 50 && request.limit === 50,
  )).toBe(true);
  await expect.poll(() => new URL(page.url()).searchParams.has("p")).toBe(false);
  await expect(page.getByText("Creator 050", { exact: true })).toBeVisible();
  const rowSelection = page.getByRole("checkbox", { name: "Select Creator 050" });
  await rowSelection.check();
  await expect(page.getByText(/1 selected \/ (50|100|150) loaded/)).toBeVisible();
  const beforeNavigationScroll = await page.evaluate(() => window.scrollY);
  expect(beforeNavigationScroll).toBeGreaterThan(0);

  await page.getByRole("link", { name: "Open Creator 050" }).click();
  await expect(page).toHaveURL(/\/admin\/creators\/creator-0050$/);
  await page.goBack();
  await expect(page.locator("[data-virtual-reference-list]")).toBeVisible();
  await expect(page.getByRole("checkbox", { name: "Select Creator 050" })).toBeChecked();
  await expect.poll(() => page.evaluate(() => window.scrollY)).toBeGreaterThan(0);

  const firstRow = page.getByRole("listitem").first();
  await expect(firstRow).toHaveAttribute("aria-setsize", String(TOTAL));
  await expect(firstRow).toHaveAttribute("aria-posinset", /\d+/);
  const axe = await new AxeBuilder({ page }).include("#main-content").analyze();
  expect(axe.violations).toEqual([]);

  await page.getByRole("button", { name: "Updated", exact: true }).click();
  await expect(page.getByRole("checkbox", { name: "Select all" })).not.toBeChecked();
  await expect(page.getByText(/0 selected \/ 50 loaded/)).toBeVisible();
});

for (const scope of ["creators", "subscriptions"] as const) {
  for (const firstOffset of [0, 50]) {
    const outcomes = firstOffset === 0
      ? ["success", "retry", "deleted", "select-loaded-row"] as const
      : ["success", "retry", "deleted"] as const;
    for (const outcome of outcomes) {
      test(`${scope} cold session selection survives ${firstOffset} first and ${outcome}`, async ({ context, page }) => {
        const prefix = scope === "creators" ? "creator" : "subscription";
        const secondOffset = firstOffset === 0 ? 50 : 0;
        let release!: () => void;
        const held = new Promise<void>((resolve) => { release = resolve; });
        const observations = {
          searches: [] as Array<{ scope: string; offset: number; limit: number; q: string }>,
          summaryBatches: [] as string[][],
          failSearch: new Set(outcome === "retry" ? [`${scope}:${secondOffset}`] : []),
          deletedIds: new Set(outcome === "deleted" ? [`${prefix}-0050`] : []),
          holdSearch: async (requestedScope: string, offset: number) => {
            if (requestedScope === scope && offset === secondOffset) await held;
          },
        };
        await installRoutes(context, observations);
        await context.addInitScript(({ scope, prefix }) => {
          const fingerprint = `${scope}:`;
          const key = `reference-list:v1:17:${encodeURIComponent(`/admin/${scope}`)}:${encodeURIComponent(fingerprint)}`;
          sessionStorage.setItem(key, JSON.stringify({ version: 1, queryFingerprint: fingerprint,
            scrollY: 0, loadedOffsets: [0, 50], selectedIds: [`${prefix}-0000`, `${prefix}-0050`],
            selectedOffsets: { [`${prefix}-0000`]: 0, [`${prefix}-0050`]: 50 } }));
        }, { scope, prefix });
        await page.setViewportSize({ width: 1440, height: 900 });
        try {
          await page.goto(`/admin/${scope}`);
          await expect(page.getByText(/\d+ selected \/ (49|50) loaded/)).toBeVisible();
          // Pending IDs are saved, but cannot enter the loaded-row actions.
          const partialSelection = outcome === "deleted" && firstOffset === 50 ? 0 : 1;
          await expect(page.getByText(new RegExp(`${partialSelection} selected / (49|50) loaded`))).toBeVisible();
          if (outcome === "select-loaded-row") {
            const otherRow = page.getByRole("listitem").nth(1).getByRole("checkbox");
            await otherRow.check();
            await expect(page.getByText("2 selected / 50 loaded", { exact: true })).toBeVisible();
            await otherRow.uncheck();
            await expect(page.getByText("1 selected / 50 loaded", { exact: true })).toBeVisible();
            await otherRow.check();
            await expect(page.getByText("2 selected / 50 loaded", { exact: true })).toBeVisible();
          }
          // A saved owner page can confirm deletion before other pages load.
          const expectedSelection = outcome === "deleted" && firstOffset === 50 ? [`${prefix}-0000`]
            : outcome === "select-loaded-row"
            ? [`${prefix}-0000`, `${prefix}-0001`, `${prefix}-0050`]
            : [`${prefix}-0000`, `${prefix}-0050`];
          const pending = await page.evaluate((scope) => {
            window.dispatchEvent(new Event("pagehide"));
            const key = `reference-list:v1:17:${encodeURIComponent(`/admin/${scope}`)}:${encodeURIComponent(`${scope}:`)}`;
            return JSON.parse(sessionStorage.getItem(key)!);
          }, scope);
          expect(pending.selectedIds).toEqual(expectedSelection);
          expect(pending.loadedOffsets).toEqual([0, 50]);
          release();
          if (outcome === "retry") {
            // The failed page may be outside the viewport; scrolling makes its
            // existing retry control visible without changing the selection.
            if (secondOffset === 50) await page.evaluate(() => window.scrollTo(0, 6000));
            await expect(page.getByRole("button", { name: "Retry", exact: true })).toBeVisible();
            await expect(page.getByText(/1 selected \/ 50 loaded/)).toBeVisible();
            observations.failSearch.clear();
            await page.getByRole("button", { name: "Retry", exact: true }).click();
          }
          const completedSelection = outcome === "deleted" ? "1 selected / 99 loaded"
            : outcome === "select-loaded-row" ? "3 selected / 100 loaded" : "2 selected / 100 loaded";
          await expect(page.getByText(completedSelection, { exact: true })).toBeVisible();
          const savedSelection = await page.evaluate((scope) => {
            window.dispatchEvent(new Event("pagehide"));
            const key = `reference-list:v1:17:${encodeURIComponent(`/admin/${scope}`)}:${encodeURIComponent(`${scope}:`)}`;
            return JSON.parse(sessionStorage.getItem(key)!).selectedIds;
          }, scope);
          expect(savedSelection).toEqual(outcome === "deleted" ? [`${prefix}-0000`] : expectedSelection);
          if (outcome === "success") {
            await page.getByRole("button", { name: "Updated", exact: true }).click();
            await expect(page.getByText(/0 selected \/ 50 loaded/)).toBeVisible();
          }
        } finally { release(); }
      });
    }
  }
}
for (const scope of ["creators", "subscriptions"] as const) {
  test(`${scope} legacy selection with unknown page ownership remains pending`, async ({ context, page }) => {
    const observations = { searches: [] as Array<{ scope: string; offset: number; limit: number; q: string }>, summaryBatches: [] as string[][] };
    await installRoutes(context, observations);
    const prefix = scope === "creators" ? "creator" : "subscription";
    await context.addInitScript(({ scope, prefix }) => {
      const key = `reference-list:v1:17:${encodeURIComponent(`/admin/${scope}`)}:${encodeURIComponent(`${scope}:`)}`;
      sessionStorage.setItem(key, JSON.stringify({ version: 1, queryFingerprint: `${scope}:`, scrollY: 0,
        loadedOffsets: [0], selectedIds: [`${prefix}-0250`] }));
    }, { scope, prefix });
    await page.goto(`/admin/${scope}`);
    await expect(page.getByText("0 selected / 50 loaded", { exact: true })).toBeVisible();
    const saved = await page.evaluate(scope => {
      window.dispatchEvent(new Event("pagehide"));
      const key = `reference-list:v1:17:${encodeURIComponent(`/admin/${scope}`)}:${encodeURIComponent(`${scope}:`)}`;
      return JSON.parse(sessionStorage.getItem(key)!);
    }, scope);
    expect(saved.selectedIds).toEqual([`${prefix}-0250`]);
  });

  test(`${scope} evicted selection survives reload and retained-page restoration`, async ({ context, page }) => {
    test.setTimeout(120_000);
    const observations = { searches: [] as Array<{ scope: string; offset: number; limit: number; q: string }>, summaryBatches: [] as string[][], total: 10_000, deletedIds: new Set<string>() };
    await installRoutes(context, observations);
    const prefix = scope === "creators" ? "creator" : "subscription";
    const name = scope === "creators" ? "Creator" : "Custom title";
    const rowName = (index: number) => scope === "creators"
      ? `Select ${name} ${String(index).padStart(3, "0")}`
      : `Select ${name} ${String(TOTAL - index).padStart(3, "0")}`;
    const save = () => page.evaluate(scope => {
      window.dispatchEvent(new Event("pagehide"));
      const key = `reference-list:v1:17:${encodeURIComponent(`/admin/${scope}`)}:${encodeURIComponent(`${scope}:`)}`;
      return JSON.parse(sessionStorage.getItem(key)!);
    }, scope);
    const scrollPage = async (index: number) => {
      const target = index * 50;
      await expect(async () => {
        await page.evaluate(target => {
          const first = document.querySelector("[data-virtual-index]") as HTMLElement;
          const exact = document.querySelector(`[data-virtual-index="${target}"]`) as HTMLElement | null;
          const row = exact || first;
          const distance = exact ? 0 : (target - Number(first.dataset.virtualIndex)) * first.getBoundingClientRect().height;
          window.scrollTo(0, row.getBoundingClientRect().top + window.scrollY + distance - 150);
        }, target);
        await expect(page.getByRole("checkbox", { name: rowName(target), exact: true })).toBeVisible({ timeout: 500 });
      }).toPass({ timeout: 8_000 });
    };
    await page.goto(`/admin/${scope}`);
    await expect(page.getByRole("checkbox", { name: rowName(0), exact: true })).toBeVisible();
    await scrollPage(5);
    await page.getByRole("checkbox", { name: rowName(250), exact: true }).check();
    for (let index = 6; index <= 45; index++) await scrollPage(index);
    const before = await save();
    expect(before.loadedOffsets).not.toContain(250);
    expect(before.selectedIds).toContain(`${prefix}-0250`);
    expect(before.selectedOffsets[`${prefix}-0250`]).toBe(250);
    await page.reload();
    await expect(page.locator("[data-virtual-reference-list]")).toBeVisible();
    // Finish loading every retained page without fetching the evicted owner.
    for (const offset of before.loadedOffsets.toReversed()) await scrollPage(offset / 50);
    expect((await save()).selectedIds).toContain(`${prefix}-0250`);
    await scrollPage(5);
    await expect(page.getByRole("checkbox", { name: rowName(250), exact: true })).toBeChecked();
    // Once the saved owner really reloads without the ID, it may be pruned.
    await save();
    observations.deletedIds.add(`${prefix}-0250`);
    const beforeDeletion = observations.searches.length;
    await page.reload();
    await expect.poll(() => observations.searches.slice(beforeDeletion).some(request => request.offset === 250)).toBe(true);
    await expect.poll(async () => (await save()).selectedIds).not.toContain(`${prefix}-0250`);
  });
}

for (const pages of [10, 50, 100]) {
  test(`favorite invalidation stays bounded after browsing ${pages} pages`, async ({ context, page }) => {
    test.setTimeout(90_000);
    const observations = { searches: [] as Array<{ scope: string; offset: number; limit: number; q: string }>, summaryBatches: [] as string[][], total: 10_000 };
    await installRoutes(context, observations);
    await page.goto('/admin/creators');
    await expect(page.getByRole('checkbox', { name: 'Select Creator 000' })).toBeVisible();
    await page.getByRole('checkbox', { name: 'Select Creator 000' }).check();
    for (let index = 1; index < pages; index++) {
      await page.evaluate(index => {
        const row = document.querySelector('[data-virtual-index]') as HTMLElement;
        const list = document.querySelector('[data-virtual-reference-list]') as HTMLElement;
        const height = row.getBoundingClientRect().height;
        window.scrollTo(0, list.getBoundingClientRect().top + window.scrollY + index * 50 * height);
      }, index);
      await expect.poll(() => observations.searches.some(request => request.offset >= index * 50)).toBe(true);
    }
    await expect(page.getByRole('button', { name: 'Favorite', exact: true }).first()).toBeVisible();
    await page.waitForTimeout(200);
    const before = observations.searches.length;
    await page.getByRole('button', { name: 'Favorite', exact: true }).first().click();
    await expect.poll(() => observations.searches.length).toBeGreaterThan(before);
    await page.waitForTimeout(500);
    expect(observations.searches.length - before).toBeLessThanOrEqual(4);
    const saved = await page.evaluate(() => {
      window.dispatchEvent(new Event('pagehide'));
      const key = 'reference-list:v1:17:' + encodeURIComponent('/admin/creators') + ':' + encodeURIComponent('creators:');
      return JSON.parse(sessionStorage.getItem(key)!);
    });
    expect(saved.loadedOffsets.length).toBeLessThanOrEqual(32);
    expect(saved.selectedIds).toContain('creator-0000');
    expect(Math.max(...saved.loadedOffsets)).toBeGreaterThanOrEqual((pages - 1) * 50);
    if (pages === 50) {
      const visibleName = await page.getByRole('link', { name: /^Open Creator \d+$/ }).evaluateAll(rows =>
        rows.find(row => {
          const rect = row.getBoundingClientRect();
          return rect.top >= 64 && rect.bottom < window.innerHeight;
        })?.getAttribute('aria-label'));
      expect(visibleName).toBeTruthy();
      const scrollY = await page.evaluate(() => window.scrollY);
      await page.getByRole('link', { name: visibleName!, exact: true }).click();
      await expect(page).toHaveURL(/\/admin\/creators\/creator-\d+/);
      await page.goBack();
      await expect(page).toHaveURL(/\/admin\/creators$/);
      await expect.poll(() => page.evaluate(() => window.scrollY)).toBeGreaterThan(scrollY - 300);
    }
    await page.evaluate(() => window.scrollTo(0, 0));
    await expect(page.getByRole('checkbox', { name: 'Select Creator 000' })).toBeChecked();
  });
}

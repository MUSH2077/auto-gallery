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
    holdCompose?: () => Promise<void>;
  },
  theme: "dark" | "light" = "dark",
) {
  await context.addCookies([{
    name: "ag_token",
    value: "reference-list-token",
    domain: acceptanceHost,
    path: "/",
  }]);
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
      const failKey = `${scope}:${offset}`;
      if (observations.failSearchOnce?.delete(failKey)) {
        await route.fulfill({ status: 503, json: { detail: "Temporary batch failure" } });
        return;
      }
      const scopedTotal = scope === "works" ? 0 : TOTAL;
      const items = scope === "works" ? [] : Array.from(
        { length: Math.max(0, Math.min(limit, TOTAL - offset)) },
        (_, index) => scope === "subscriptions"
          ? subscriptionAt(offset + index)
          : creatorAt(offset + index),
      );
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

  await page.clock.fastForward(16_000);
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

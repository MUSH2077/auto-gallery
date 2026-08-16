import { expect, test, type BrowserContext, type Route } from "@playwright/test";

type FixtureToken =
  | { kind: "text"; value: string; quoted: boolean; start: number; end: number }
  | { kind: "qualifier"; key: string; value: string; negated: boolean; quoted: boolean; start: number; end: number };

type Compose = {
  key: string;
  value?: string | null;
  operation?: "set" | "add" | "toggle" | "remove" | "replace-group";
  negated?: boolean;
  replace_values?: string[];
};

const me = {
  id: 1,
  username: "search-review",
  display_name: "Search Review",
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

const work = {
  id: "work-aurora",
  title: "Aurora Study",
  description: "A synthetic search fixture",
  posted_at: "2026-07-28T10:00:00Z",
  is_nsfw: false,
  is_ai_generated: false,
  asset_count: 1,
  created_at: "2026-07-28T10:00:00Z",
  source: "pixiv",
  creator_name: "Studio Atlas",
  creator_id: "creator-atlas",
  tags: ["aurora"],
  is_favorite: true,
  curation_visibility: "visible",
};

const creator = {
  id: "creator-atlas",
  name: "studio-atlas",
  display_name: "Studio Atlas",
  description: "Synthetic creator fixture",
  is_active: true,
  is_favorite: true,
  subscription_count: 1,
  source_count: 1,
  repository_count: 1,
  created_at: "2026-07-28T09:00:00Z",
  updated_at: "2026-07-28T10:00:00Z",
};

const tag = {
  id: "tag-aurora",
  normalized_name: "aurora",
  category: "general",
  usage_count: 12,
  created_at: "2026-07-28T09:00:00Z",
};

const repository = {
  id: "repo-atlas",
  name: "Atlas Pixiv",
  source: "pixiv",
  source_creator_id: "12345",
  source_url: "https://www.pixiv.net/users/12345",
  creator_id: "creator-atlas",
  creator_name: "Studio Atlas",
  subscription_id: "subscription-atlas",
  subscription_name: "Atlas Archive",
  is_enabled: true,
  auth_healthy: true,
  auth_status: "ok",
  last_synced_at: "2026-07-28T10:00:00Z",
  created_at: "2026-07-28T09:00:00Z",
  updated_at: "2026-07-28T10:00:00Z",
};

const subscription = {
  id: "subscription-atlas",
  name: "Atlas Archive",
  creator_id: "creator-atlas",
  creator_name: "Studio Atlas",
  creator_display_name: "Studio Atlas",
  is_active: true,
  sync_enabled: true,
  sync_interval_hours: 6,
  last_synced_at: "2026-07-28T10:00:00Z",
  source_count: 1,
  enabled_source_count: 1,
  running_job_count: 0,
  failed_job_count: 0,
  created_at: "2026-07-28T09:00:00Z",
  updated_at: "2026-07-28T10:00:00Z",
};

const TARGETS = ["works", "creators", "tags", "repositories", "subscriptions"] as const;

function parseQuery(raw: string, scope: string) {
  const tokens: FixtureToken[] = [];
  const matcher = /(-?)([a-z][a-z-]*)[:：](?:"((?:\\.|[^"])*)"|([^\s]+))|("(?:\\.|[^"])*"|[^\s]+)/giu;
  for (const match of raw.matchAll(matcher)) {
    const start = match.index || 0;
    if (match[2]) {
      const value = (match[3] ?? match[4] ?? "").replace(/\\"/g, "\"").replace(/\\\\/g, "\\");
      tokens.push({
        kind: "qualifier",
        key: match[2].toLowerCase(),
        value,
        negated: match[1] === "-",
        quoted: match[3] !== undefined,
        start,
        end: start + match[0].length,
      });
    } else {
      const quoted = match[5].startsWith("\"");
      tokens.push({
        kind: "text",
        value: quoted ? match[5].slice(1, -1) : match[5],
        quoted,
        start,
        end: start + match[0].length,
      });
    }
  }
  const canonical = tokens.map((token) => {
    const escaped = token.value.replace(/\\/g, "\\\\").replace(/"/g, "\\\"");
    const value = token.quoted || /\s/.test(token.value) ? `"${escaped}"` : escaped;
    return token.kind === "text" ? value : `${token.negated ? "-" : ""}${token.key}:${value}`;
  }).join(" ");
  const explicitType = tokens.find((token) => token.kind === "qualifier" && token.key === "type");
  const typeTarget: Record<string, string> = {
    work: "works",
    creator: "creators",
    tag: "tags",
    repo: "repositories",
    subscription: "subscriptions",
  };
  const scopeTarget: Record<string, string[]> = {
    global: [...TARGETS],
    works: ["works"],
    creators: ["creators"],
    tags: ["tags"],
    repositories: ["repositories"],
    subscriptions: ["subscriptions"],
    tasks: ["tasks"],
    scheduler: ["scheduler"],
    "creator-picker": ["creators"],
  };
  const targets = explicitType?.kind === "qualifier"
    ? [typeTarget[explicitType.value] || explicitType.value]
    : (scopeTarget[scope] || [...TARGETS]);
  return { raw, canonical, scope, targets, tokens };
}

function composeQuery(query: string, compose: Compose) {
  let tokens = parseQuery(query, "global").tokens;
  const same = (token: FixtureToken) => (
    token.kind === "qualifier"
    && token.key === compose.key
    && token.negated === Boolean(compose.negated)
    && (compose.value == null || token.value === compose.value)
  );
  const operation = compose.operation || "set";
  if (operation === "remove") {
    tokens = tokens.filter((token) => !same(token));
  } else if (operation === "toggle" && tokens.some(same)) {
    tokens = tokens.filter((token) => !same(token));
  } else {
    if (operation === "set") {
      tokens = tokens.filter((token) => token.kind !== "qualifier" || token.key !== compose.key);
    }
    if (operation === "replace-group") {
      const replaced = new Set(compose.replace_values || []);
      tokens = tokens.filter((token) => (
        token.kind !== "qualifier" || token.key !== compose.key || !replaced.has(token.value)
      ));
    }
    if (compose.value != null && compose.value !== "") {
      tokens.push({
        kind: "qualifier",
        key: compose.key,
        value: compose.value,
        negated: Boolean(compose.negated),
        quoted: /\s/.test(compose.value),
        start: 0,
        end: 0,
      });
    }
  }
  return tokens.map((token) => {
    const value = token.quoted || /\s/.test(token.value) ? `"${token.value}"` : token.value;
    return token.kind === "text" ? value : `${token.negated ? "-" : ""}${token.key}:${value}`;
  }).join(" ");
}

async function fulfillAssist(route: Route) {
  const body = route.request().postDataJSON() as {
    before_cursor?: string;
    after_cursor?: string;
    scope?: string;
    compose?: Compose;
    composes?: Compose[];
  };
  let query = `${body.before_cursor || ""}${body.after_cursor || ""}`.trim();
  for (const compose of [...(body.composes || []), ...(body.compose ? [body.compose] : [])]) {
    query = composeQuery(query, compose);
  }
  const parsed = parseQuery(query, body.scope || "global");
  const invalidDate = /posted:2026-99-99/.test(query);
  const suggestions = /(?:^|\s)tag[:：](?:a|au|aur)?$/i.test(query)
    ? [{
        kind: "value",
        label: "tag:aurora",
        description: "12 works",
        query: query.replace(/tag[:：][^\s]*$/i, "tag:aurora"),
      }]
    : query === "" || query === "t"
      ? [{
          kind: "qualifier",
          label: "tag:",
          description: "Filter by an exact normalized tag.",
          qualifier_key: "tag",
          help_id: "search.qualifier.tag",
          example: "tag:landscape",
          query: "tag:",
        }]
      : [];
  await route.fulfill({
    json: {
      query,
      canonical_query: invalidDate ? null : parsed.canonical,
      parsed: invalidDate ? null : parsed,
      diagnostics: invalidDate
        ? [{
            code: "invalid_date",
            message: "Use a valid ISO date such as 2026-07-28.",
            start: 0,
            end: query.length,
            token: query,
            suggestions: ["posted:2026-07-28"],
          }]
        : [],
      suggestions,
      catalog: [{
        key: "tag",
        negatable: true,
        values: [],
        help_id: "search.qualifier.tag",
        example: "tag:landscape",
        description: "Filter by an exact normalized tag.",
      }],
    },
  });
}

async function installSearchFixtures(context: BrowserContext) {
  await context.addCookies([{
    name: "ag_token",
    value: "search-test-token",
    domain: new URL(process.env.PLAYWRIGHT_BASE_URL || "http://127.0.0.1:13000").hostname,
    path: "/",
  }]);
  await context.addInitScript(() => {
    window.localStorage.setItem("ag_token", "search-test-token");
    window.localStorage.setItem("auto-gallery-lang", "en");
    window.localStorage.setItem("auto-gallery-theme", "dark");
  });
  await context.route("**/api/v1/**", async (route) => {
    const url = new URL(route.request().url());
    const path = url.pathname;
    if (path === "/api/v1/auth/me") {
      await route.fulfill({ json: me });
      return;
    }
    if (path === "/api/v1/search/assist") {
      await fulfillAssist(route);
      return;
    }
    if (path === "/api/v1/search") {
      const query = url.searchParams.get("q") || "";
      const scope = url.searchParams.get("scope") || "global";
      const parsed = parseQuery(query, scope);
      const groups: Record<string, { total: number; items: unknown[] }> = {};
      for (const target of parsed.targets) {
        if (target === "works") groups.works = { total: 1, items: [work] };
        if (target === "creators") groups.creators = { total: 1, items: [creator] };
        if (target === "tags") groups.tags = { total: 1, items: [tag] };
        if (target === "repositories") groups.repositories = { total: 1, items: [repository] };
        if (target === "subscriptions") groups.subscriptions = { total: 1, items: [subscription] };
        if (target === "tasks") groups.tasks = { total: 0, items: [] };
        if (target === "scheduler") groups.scheduler = { total: 0, items: [] };
      }
      await route.fulfill({
        json: {
          query,
          canonical_query: parsed.canonical,
          parsed,
          groups,
          total: Object.values(groups).reduce((total, group) => total + group.total, 0),
          results: groups.works?.items || [],
          creators: groups.creators?.items || [],
          tags: groups.tags?.items || [],
          repositories: groups.repositories?.items || [],
          subscriptions: groups.subscriptions?.items || [],
        },
      });
      return;
    }
    if (path === "/api/v1/creators/count" || path === "/api/v1/subscriptions/count") {
      await route.fulfill({ json: { count: 1 } });
      return;
    }
    if (path === "/api/v1/tasks") {
      await route.fulfill({ json: { items: [], total: 0, offset: 0, limit: 50 } });
      return;
    }
    if (path === "/api/v1/download-jobs") {
      await route.fulfill({ json: [] });
      return;
    }
    if (path === "/api/v1/import-jobs") {
      await route.fulfill({ json: { items: [], total: 0, offset: 0, limit: 50 } });
      return;
    }
    if (path === "/api/v1/system/queue-stats") {
      await route.fulfill({ json: { default_queue: 0, scheduled_queue: 0, failed_jobs: 0, scheduler_enabled: true } });
      return;
    }
    if (path === "/api/v1/system/scheduler-decisions") {
      await route.fulfill({ json: { updated_at: "2026-07-28T10:00:00Z", scheduler_enabled: true, timezone: "UTC", items: [] } });
      return;
    }
    if (path === "/api/v1/system/workbench") {
      await route.fulfill({
        json: {
          updated_at: "2026-07-28T10:00:00Z",
          queue: {},
          scheduler: {},
          storage: {},
          health: {},
          attention: {},
          recent: { download_jobs: [], import_jobs: [], tasks: [], works: [], successful_syncs: [] },
        },
      });
      return;
    }
    if (path === "/api/v1/operations/overview") {
      await route.fulfill({
        json: {
          view: url.searchParams.get("view") || "attention",
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
        },
      });
      return;
    }
    if (path === "/api/v1/creators") {
      await route.fulfill({ json: { items: [creator], total: 1 } });
      return;
    }
    if (path === "/api/v1/subscriptions") {
      await route.fulfill({ json: [subscription] });
      return;
    }
    await route.fulfill({ json: {} });
  });
}

test.beforeEach(async ({ context }) => {
  await installSearchFixtures(context);
});

test("global search groups five entity types and supports keyboard suggestions and token removal", async ({ page }) => {
  await page.goto("/admin/search?q=tag%3Aaurora");
  const input = page.getByRole("combobox", { name: "Search works..." });
  await expect(input).toHaveValue("tag:aurora");
  await expect(page.getByText("Aurora Study", { exact: true })).toBeVisible();
  await expect(page.getByText("Studio Atlas", { exact: true }).first()).toBeVisible();
  await expect(page.getByText("#aurora")).toBeVisible();
  await expect(page.getByText("Atlas Pixiv")).toBeVisible();
  await expect(page.getByText("Atlas Archive")).toBeVisible();

  await expect(page.getByRole("button", { name: "Remove search condition: tag:aurora" })).toBeVisible();
  await input.fill("tag:a");
  await expect(page.getByRole("option", { name: /tag:aurora/ })).toBeVisible();
  await input.press("ArrowDown");
  await input.press("Enter");
  await expect(input).toHaveValue("tag:aurora");
  await expect(page).toHaveURL(/q=tag%3Aaurora/);

  await page.getByRole("button", { name: "Remove search condition: tag:aurora" }).click();
  await expect(input).toHaveValue("");
  await expect(page).not.toHaveURL(/(?:\\?|&)q=/);
});

test("qualifier suggestions explain their purpose and example", async ({ page }) => {
  await page.goto("/admin/search");
  const input = page.getByRole("combobox", { name: "Search works..." });
  await input.fill("t");
  const option = page.getByRole("option", {
    name: "tag: Filter by an exact normalized tag, for example tag:landscape",
  });
  await expect(option).toBeVisible();
  await expect(page.getByText("Insert search qualifier")).toHaveCount(0);
});

test("entity tabs and visible work filters write only the canonical q parameter", async ({ page }) => {
  await page.goto("/admin/search?q=aurora");
  await page.getByRole("tab", { name: /^Repositories/ }).click();
  await expect(page).toHaveURL(/q=(?:type%3Arepo(?:\+|%20)aurora|aurora(?:\+|%20)type%3Arepo)/);
  await expect(page.getByText("Atlas Pixiv")).toBeVisible();
  await page.goBack();
  await expect(page).toHaveURL(/q=aurora/);

  await page.goto("/admin/works");
  await page.getByRole("combobox", { name: "Filter source" }).selectOption("pixiv");
  await expect.poll(() => new URL(page.url()).searchParams.get("q")).toBe("source:pixiv");
  const params = new URL(page.url()).searchParams;
  for (const legacy of ["source", "creator", "nsfw", "fav", "ai", "sort", "order"]) {
    expect(params.has(legacy), `${legacy} must not be a second search state`).toBe(false);
  }
});

test("every internal search surface uses the shared smart-search contract", async ({ page }) => {
  const routes = [
    "/admin/works",
    "/admin/creators",
    "/admin/subscriptions",
    "/admin/jobs?tab=all",
    "/admin/scheduler",
    "/admin/upload",
  ];
  await page.setViewportSize({ width: 390, height: 844 });
  for (const route of routes) {
    await page.goto(route);
    if (route === "/admin/works") {
      await page.getByRole("button", { name: /Filters/ }).click();
    } else if (route === "/admin/scheduler") {
      await page.locator("details").filter({ hasText: "Healthy schedules" }).locator("summary").click();
    }
    await expect(page.locator("[data-smart-search]").first(), route).toBeVisible();
    await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth)).toBe(true);
  }
});

test("server diagnostics are exposed without executing an invalid query", async ({ page }) => {
  await page.goto("/admin/search");
  const input = page.getByRole("combobox", { name: "Search works..." });
  await input.fill("posted:2026-99-99");
  await expect(input).toHaveAttribute("aria-invalid", "true");
  await expect(page.getByText("Use a valid ISO date: posted:2026-99-99").first()).toBeVisible();
});

import { expect, test, type Browser, type BrowserContext, type Route } from "@playwright/test";

test.describe.configure({ timeout: 90_000 });

const modules = {
  library: "Library",
  curation: "Curation",
  upload: "Upload",
  subscriptions: "Subscriptions",
  tasks: "Tasks",
  system: "System",
};

const principal = (permissions: string[]) => ({
  id: permissions.length + 40,
  username: permissions.join("-") || "denied",
  display_name: "Fixture Operator",
  is_admin: false,
  is_active: true,
  permissions,
  modules,
  preferences: {},
  nsfw_visible: true,
  upload_used_bytes: 0,
  must_change_password: false,
});

const json = (route: Route, body: unknown, status = 200) => route.fulfill({
  status,
  contentType: "application/json",
  body: JSON.stringify(body),
});

const workbench = {
  updated_at: "2026-09-13T00:00:00Z",
  queue: {}, scheduler: {}, storage: {}, health: {}, attention: {}, recent: {},
};

type FixtureHandler = (route: Route, url: URL, method: string) => Promise<boolean>;

async function openFixture(
  browser: Browser,
  pathname: string,
  permissions: string[],
  handler: FixtureHandler,
  init?: (context: BrowserContext) => Promise<void>,
) {
  const context = await browser.newContext();
  await context.addCookies([{ name: "ag_token", value: "fixture", domain: "127.0.0.1", path: "/" }]);
  await context.addInitScript(() => {
    localStorage.setItem("ag_token", "fixture");
    localStorage.setItem("auto-gallery-lang", "en");
  });
  await init?.(context);
  const unhandled: string[] = [];
  await context.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const method = request.method();
    if (await handler(route, url, method)) return;
    if (url.pathname === "/api/v1/auth/me") return json(route, principal(permissions));
    if (url.pathname === "/api/v1/auth/ws-ticket") return json(route, { detail: "fixture websocket unavailable" }, 503);
    if (url.pathname === "/api/v1/system/workbench") return json(route, workbench);
    if (url.pathname === "/api/v1/tasks") return json(route, { total: 0, items: [] });
    if (url.pathname === "/api/v1/system/scheduler-decisions") return json(route, { total: 0, items: [], summary: { blocked_count: 0 } });
    if (url.pathname === "/api/v1/operations/overview") return json(route, { summary: { attention: 0 }, items: [] });
    unhandled.push(`${method} ${url.pathname}${url.search}`);
    return json(route, { detail: `Unhandled fixture request: ${method} ${url.pathname}` }, 501);
  });
  const page = await context.newPage();
  await page.goto(pathname);
  return { context, page, unhandled };
}

const guardedRoutes = [
  {
    name: "data management",
    pathname: "/admin/data-mgmt",
    protectedRequest: (url: URL) => [
      "/api/v1/admin/system-info",
      "/api/v1/admin/storage-breakdown",
      "/api/v1/admin/backup/list",
      "/api/v1/admin/integrity-check/latest",
      "/api/v1/admin/backup/latest",
      "/api/v1/admin/cleanup-metadata-jsons/latest",
      "/api/v1/admin/library/rebuild/latest",
      "/api/v1/admin/library/import-from-disk/latest",
      "/api/v1/admin/creators/re-enrich/latest",
      "/api/v1/admin/operations/clear/latest",
    ].includes(url.pathname),
  },
  { name: "tags", pathname: "/admin/tags", protectedRequest: (url: URL) => url.pathname === "/api/v1/tags/page" },
  { name: "settings", pathname: "/admin/settings", protectedRequest: (url: URL) => url.pathname === "/api/v1/admin/settings" },
  { name: "dedup settings", pathname: "/admin/settings/dedup", protectedRequest: (url: URL) => url.pathname === "/api/v1/admin/settings" },
  {
    name: "download defaults",
    pathname: "/admin/settings/download-defaults",
    protectedRequest: (url: URL) => url.pathname === "/api/v1/admin/settings" || url.pathname === "/api/v1/system/health",
  },
  { name: "gallery-dl settings", pathname: "/admin/settings/gallerydl", protectedRequest: (url: URL) => url.pathname === "/api/v1/admin/gallerydl-config" },
  {
    name: "proxy settings",
    pathname: "/admin/settings/proxy",
    protectedRequest: (url: URL) => url.pathname === "/api/v1/admin/settings" || url.pathname === "/api/v1/admin/proxy/test/latest",
  },
  { name: "scheduler defaults", pathname: "/admin/settings/scheduler-defaults", protectedRequest: (url: URL) => url.pathname === "/api/v1/admin/settings" },
  { name: "subscription defaults", pathname: "/admin/settings/subscription-defaults", protectedRequest: (url: URL) => url.pathname === "/api/v1/admin/settings" },
  { name: "Gitllery settings", pathname: "/admin/settings/gitllery", protectedRequest: (url: URL) => url.pathname === "/api/v1/admin/gitllery/settings" },
  {
    name: "notifications",
    pathname: "/admin/notifications",
    protectedRequest: (url: URL) => url.pathname === "/api/v1/tasks" && url.searchParams.get("limit") === "50",
  },
  { name: "dedup", pathname: "/admin/data-mgmt/dedup", protectedRequest: (url: URL) => url.pathname === "/api/v1/admin/dedup/cases" },
  { name: "search", pathname: "/admin/search?q=guard-fixture", protectedRequest: (url: URL) => url.pathname === "/api/v1/search" },
  {
    name: "Danbooru stale batch recovery",
    pathname: "/admin/upload/danbooru",
    protectedRequest: (url: URL) => url.pathname === "/api/v1/reference/danbooru/artist/batch-import/status",
    init: async (context: BrowserContext) => {
      await context.addInitScript(() => {
        sessionStorage.setItem("danbooru_batch_job", JSON.stringify({ jobId: "stale-fixture", startedAt: Date.now() }));
      });
    },
  },
] as const;

for (const guarded of guardedRoutes) {
  test(`${guarded.name} rejects cold navigation before protected hooks mount`, async ({ browser }) => {
    const protectedRequests: string[] = [];
    const opened = await openFixture(browser, guarded.pathname, [], async (route, url, method) => {
      if (!guarded.protectedRequest(url)) return false;
      protectedRequests.push(`${method} ${url.pathname}${url.search}`);
      await json(route, { detail: "permission required" }, 403);
      return true;
    }, "init" in guarded ? guarded.init : undefined);
    try {
      await expect(opened.page.getByText("You don't have permission to access this page", { exact: true })).toBeVisible();
      expect(protectedRequests).toEqual([]);
      expect(opened.unhandled).toEqual([]);
    } finally {
      await opened.context.close();
    }
  });
}

test("the login page does not start a permission query before authentication", async ({ browser }) => {
  const context = await browser.newContext();
  let meRequests = 0;
  const unhandled: string[] = [];
  await context.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (url.pathname === "/api/v1/auth/me") {
      meRequests += 1;
      await json(route, { detail: "Not authenticated" }, 401);
      return;
    }
    unhandled.push(`${request.method()} ${url.pathname}`);
    await json(route, { detail: "Unhandled login fixture request" }, 501);
  });
  const page = await context.newPage();
  try {
    await page.goto("/admin/login");
    await expect(page.getByRole("button", { name: /Login|登录/ })).toBeVisible();
    await page.waitForTimeout(250);
    expect(meRequests).toBe(0);
    expect(unhandled).toEqual([]);
  } finally {
    await context.close();
  }
});

test("a subscriptions user still resumes a saved Danbooru batch", async ({ browser }) => {
  let statusRequests = 0;
  const opened = await openFixture(browser, "/admin/upload/danbooru", ["subscriptions"], async (route, url) => {
    if (url.pathname !== "/api/v1/reference/danbooru/artist/batch-import/status") return false;
    statusRequests += 1;
    await json(route, {
      status: "completed",
      progress: null,
      result: { total: 1, imported_count: 1, low_confidence_count: 0, not_found_count: 0, error_count: 0, imported: [], low_confidence: [], not_found: [], errors: [] },
      job_status: "complete",
    });
    return true;
  }, async (context) => {
    await context.addInitScript(() => {
      sessionStorage.setItem("danbooru_batch_job", JSON.stringify({ jobId: "allowed-fixture", importType: "pixiv", total: 1, startedAt: Date.now() }));
    });
  });
  try {
    await expect(opened.page.getByRole("heading", { level: 1, name: "Danbooru Reference Mapping" })).toBeVisible();
    await expect.poll(() => statusRequests).toBeGreaterThan(0);
    expect(opened.unhandled).toEqual([]);
  } finally {
    await opened.context.close();
  }
});

test("logout and account change stop saved Danbooru polling despite cached prior permissions", async ({ browser }) => {
  let statusRequests = 0;
  let deniedPermissionQueries = 0;
  const opened = await openFixture(browser, "/admin/upload/danbooru", ["subscriptions"], async (route, url, method) => {
    if (url.pathname === "/api/v1/auth/login" && method === "POST") {
      await json(route, { access_token: "denied-token", token_type: "bearer" });
      return true;
    }
    if (url.pathname === "/api/v1/auth/me" && route.request().headers()["authorization"] === "Bearer denied-token") {
      if (route.request().headers()["content-type"]) deniedPermissionQueries += 1;
      await json(route, { ...principal([]), id: 99, username: "next-denied" });
      return true;
    }
    if (url.pathname !== "/api/v1/reference/danbooru/artist/batch-import/status") return false;
    statusRequests += 1;
    await json(route, {
      status: "running",
      progress: { current: 1, total: 2, imported: 1, errors: 0 },
      result: null,
      job_status: "running",
    });
    return true;
  }, async (context) => {
    await context.addInitScript(() => {
      sessionStorage.setItem("danbooru_batch_job", JSON.stringify({ jobId: "logout-fixture", importType: "pixiv", total: 2, startedAt: Date.now() }));
    });
  });
  try {
    await expect.poll(() => statusRequests).toBeGreaterThan(0);
    await opened.page.getByRole("button", { name: "User menu", exact: true }).click();
    await opened.page.getByRole("menuitem", { name: "Sign Out", exact: true }).click();
    await expect(opened.page).toHaveURL(/\/admin\/login$/, { timeout: 15_000 });
    await opened.page.waitForTimeout(100);
    const requestsAtLogout = statusRequests;
    await opened.page.waitForTimeout(2_500);
    expect(statusRequests).toBe(requestsAtLogout);

    await opened.page.evaluate(() => {
      sessionStorage.setItem("danbooru_batch_job", JSON.stringify({ jobId: "next-account-fixture", importType: "pixiv", total: 2, startedAt: Date.now() }));
    });
    await opened.page.getByLabel("Username", { exact: true }).fill("next-denied");
    await opened.page.getByLabel("Password", { exact: true }).fill("fixture-password");
    await opened.page.getByRole("button", { name: "Sign in", exact: true }).click();
    await expect.poll(() => opened.page.evaluate(() => localStorage.getItem("ag_token"))).toBe("denied-token");
    const requestsAfterAccountChange = statusRequests;
    await opened.page.waitForTimeout(2_500);
    expect(statusRequests).toBe(requestsAfterAccountChange);
    expect(deniedPermissionQueries).toBe(0);
    expect(opened.unhandled).toEqual([]);
  } finally {
    await opened.context.close();
  }
});

test("Escape closes the command palette before its delayed autofocus runs", async ({ browser }) => {
  const opened = await openFixture(browser, "/admin/tags", ["library"], async (route, url) => {
    if (url.pathname !== "/api/v1/tags/page") return false;
    await json(route, { total: 0, offset: 0, limit: 100, items: [] });
    return true;
  });
  try {
    await opened.page.evaluate(() => {
      const testWindow = window as Window & {
        __flushCommandFocus?: () => void;
      };
      const nativeFocus = HTMLInputElement.prototype.focus;
      HTMLInputElement.prototype.focus = function delayedCommandFocus(options) {
        testWindow.__flushCommandFocus = () => nativeFocus.call(this, options);
      };
    });

    const trigger = opened.page.getByRole("button", { name: /Search works/ }).first();
    await trigger.focus();
    await trigger.evaluate((element: HTMLButtonElement) => element.click());
    const dialog = opened.page.getByRole("dialog", { name: "Search" });
    await expect(dialog).toBeVisible();
    await expect(trigger).toBeFocused();
    await opened.page.keyboard.press("Escape");
    await expect(dialog).toHaveCount(0);
    await opened.page.evaluate(() => {
      const testWindow = window as Window & { __flushCommandFocus?: () => void };
      testWindow.__flushCommandFocus?.();
    });
    await expect(trigger).toBeFocused();
    expect(opened.unhandled).toEqual([]);
  } finally {
    await opened.context.close();
  }
});

test("library rebuild describes its real scope and retains confirmation until a request is accepted", async ({ browser }) => {
  let rebuildAttempts = 0;
  const rebuildBodies: string[] = [];
  const operationPollIds: string[] = [];
  const taskDetailIds: string[] = [];
  const opened = await openFixture(browser, "/admin/data-mgmt", ["system", "tasks"], async (route, url, method) => {
    if (url.pathname === "/api/v1/admin/system-info") {
      await json(route, { version: "fixture", downloads_size_mb: 0, library_size_mb: 0, downloads_free_gb: 1, archives_kb: {}, db_stats: { works: 0, assets: 0, creators: 0, subscriptions: 0, tags: 0 } });
      return true;
    }
    if (url.pathname === "/api/v1/admin/storage-breakdown") {
      await json(route, { sources: {}, creator_tree: [], unlinked_repositories: [], db_stats: { works: 0, assets: 0, creators: 0, subscriptions: 0, tags: 0 } });
      return true;
    }
    if (url.pathname === "/api/v1/admin/backup/list") { await json(route, { backups: [] }); return true; }
    if ([
      "/api/v1/admin/backup/latest",
      "/api/v1/admin/integrity-check/latest",
      "/api/v1/admin/cleanup-metadata-jsons/latest",
      "/api/v1/admin/library/rebuild/latest",
      "/api/v1/admin/library/import-from-disk/latest",
      "/api/v1/admin/creators/re-enrich/latest",
      "/api/v1/admin/operations/clear/latest",
    ].includes(url.pathname)) {
      await json(route, { current: null, snapshot: null });
      return true;
    }
    if (url.pathname === "/api/v1/admin/library/rebuild") {
      rebuildAttempts += 1;
      rebuildBodies.push(await route.request().postData() || "");
      if (method !== "POST") { await json(route, { detail: `Expected POST, received ${method}` }, 405); return true; }
      if (rebuildAttempts === 1) { await json(route, { detail: "rebuild queue unavailable" }, 503); return true; }
      await json(route, {
        task_id: "22222222-2222-4222-8222-222222222222",
        job_id: "admin-22222222-2222-4222-8222-222222222222-attempt-1",
        status: "enqueued",
        message: "Library rebuild queued",
      }, 202);
      return true;
    }
    if (url.pathname.startsWith("/api/v1/admin/operations/")) {
      operationPollIds.push(url.pathname.slice("/api/v1/admin/operations/".length));
      await json(route, {
        job_id: url.pathname.slice("/api/v1/admin/operations/".length),
        status: "running",
        operation_type: "admin-rebuild",
        progress: { phase: "rebuilding", label: "Rebuilding library" },
      });
      return true;
    }
    if (url.pathname === "/api/v1/tasks/22222222-2222-4222-8222-222222222222") {
      taskDetailIds.push(url.pathname.slice("/api/v1/tasks/".length));
      await json(route, {
        id: "22222222-2222-4222-8222-222222222222",
        kind: "admin",
        operation_type: "admin-rebuild",
        status: "running",
        title: "Library rebuild fixture",
        queue_name: "maintenance",
        rq_job_id: "admin-22222222-2222-4222-8222-222222222222-attempt-1",
        created_at: "2026-09-13T00:00:00Z",
      });
      return true;
    }
    if (url.pathname === "/api/v1/search/assist") {
      await json(route, { query: "", canonical_query: "", parsed: { tokens: [] }, suggestions: [] });
      return true;
    }
    return false;
  });
  try {
    await expect(opened.page.getByText("Rebuild library metadata", { exact: true })).toBeVisible();
    await expect(opened.page.getByText(/Meilisearch is not cleared/)).toBeVisible();
    await expect(opened.page.getByText("Rebuild Search Index", { exact: true })).toHaveCount(0);

    await opened.page.getByRole("button", { name: "Rebuild library", exact: true }).click();
    const dialog = opened.page.getByRole("dialog");
    await expect(dialog).toContainText("Rebuild library metadata?");
    await expect(dialog).toContainText("Original files are not deleted");
    expect(rebuildAttempts).toBe(0);

    await dialog.getByRole("button", { name: "Cancel", exact: true }).click();
    expect(rebuildAttempts).toBe(0);
    await opened.page.getByRole("button", { name: "Rebuild library", exact: true }).click();
    await dialog.getByRole("button", { name: "Confirm", exact: true }).click();
    await expect(dialog.getByRole("alert")).toContainText("rebuild queue unavailable");
    await expect(dialog).toBeVisible();
    await dialog.getByRole("button", { name: "Confirm", exact: true }).click();
    await expect(dialog).toBeHidden();
    await expect(opened.page.getByText("Library rebuild submitted. Track its progress in Jobs.", { exact: true }).first()).toBeVisible();
    await expect(opened.page.getByText("Library rebuild queued", { exact: true })).toHaveCount(0);
    const taskAction = opened.page.getByRole("button", { name: "Task detail", exact: true });
    await expect(taskAction).toBeVisible();
    await opened.page.waitForTimeout(4_000);
    await expect(taskAction).toBeVisible();
    await taskAction.click({ timeout: 3_000 });
    await expect(opened.page).toHaveURL(/\/admin\/jobs\?tab=admin&task=22222222-2222-4222-8222-222222222222$/, { timeout: 30_000 });
    await expect(opened.page.getByText("Library rebuild fixture", { exact: true })).toBeVisible();
    await expect.poll(() => operationPollIds.length).toBeGreaterThan(0);
    expect(new Set(operationPollIds)).toEqual(new Set([
      "22222222-2222-4222-8222-222222222222",
      "admin-22222222-2222-4222-8222-222222222222-attempt-1",
    ]));
    expect(new Set(taskDetailIds)).toEqual(new Set(["22222222-2222-4222-8222-222222222222"]));
    expect(rebuildAttempts).toBe(2);
    expect(rebuildBodies).toEqual(["{}", "{}"]);
    expect(opened.unhandled).toEqual([]);
  } finally {
    await opened.context.close();
  }
});

test("proxy save reports network and validation failures without discarding edits", async ({ browser }) => {
  let saveAttempts = 0;
  const opened = await openFixture(browser, "/admin/settings/proxy", ["system"], async (route, url, method) => {
    if (url.pathname === "/api/v1/admin/settings" && method === "GET") {
      await json(route, { proxy: { enabled: false, http_proxy: "", https_proxy: "", no_proxy: "" } });
      return true;
    }
    if (url.pathname === "/api/v1/admin/proxy/test/latest") {
      await json(route, { current: null, snapshot: null });
      return true;
    }
    if (url.pathname === "/api/v1/admin/settings" && method === "PUT") {
      saveAttempts += 1;
      if (saveAttempts === 1) { await route.abort("failed"); return true; }
      if (saveAttempts === 2) { await json(route, { detail: "Proxy URL rejected" }, 422); return true; }
      await json(route, { status: "ok", message: "saved" });
      return true;
    }
    return false;
  });
  try {
    const input = opened.page.getByRole("textbox", { name: "HTTP Proxy", exact: true });
    await input.fill("http://proxy.example:7890");
    const save = opened.page.getByRole("button", { name: "Save Settings", exact: true });
    const saveError = opened.page.locator("p[role='alert']");
    await save.click();
    await expect(saveError).toContainText("Network error");
    await expect(input).toHaveValue("http://proxy.example:7890");
    await save.click();
    await expect(saveError).toContainText("Proxy URL rejected");
    await expect(input).toHaveValue("http://proxy.example:7890");
    await save.click();
    await expect(saveError).toHaveCount(0);
    expect(saveAttempts).toBe(3);
    expect(opened.unhandled).toEqual([]);
  } finally {
    await opened.context.close();
  }
});

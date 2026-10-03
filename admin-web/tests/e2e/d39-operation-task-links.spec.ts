import { expect, test, type Browser, type BrowserContext, type Page, type Route } from "@playwright/test";

test.describe.configure({ timeout: 150_000 });

const TASK_ID = "11111111-1111-4111-8111-111111111111";
const RQ_JOB_ID = `admin-${TASK_ID}-attempt-1`;

const principal = {
  id: 39,
  username: "d39-system",
  display_name: "D39 Fixture Operator",
  is_admin: false,
  is_active: true,
  permissions: ["system", "subscriptions", "tasks", "library"],
  modules: { system: "System", subscriptions: "Subscriptions", tasks: "Tasks", library: "Library" },
  preferences: {},
  nsfw_visible: true,
  upload_used_bytes: 0,
  must_change_password: false,
};

const json = (route: Route, body: unknown, status = 200) => route.fulfill({
  status,
  contentType: "application/json",
  body: JSON.stringify(body),
});

type Handler = (route: Route, url: URL, method: string) => Promise<boolean>;

async function openFixture(
  browser: Browser,
  pathname: string,
  handler: Handler,
  init?: (context: BrowserContext) => Promise<void>,
) {
  const context = await browser.newContext();
  await context.routeWebSocket("**/api/v1/ws*", (webSocket) => webSocket.close({ code: 1000, reason: "fixture" }));
  await context.addCookies([{ name: "ag_session", value: "fixture", domain: "127.0.0.1", path: "/" }, { name: "ag_csrf", value: "fixture-csrf", url: process.env.PLAYWRIGHT_BASE_URL || "http://127.0.0.1:13000" }]);
  await context.addInitScript(() => {
    localStorage.setItem("ag_token", "fixture");
    localStorage.setItem("auto-gallery-lang", "en");
  });
  await init?.(context);
  const unhandled: string[] = [];
  const taskDetailIds: string[] = [];
  await context.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const method = request.method();
    if (await handler(route, url, method)) return;
    if (url.pathname === "/api/v1/auth/me" && method === "GET") return json(route, principal);
    if (url.pathname === "/api/v1/auth/ws-ticket" && method === "POST") return json(route, { detail: "fixture websocket unavailable" }, 503);
    if (url.pathname === `/api/v1/tasks/${TASK_ID}` && method === "GET") {
      taskDetailIds.push(TASK_ID);
      return json(route, {
        id: TASK_ID,
        kind: "admin",
        operation_type: "d39-fixture",
        status: "running",
        title: "Canonical D39 task",
        queue_name: "maintenance",
        rq_job_id: RQ_JOB_ID,
        created_at: "2026-09-13T00:00:00Z",
      });
    }
    if (url.pathname === "/api/v1/tasks" && method === "GET") return json(route, { total: 0, items: [] });
    if (url.pathname === "/api/v1/system/workbench" && method === "GET") return json(route, { updated_at: "2026-09-13T00:00:00Z", queue: {}, scheduler: {}, storage: {}, health: {}, attention: {}, recent: {} });
    if (url.pathname === "/api/v1/system/scheduler-decisions" && method === "GET") return json(route, { total: 0, items: [], summary: { blocked_count: 0 } });
    if (url.pathname === "/api/v1/operations/overview" && method === "GET") return json(route, { summary: { attention: 0 }, items: [] });
    if (url.pathname === "/api/v1/search/assist" && method === "POST") return json(route, { query: "", canonical_query: "", parsed: { tokens: [] }, suggestions: [] });
    unhandled.push(`${method} ${url.pathname}${url.search}`);
    return json(route, { detail: `Unhandled fixture request: ${method} ${url.pathname}` }, 501);
  });
  const page = await context.newPage();
  await page.goto(pathname);
  return { context, page, taskDetailIds, unhandled };
}

async function expectCanonicalTask(page: Page, taskDetailIds: string[]) {
  await expect(page).toHaveURL(new RegExp(`/admin/jobs\\?tab=admin&task=${TASK_ID}$`), { timeout: 30_000 });
  await expect(page.getByText("Canonical D39 task", { exact: true })).toBeVisible();
  expect(new Set(taskDetailIds)).toEqual(new Set([TASK_ID]));
}

const dataActions = [
  { section: "Import from disk", button: "Import", endpoint: "/api/v1/admin/library/import-from-disk", operationType: "admin-disk-import" },
  { section: "Re-enrich creators", button: "Re-enrich", endpoint: "/api/v1/admin/creators/re-enrich", operationType: "admin-creator-reenrich" },
] as const;

for (const action of dataActions) {
  test(`${action.section} links its accepted transport to the canonical task`, async ({ browser }) => {
    const operationPollIds: string[] = [];
    const opened = await openFixture(browser, "/admin/data-mgmt", async (route, url, method) => {
      if (url.pathname === "/api/v1/admin/system-info" && method === "GET") return json(route, { version: "fixture", downloads_size_mb: 0, library_size_mb: 0, downloads_free_gb: 1, archives_kb: {}, db_stats: {} }).then(() => true);
      if (url.pathname === "/api/v1/admin/storage-breakdown" && method === "GET") return json(route, { sources: {}, creator_tree: [], unlinked_repositories: [], db_stats: {} }).then(() => true);
      if (url.pathname === "/api/v1/admin/backup/list" && method === "GET") return json(route, { backups: [] }).then(() => true);
      if ([
        "/api/v1/admin/backup/latest",
        "/api/v1/admin/integrity-check/latest",
        "/api/v1/admin/cleanup-metadata-jsons/latest",
        "/api/v1/admin/library/rebuild/latest",
        "/api/v1/admin/library/import-from-disk/latest",
        "/api/v1/admin/creators/re-enrich/latest",
        "/api/v1/admin/operations/clear/latest",
      ].includes(url.pathname) && method === "GET") return json(route, { current: null, snapshot: null }).then(() => true);
      if (url.pathname === action.endpoint && method === "POST") {
        await json(route, { task_id: TASK_ID, job_id: RQ_JOB_ID, status: "enqueued", operation_type: action.operationType, message: `${action.section} queued` }, 202);
        return true;
      }
      if ((url.pathname === `/api/v1/admin/operations/${RQ_JOB_ID}` || url.pathname === `/api/v1/admin/operations/${TASK_ID}`) && method === "GET") {
        operationPollIds.push(url.pathname.slice("/api/v1/admin/operations/".length));
        await json(route, { task_id: TASK_ID, job_id: TASK_ID, rq_job_id: RQ_JOB_ID, status: "running", operation_type: action.operationType });
        return true;
      }
      return false;
    });
    try {
      await opened.page.getByRole("button", { name: action.button, exact: true }).click();
      await expect.poll(() => operationPollIds.length).toBe(1);
      // A page-level confirmation timer used to add another request after 1s.
      await opened.page.waitForTimeout(1500);
      expect(operationPollIds).toEqual([TASK_ID]);
      await opened.page.getByRole("button", { name: "Task detail", exact: true }).click();
      await expectCanonicalTask(opened.page, opened.taskDetailIds);
      expect(new Set(operationPollIds)).toEqual(new Set([TASK_ID]));
      expect(opened.unhandled).toEqual([]);
    } finally {
      await opened.context.close();
    }
  });
}

for (const terminal of ["complete", "404", "410"] as const) {
  test(`data-management shared monitor handles ${terminal} once`, async ({ browser }) => {
    let polls = 0;
    let infoReads = 0;
    const ids: string[] = [];
    const opened = await openFixture(browser, "/admin/data-mgmt", async (route, url, method) => {
      if (url.pathname === "/api/v1/admin/system-info") {
        infoReads++;
        return json(route, { version: "fixture", downloads_size_mb: 0, library_size_mb: 0, downloads_free_gb: 1, archives_kb: {}, db_stats: {} }).then(() => true);
      }
      if (url.pathname === "/api/v1/admin/storage-breakdown") return json(route, { sources: {}, creator_tree: [], unlinked_repositories: [], db_stats: {} }).then(() => true);
      if (url.pathname === "/api/v1/admin/backup/list") return json(route, { backups: [] }).then(() => true);
      if (url.pathname.endsWith("/latest")) return json(route, { current: null, snapshot: null }).then(() => true);
      if (url.pathname === "/api/v1/admin/library/import-from-disk" && method === "POST") {
        return json(route, { task_id: TASK_ID, job_id: RQ_JOB_ID, status: "enqueued", operation_type: "admin-disk-import" }, 202).then(() => true);
      }
      if (url.pathname.startsWith("/api/v1/admin/operations/")) {
        ids.push(url.pathname.split("/").at(-1)!);
        polls++;
        if (polls === 1) return json(route, { task_id: TASK_ID, status: "running", operation_type: "admin-disk-import" }).then(() => true);
        return (terminal === "complete"
          ? json(route, { task_id: TASK_ID, status: "complete", operation_type: "admin-disk-import", result: { message: "Fixture finished" } })
          : json(route, { detail: "Operation missing" }, Number(terminal))).then(() => true);
      }
      return false;
    });
    const errors: string[] = [];
    opened.page.on("pageerror", error => errors.push(error.message));
    try {
      await opened.page.getByRole("button", { name: "Import", exact: true }).click();
      await expect.poll(() => polls, { timeout: 20_000 }).toBe(2);
      await expect.poll(() => opened.page.evaluate(() => sessionStorage.getItem("admin_operation_job"))).toBeNull();
      await expect(opened.page.getByRole("button", { name: "Import", exact: true })).toBeEnabled();
      await opened.page.waitForTimeout(11_000);
      expect(polls).toBe(2);
      expect(ids).toEqual([TASK_ID, TASK_ID]);
      expect(infoReads).toBe(terminal === "complete" ? 2 : 1);
      expect(errors).toEqual([]);
      expect(opened.unhandled).toEqual([]);
    } finally { await opened.context.close(); }
  });
}

for (const entry of ["row", "drawer", "batch", "late-response"] as const) {
  test(`management failure retried from Jobs ${entry} restarts its monitor`, async ({ browser }) => {
    let started = false;
    let attempt = 1;
    let done = false;
    let retries = 0;
    let secondPolls = 0;
    let infoReads = 0;
    let firstPolls = 0;
    let oldResponse: Route | null = null;
    const task = () => ({ id: TASK_ID, kind: "admin", operation_type: "admin-disk-import", title: "Cross-entry retry",
      status: attempt === 1 ? "failed" : done ? "complete" : "running", available_actions: attempt === 1 ? ["retry"] : [],
      created_at: "2026-10-03T00:00:00Z", updated_at: "2026-10-03T00:00:00Z" });
    const opened = await openFixture(browser, "/admin/data-mgmt", async (route, url, method) => {
      if (url.pathname === "/api/v1/admin/system-info") {
        infoReads++;
        return json(route, { version: "fixture", downloads_size_mb: 0, library_size_mb: 0, downloads_free_gb: 1, archives_kb: {}, db_stats: {} }).then(() => true);
      }
      if (url.pathname === "/api/v1/admin/storage-breakdown") return json(route, { sources: {}, creator_tree: [], unlinked_repositories: [], db_stats: {} }).then(() => true);
      if (url.pathname === "/api/v1/admin/backup/list") return json(route, { backups: [] }).then(() => true);
      if (url.pathname.endsWith("/latest")) return json(route, { current: null, snapshot: null }).then(() => true);
      if (url.pathname === "/api/v1/admin/library/import-from-disk" && method === "POST") {
        started = true;
        return json(route, { task_id: TASK_ID, job_id: RQ_JOB_ID, status: "enqueued", operation_type: "admin-disk-import" }, 202).then(() => true);
      }
      if (url.pathname === `/api/v1/tasks/${TASK_ID}/retry`) {
        retries++; attempt = 2;
        // Intentionally omit job_id/operation_type: generic retry promises neither.
        return json(route, { task_id: TASK_ID, status: "enqueued" }).then(() => true);
      }
      if (url.pathname === `/api/v1/tasks/${TASK_ID}`) return json(route, task()).then(() => true);
      if (url.pathname === "/api/v1/tasks") return json(route, { total: started ? 1 : 0, items: started ? [task()] : [] }).then(() => true);
      if (url.pathname.startsWith("/api/v1/admin/operations/")) {
        if (attempt === 1 && entry === "late-response" && ++firstPolls > 1) {
          oldResponse = route;
          return true;
        }
        if (attempt === 2) secondPolls++;
        return json(route, { task_id: TASK_ID, operation_type: "admin-disk-import", status: attempt === 1 && entry === "late-response" ? "running" : task().status,
          error: attempt === 1 ? "Attempt 1 failed" : null,
          progress: { phase: "running", label: `Attempt ${attempt} running` },
          result: done ? { message: "Second attempt result" } : null }).then(() => true);
      }
      return false;
    });
    opened.page.on("dialog", dialog => void dialog.accept());
    try {
      await opened.page.getByRole("button", { name: "Import", exact: true }).click();
      await expect(opened.page.locator('[data-admin-operation="admin-disk-import"]')).toContainText(entry === "late-response" ? "Attempt 1 running" : "Attempt 1 failed");
      await opened.page.getByRole("button", { name: "Task detail", exact: true }).click();
      const drawer = opened.page.locator('aside[aria-label="Task detail"]');
      await expect(drawer).toContainText("Cross-entry retry");
      if (entry === "drawer") await drawer.getByRole("button", { name: "Retry", exact: true }).click();
      await drawer.getByRole("button", { name: "Close", exact: true }).click();
      await expect(drawer).not.toBeVisible();
      if (entry === "late-response") await expect.poll(() => !!oldResponse, { timeout: 15_000 }).toBe(true);
      if (entry === "row" || entry === "late-response") await opened.page.locator("#main-content").getByRole("button", { name: "Retry", exact: true }).click();
      if (entry === "batch") {
        await opened.page.getByRole("button", { name: "Batch actions", exact: true }).click();
        await opened.page.getByRole("checkbox", { name: /11111111/ }).check();
        await opened.page.getByRole("combobox", { name: "Batch action" }).selectOption("retry");
        await opened.page.getByRole("button", { name: "Apply", exact: true }).click();
      }
      await expect.poll(() => retries).toBe(1);
      await opened.page.locator('a[href="/admin/data-mgmt"]').first().click();
      const status = opened.page.locator('[data-admin-operation="admin-disk-import"]');
      await expect(status).toContainText("Attempt 2 running");
      if (oldResponse) await (oldResponse as Route).fulfill({ json: { task_id: TASK_ID, status: "failed", error: "Late attempt 1" } }).catch(() => {});
      await opened.page.waitForTimeout(1200);
      await expect(status).toContainText("Attempt 2 running");
      expect(secondPolls).toBe(1);
      const before = infoReads;
      done = true;
      await expect(status).toContainText(/complete/i, { timeout: 15_000 });
      await expect.poll(() => infoReads).toBe(before + 1);
      await expect.poll(() => opened.page.evaluate(() => sessionStorage.getItem("admin_operation_job"))).toBeNull();
      const completedReads = infoReads;
      await opened.page.locator('a[href="/admin/jobs"]').first().click();
      await opened.page.locator('a[href="/admin/data-mgmt"]').first().click();
      await expect(status).toContainText(/complete/i);
      await opened.page.waitForTimeout(500);
      expect(infoReads).toBeLessThanOrEqual(completedReads + 1);
      expect(secondPolls).toBe(2);
      expect(opened.unhandled).toEqual([]);
    } finally { await opened.context.close(); }
  });
}

test("Danbooru mapping refresh exposes a canonical task action while polling its transport", async ({ browser }) => {
  const operationPollIds: string[] = [];
  const opened = await openFixture(browser, "/admin/upload/danbooru", async (route, url, method) => {
    if (url.pathname === "/api/v1/reference/danbooru/mappings/refresh" && method === "POST") {
      await json(route, { task_id: TASK_ID, job_id: RQ_JOB_ID, rq_job_id: RQ_JOB_ID, status: "enqueued", operation_type: "danbooru-mapping-refresh", message: "queued" }, 202);
      return true;
    }
    if (url.pathname === `/api/v1/reference/danbooru/mappings/refresh/${RQ_JOB_ID}` && method === "GET") {
      operationPollIds.push(RQ_JOB_ID);
      await json(route, { task_id: TASK_ID, job_id: RQ_JOB_ID, rq_job_id: RQ_JOB_ID, status: "running", operation_type: "danbooru-mapping-refresh" });
      return true;
    }
    return false;
  });
  try {
    await opened.page.getByRole("button", { name: "Refresh all mappings", exact: true }).click();
    await opened.page.getByRole("button", { name: "View task", exact: true }).click();
    await expectCanonicalTask(opened.page, opened.taskDetailIds);
    await expect.poll(() => operationPollIds.length).toBeGreaterThan(0);
    expect(new Set(operationPollIds)).toEqual(new Set([RQ_JOB_ID]));
    expect(opened.unhandled).toEqual([]);
  } finally {
    await opened.context.close();
  }
});

test("Danbooru import-all preserves its canonical task action", async ({ browser }) => {
  const operationPollIds: string[] = [];
  const opened = await openFixture(browser, "/admin/upload/danbooru", async (route, url, method) => {
    if (url.pathname === "/api/v1/creators" && method === "GET") {
      await json(route, { items: [], total: 0 });
      return true;
    }
    if (url.pathname === "/api/v1/reference/danbooru/artist/preview" && method === "POST") {
      await json(route, {
        status: "ok",
        found: true,
        artist: { id: 39, name: "d39_artist", other_names: [], urls: [] },
        suggested_links: [],
      });
      return true;
    }
    if (url.pathname === "/api/v1/reference/danbooru/artist/import-all/async" && method === "POST") {
      await json(route, { task_id: TASK_ID, job_id: RQ_JOB_ID, status: "enqueued" }, 202);
      return true;
    }
    if (url.pathname === `/api/v1/admin/operations/${TASK_ID}` && method === "GET") {
      operationPollIds.push(TASK_ID);
      await json(route, { task_id: TASK_ID, job_id: TASK_ID, rq_job_id: RQ_JOB_ID, status: "running", operation_type: "danbooru-import-all" });
      return true;
    }
    return false;
  });
  try {
    const search = opened.page.getByRole("heading", { name: "Search by Artist Name" }).locator("..");
    await search.getByRole("textbox").fill("d39_artist");
    await search.getByRole("button", { name: "Search Danbooru" }).click();
    await opened.page.getByRole("button", { name: "Import All & Subscribe", exact: true }).click();
    await opened.page.getByRole("button", { name: "View task", exact: true }).click();
    await expectCanonicalTask(opened.page, opened.taskDetailIds);
    await expect.poll(() => operationPollIds.length).toBeGreaterThan(0);
    expect(new Set(operationPollIds)).toEqual(new Set([TASK_ID]));
    expect(opened.unhandled).toEqual([]);
  } finally {
    await opened.context.close();
  }
});

test("a restored operation learns its canonical task before the notification entry navigates", async ({ browser }) => {
  const opened = await openFixture(browser, "/admin/tags", async (route, url, method) => {
    if (url.pathname === "/api/v1/tags/page" && method === "GET") { await json(route, { total: 0, offset: 0, limit: 100, items: [] }); return true; }
    if (url.pathname === `/api/v1/admin/operations/${RQ_JOB_ID}` && method === "GET") {
      await json(route, { task_id: TASK_ID, job_id: TASK_ID, rq_job_id: RQ_JOB_ID, status: "complete", operation_type: "admin-disk-import", result: { message: "done" } });
      return true;
    }
    return false;
  }, async (context) => {
    await context.addInitScript(({ jobId }) => {
      sessionStorage.setItem("admin_operation_job", JSON.stringify({ jobId, kind: "admin-disk-import", title: "Restored disk import", startedAt: Date.now() }));
    }, { jobId: RQ_JOB_ID });
  });
  try {
    await opened.page.getByRole("button", { name: "Notifications", exact: true }).click();
    const entry = opened.page.getByText("Restored disk import", { exact: true });
    await expect(entry).toBeVisible();
    await expect(opened.page.getByText("done", { exact: true })).toBeVisible();
    await entry.click();
    await expectCanonicalTask(opened.page, opened.taskDetailIds);
    expect(opened.unhandled).toEqual([]);
  } finally {
    await opened.context.close();
  }
});

test("a retained legacy operation without a task UUID never builds a Jobs task link", async ({ browser }) => {
  const opened = await openFixture(browser, "/admin/tags", async (route, url, method) => {
    if (url.pathname === "/api/v1/tags/page" && method === "GET") { await json(route, { total: 0, offset: 0, limit: 100, items: [] }); return true; }
    if (url.pathname === "/api/v1/admin/operations/legacy-transport-id" && method === "GET") {
      await json(route, { job_id: "legacy-transport-id", status: "complete", operation_type: "admin-disk-import", result: { message: "done" } });
      return true;
    }
    return false;
  }, async (context) => {
    await context.addInitScript(() => {
      sessionStorage.setItem("admin_operation_job", JSON.stringify({ jobId: "legacy-transport-id", kind: "admin-disk-import", title: "Legacy disk import", startedAt: Date.now() }));
    });
  });
  try {
    await opened.page.getByRole("button", { name: "Notifications", exact: true }).click();
    const entry = opened.page.getByText("Legacy disk import", { exact: true });
    await expect(entry).toBeVisible();
    await expect(opened.page.getByText("done", { exact: true })).toBeVisible();
    await expect(entry.locator("xpath=ancestor::div[contains(@class,'border-b')][1]")).not.toHaveClass(/cursor-pointer/);
    const before = opened.page.url();
    await entry.click();
    await opened.page.waitForTimeout(250);
    expect(opened.page.url()).toBe(before);
    expect(opened.taskDetailIds).toEqual([]);
    expect(opened.unhandled).toEqual([]);
  } finally {
    await opened.context.close();
  }
});

import { expect, test, type Browser, type BrowserContext, type Route } from "@playwright/test";

test.describe.configure({ timeout: 120_000 });

const principal = {
  id: 290,
  username: "data-center-fixture",
  display_name: "Data Center Fixture",
  is_admin: false,
  is_active: true,
  permissions: ["system", "tasks"],
  modules: { system: "System", tasks: "Tasks" },
  preferences: {},
  nsfw_visible: true,
  upload_used_bytes: 0,
  must_change_password: false,
};

const operationTypes = [
  "admin-integrity-scan",
  "admin-cleanup-metadata-jsons",
  "admin-rebuild",
  "admin-disk-import",
  "admin-creator-reenrich",
  "admin-backup-create",
  "admin-clear",
] as const;

const latestPaths: Record<(typeof operationTypes)[number], string> = {
  "admin-integrity-scan": "/api/v1/admin/integrity-check/latest",
  "admin-cleanup-metadata-jsons": "/api/v1/admin/cleanup-metadata-jsons/latest",
  "admin-rebuild": "/api/v1/admin/library/rebuild/latest",
  "admin-disk-import": "/api/v1/admin/library/import-from-disk/latest",
  "admin-creator-reenrich": "/api/v1/admin/creators/re-enrich/latest",
  "admin-backup-create": "/api/v1/admin/backup/latest",
  "admin-clear": "/api/v1/admin/operations/clear/latest",
};

const json = (route: Route, body: unknown, status = 200) => route.fulfill({
  status,
  contentType: "application/json",
  body: JSON.stringify(body),
});

type Handler = (route: Route, url: URL, method: string) => Promise<boolean>;

async function openFixture(browser: Browser, handler: Handler) {
  const context = await browser.newContext();
  await context.routeWebSocket("**/api/v1/ws*", (socket) => socket.close({ code: 1000, reason: "fixture" }));
  await context.addCookies([{ name: "ag_session", value: "fixture", domain: "127.0.0.1", path: "/" }, { name: "ag_csrf", value: "fixture-csrf", url: process.env.PLAYWRIGHT_BASE_URL || "http://127.0.0.1:13000" }]);
  await context.addInitScript(() => {
    localStorage.setItem("ag_token", "fixture");
    localStorage.setItem("auto-gallery-lang", "en");
  });
  const unhandled: string[] = [];
  await context.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const method = request.method();
    if (await handler(route, url, method)) return;
    if (url.pathname === "/api/v1/auth/me" && method === "GET") return void await json(route, principal);
    if (url.pathname === "/api/v1/auth/ws-ticket" && method === "POST") return void await json(route, { detail: "fixture websocket unavailable" }, 503);
    if (url.pathname === "/api/v1/admin/system-info" && method === "GET") return void await json(route, {
      version: "fixture", downloads_size_mb: 0, library_size_mb: 0, downloads_free_gb: 1, archives_kb: {},
      db_stats: { works: 0, assets: 0, creators: 0, subscriptions: 0, tags: 0 },
    });
    if (url.pathname === "/api/v1/admin/storage-breakdown" && method === "GET") return void await json(route, {
      sources: {}, creator_tree: [], unlinked_repositories: [],
      db_stats: { works: 0, assets: 0, creators: 0, subscriptions: 0, tags: 0 },
    });
    if (url.pathname === "/api/v1/admin/backup/list" && method === "GET") return void await json(route, { backups: [] });
    if (Object.values(latestPaths).includes(url.pathname) && method === "GET") return void await json(route, { current: null, snapshot: null });
    if (url.pathname === "/api/v1/tasks" && method === "GET") return void await json(route, { total: 0, items: [] });
    if (url.pathname === "/api/v1/system/workbench" && method === "GET") return void await json(route, { updated_at: "2026-09-20T00:00:00Z", queue: {}, scheduler: {}, storage: {}, health: {}, attention: {}, recent: {} });
    if (url.pathname === "/api/v1/system/scheduler-decisions" && method === "GET") return void await json(route, { total: 0, items: [], summary: { blocked_count: 0 } });
    if (url.pathname === "/api/v1/operations/overview" && method === "GET") return void await json(route, { summary: { attention: 0 }, items: [] });
    if (url.pathname === "/api/v1/search/assist" && method === "POST") return void await json(route, { query: "", canonical_query: "", parsed: { tokens: [] }, suggestions: [] });
    unhandled.push(`${method} ${url.pathname}${url.search}`);
    await json(route, { detail: `Unhandled fixture request: ${method} ${url.pathname}` }, 501);
  });
  const page = await context.newPage();
  await page.goto("/admin/data-mgmt");
  return { context, page, unhandled };
}

test("reload restores every failed data-center TaskRun and retries the same durable task", async ({ browser }) => {
  const taskIds = new Map(operationTypes.map((type, index) => [type, `29000000-0000-4000-8000-${String(index + 1).padStart(12, "0")}`]));
  const retried: string[] = [];
  const opened = await openFixture(browser, async (route, url, method) => {
    const operationType = operationTypes.find((type) => latestPaths[type] === url.pathname);
    if (operationType && method === "GET") {
      const taskId = taskIds.get(operationType)!;
      await json(route, {
        snapshot: null,
        current: {
          task_id: taskId,
          job_id: `admin-${taskId}-attempt-1`,
          status: "failed",
          operation_type: operationType,
          progress: { phase: "failed", label: "Operation failed" },
        },
      });
      return true;
    }
    const retryMatch = url.pathname.match(/^\/api\/v1\/admin\/operations\/([^/]+)\/retry$/);
    if (retryMatch && method === "POST") {
      retried.push(retryMatch[1]);
      const type = operationTypes.find((candidate) => taskIds.get(candidate) === retryMatch[1])!;
      await json(route, { task_id: retryMatch[1], job_id: `admin-${retryMatch[1]}-attempt-2`, status: "enqueued", operation_type: type }, 202);
      return true;
    }
    const statusMatch = url.pathname.match(/^\/api\/v1\/admin\/operations\/([^/]+)$/);
    if (statusMatch && method === "GET") {
      const type = operationTypes.find((candidate) => taskIds.get(candidate) === statusMatch[1])!;
      await json(route, {
        task_id: statusMatch[1], job_id: statusMatch[1], rq_job_id: `admin-${statusMatch[1]}-attempt-1`,
        status: "failed", operation_type: type,
        progress: { phase: "failed", label: "Operation failed" },
        error: `${type} retained failure`, reason_code: "task_failed",
      });
      return true;
    }
    return false;
  });
  try {
    for (const type of operationTypes) {
      const section = opened.page.locator(`[data-admin-operation='${type}']`);
      await expect(section.getByRole("alert")).toContainText(`${type} retained failure`);
      await section.getByRole("button", { name: "Retry", exact: true }).click();
    }
    await expect.poll(() => new Set(retried).size).toBe(operationTypes.length);
    expect(new Set(retried)).toEqual(new Set(taskIds.values()));
    expect(opened.unhandled).toEqual([]);
  } finally {
    await opened.context.close();
  }
});

test("metadata cleanup reports acceptance separately and renders terminal counts", async ({ browser }) => {
  const taskId = "29100000-0000-4000-8000-000000000001";
  let statusReads = 0;
  const opened = await openFixture(browser, async (route, url, method) => {
    if (url.pathname === "/api/v1/admin/cleanup-metadata-jsons" && method === "POST") {
      await json(route, { task_id: taskId, job_id: `admin-${taskId}-attempt-1`, status: "enqueued", operation_type: "admin-cleanup-metadata-jsons" }, 202);
      return true;
    }
    if (url.pathname === `/api/v1/admin/operations/${taskId}` || url.pathname === `/api/v1/admin/operations/admin-${taskId}-attempt-1`) {
      statusReads += 1;
      const complete = statusReads >= 2;
      await json(route, {
        task_id: taskId, job_id: taskId, rq_job_id: `admin-${taskId}-attempt-1`,
        status: complete ? "complete" : "running", operation_type: "admin-cleanup-metadata-jsons",
        progress: complete ? { phase: "complete", label: "Cleanup complete" } : { phase: "cleaning", label: "Checking sidecars", current: 1, total: 3 },
        result: complete ? { status: "ok", removed: 1, skipped: 2, failed: 0, message: "Metadata cleanup complete" } : null,
      });
      return true;
    }
    return false;
  });
  try {
    await opened.page.getByRole("button", { name: "Clean JSONs", exact: true }).click();
    await expect(opened.page.getByText("Cleanup accepted; the removed count will be available after the task finishes.", { exact: true })).toBeVisible();
    await expect(opened.page.getByText("Removed 1 · Skipped 2 · Failed 0", { exact: true })).toBeVisible({ timeout: 10_000 });
    await expect(opened.page.locator("[data-admin-operation='admin-cleanup-metadata-jsons']").getByRole("link", { name: "Task detail" }))
      .toHaveAttribute("href", `/admin/jobs?tab=admin&task=${taskId}`);
    expect(opened.unhandled).toEqual([]);
  } finally {
    await opened.context.close();
  }
});

test("disk import preserves reset-ledger intent after rejection and submits it unchanged", async ({ browser }) => {
  const bodies: unknown[] = [];
  let attempts = 0;
  const taskId = "29200000-0000-4000-8000-000000000001";
  const opened = await openFixture(browser, async (route, url, method) => {
    if (url.pathname === "/api/v1/admin/library/import-from-disk" && method === "POST") {
      attempts += 1;
      bodies.push(route.request().postDataJSON());
      if (attempts === 1) await json(route, { detail: "disk queue unavailable" }, 503);
      else await json(route, { task_id: taskId, job_id: `admin-${taskId}-attempt-1`, status: "enqueued", operation_type: "admin-disk-import", message: "Disk import queued" }, 202);
      return true;
    }
    if (url.pathname === `/api/v1/admin/operations/${taskId}` || url.pathname === `/api/v1/admin/operations/admin-${taskId}-attempt-1`) {
      await json(route, { task_id: taskId, job_id: taskId, rq_job_id: `admin-${taskId}-attempt-1`, status: "running", operation_type: "admin-disk-import", progress: { phase: "scanning", label: "Scanning disk" } });
      return true;
    }
    return false;
  });
  try {
    const reset = opened.page.getByRole("checkbox", { name: "Reset ledger (recover creators/works deleted from the DB)" });
    await reset.check();
    const submit = opened.page.getByRole("button", { name: "Import", exact: true });
    await submit.click();
    await expect(opened.page.locator("[data-admin-operation='admin-disk-import']").getByRole("alert")).toContainText("disk queue unavailable");
    await expect(reset).toBeChecked();
    await submit.click();
    await expect(opened.page.locator("[data-admin-operation='admin-disk-import']").getByRole("link", { name: "Task detail" })).toBeVisible();
    expect(bodies).toEqual([{ reset_ledger: true }, { reset_ledger: true }]);
    expect(opened.unhandled).toEqual([]);
  } finally {
    await opened.context.close();
  }
});

test("clear-all cancel is inert and request failure keeps the typed confirmation", async ({ browser }) => {
  const taskId = "29300000-0000-4000-8000-000000000001";
  let attempts = 0;
  const bodies: unknown[] = [];
  const opened = await openFixture(browser, async (route, url, method) => {
    if (url.pathname === "/api/v1/admin/clear/preview/all" && method === "GET") {
      await json(route, { entity: "all", confirmation_phrase: "DELETE-ALL-DATA", counts: { works: 2, assets: 3 }, preserves_repository_sync_receipts: false, deletes_media_files: true });
      return true;
    }
    if (url.pathname === "/api/v1/admin/operations/clear" && method === "POST") {
      attempts += 1;
      bodies.push(route.request().postDataJSON());
      if (attempts === 1) await json(route, { detail: "clear queue unavailable" }, 503);
      else await json(route, { task_id: taskId, job_id: `admin-${taskId}-attempt-1`, status: "enqueued", operation_type: "admin-clear" }, 202);
      return true;
    }
    if (url.pathname === `/api/v1/admin/operations/${taskId}` || url.pathname === `/api/v1/admin/operations/admin-${taskId}-attempt-1`) {
      await json(route, { task_id: taskId, job_id: taskId, rq_job_id: `admin-${taskId}-attempt-1`, status: "running", operation_type: "admin-clear", progress: { phase: "clearing", label: "Clearing all data" }, meta: { entity: "all" } });
      return true;
    }
    return false;
  });
  try {
    const open = opened.page.getByRole("button", { name: "Delete All Data", exact: true });
    await open.click();
    let dialog = opened.page.getByRole("dialog");
    await expect(dialog).toContainText("works: 2");
    await dialog.getByRole("button", { name: "Cancel", exact: true }).click();
    expect(attempts).toBe(0);

    await open.click();
    dialog = opened.page.getByRole("dialog");
    const confirmation = dialog.getByRole("textbox");
    await confirmation.fill("DELETE-ALL-DATA");
    await dialog.getByRole("button", { name: "Confirm", exact: true }).click();
    await expect(dialog.getByRole("alert")).toContainText("clear queue unavailable");
    await expect(confirmation).toHaveValue("DELETE-ALL-DATA");
    await dialog.getByRole("button", { name: "Confirm", exact: true }).click();
    await expect(dialog).toBeHidden();
    await expect(opened.page.locator("[data-admin-operation='admin-clear']").getByRole("link", { name: "Task detail" })).toBeVisible();
    expect(bodies).toEqual([
      { entity: "all", confirmation: "DELETE-ALL-DATA" },
      { entity: "all", confirmation: "DELETE-ALL-DATA" },
    ]);
    expect(opened.unhandled).toEqual([]);
  } finally {
    await opened.context.close();
  }
});

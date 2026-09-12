import { expect, test, type BrowserContext, type Route } from "@playwright/test";

test.describe.configure({ timeout: 60_000 });

type FixtureOptions = {
  post?: "accepted" | "conflict" | "network-once";
  language?: "en" | "zh";
  taskStates?: Array<Record<string, unknown>>;
  taskReadDelayMs?: number;
  taskReadError?: { status?: number; network?: boolean; count?: number };
  itemReadError?: { status?: number; network?: boolean; count?: number };
  itemPage?: (offset: number, limit: number, read: number) => { total: number; items: Array<Record<string, unknown>> };
  decisionsPage?: (url: URL) => Record<string, unknown>;
};

const systemUser = {
  id: 7,
  username: "system-operator",
  display_name: "System Operator",
  is_admin: false,
  is_active: true,
  permissions: ["system"],
  modules: { system: true, tasks: false },
  preferences: {},
  nsfw_visible: false,
  upload_quota_bytes: null,
  upload_used_bytes: 0,
  must_change_password: false,
};

function json(route: Route, body: unknown, status = 200) {
  return route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
}

async function installSchedulerFixtures(context: BrowserContext, options: FixtureOptions = {}) {
  const posts: Array<Record<string, unknown>> = [];
  let postAttempts = 0;
  let taskReads = 0;
  let taskSuccessReads = 0;
  let itemReads = 0;
  let wsTicketReads = 0;
  const taskStates = options.taskStates || [{
    id: "task-accepted",
    kind: "admin",
    operation_type: "subscription-sync-batch",
    status: "running",
    progress_stage: "downloading",
    progress_current: 2,
    progress_total: 4,
    progress_data: { phase: "downloading", current: 2, total: 4 },
    result_data: {
      status: "pending", mode: "manual_all_enabled", candidate_count: 4,
      pending_count: 0, queued_count: 0, waiting_count: 0, downloading_count: 2,
      importing_count: 0, succeeded_count: 0, skipped_count: 1, failed_count: 1,
      cancelled_count: 0, enqueued_count: 3, error_count: 1,
    },
  }, {
    id: "task-accepted",
    kind: "admin",
    operation_type: "subscription-sync-batch",
    status: "failed",
    progress_stage: "failed",
    progress_current: 4,
    progress_total: 4,
    progress_data: { phase: "failed", current: 4, total: 4 },
    result_data: {
      status: "partial_error", mode: "manual_all_enabled", candidate_count: 4,
      pending_count: 0, queued_count: 0, waiting_count: 0, downloading_count: 0,
      importing_count: 0, succeeded_count: 2, skipped_count: 1, failed_count: 1,
      cancelled_count: 0, enqueued_count: 3, error_count: 1,
      skipped_reasons: { auth_unhealthy: 1 },
    },
  }];

  await context.addCookies([{
    name: "ag_token",
    value: "scheduler-fixture-token",
    domain: "127.0.0.1",
    path: "/",
  }]);
  await context.addInitScript((language) => {
    localStorage.setItem("ag_token", "scheduler-fixture-token");
    localStorage.setItem("auto-gallery-lang", language);
    localStorage.setItem("auto-gallery-theme", "dark");
  }, options.language || "en");
  await context.route("https://fonts.loli.net/**", (route) => route.fulfill({
    status: 200,
    contentType: "text/css",
    body: "",
  }));
  await context.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    if (path === "/api/v1/auth/me") return json(route, systemUser);
    if (path === "/api/v1/auth/ws-ticket") {
      wsTicketReads += 1;
      return json(route, { detail: "fixture websocket unavailable" }, 503);
    }
    if (path === "/api/v1/system/queue-stats") return json(route, {
      default_queue: 0, scheduled_queue: 0, failed_jobs: 0, scheduler_enabled: true,
      scheduler_loop: { status: "scheduled", active: { queued: 0, scheduled: 0, started: 0 } },
    });
    if (path === "/api/v1/system/scheduler-decisions") return json(route, options.decisionsPage?.(url) || {
      updated_at: "2026-09-08T00:00:00Z", scheduler_enabled: true, timezone: "Asia/Shanghai", view: url.searchParams.get("view") || "all", total: 0, offset: Number(url.searchParams.get("offset") || 0), limit: Number(url.searchParams.get("limit") || 25), next_offset: null, summary: { blocked_count: 0, overdue_count: 0, oldest_overdue_at: null }, suppressed_count: 0, items: [],
    });
    if (path === "/api/v1/admin/scheduler/sync-now" && request.method() === "POST") {
      postAttempts += 1;
      posts.push(JSON.parse(request.postData() || "{}"));
      if (options.post === "network-once" && postAttempts === 1) return route.abort("connectionreset");
      if (options.post === "conflict") return json(route, {
        detail: { code: "batch_active", message: "A subscription sync batch is already active", task_id: "task-existing" },
      }, 409);
      return json(route, {
        task_id: "task-accepted", job_id: "rq-accepted", status: "enqueued",
        operation_type: "subscription-sync-batch", mode: posts.at(-1)?.mode,
      }, 202);
    }
    if (/^\/api\/v1\/tasks\/[^/]+$/.test(path)) {
      taskReads += 1;
      if (options.taskReadError && taskReads <= (options.taskReadError.count ?? Number.POSITIVE_INFINITY)) {
        if (options.taskReadError.network) return route.abort("connectionreset");
        const status = options.taskReadError.status || 503;
        return json(route, { detail: `Task fixture ${status}` }, status);
      }
      if (options.taskReadDelayMs) await new Promise((resolve) => setTimeout(resolve, options.taskReadDelayMs));
      const state = taskStates[Math.min(taskSuccessReads, taskStates.length - 1)];
      taskSuccessReads += 1;
      return json(route, { ...state, id: path.split("/").at(-1) });
    }
    if (/^\/api\/v1\/admin\/scheduler\/batches\/[^/]+\/items$/.test(path)) {
      itemReads += 1;
      if (options.itemReadError && itemReads <= (options.itemReadError.count ?? Number.POSITIVE_INFINITY)) {
        if (options.itemReadError.network) return route.abort("connectionreset");
        const status = options.itemReadError.status || 503;
        return json(route, { detail: `Item fixture ${status}` }, status);
      }
      const offset = Number(url.searchParams.get("offset") || 0);
      const limit = Number(url.searchParams.get("limit") || 50);
      if (options.itemPage) return json(route, options.itemPage(offset, limit, itemReads));
      return json(route, {
        total: 2,
        items: [
          { id: "item-1", source_id: "source-1", source: "pixiv", status: "succeeded", attempts: 1, next_retry_at: null, download_job_id: "download-1", reason_code: null, error: null, outcome: { import_status: "complete" } },
          { id: "item-2", source_id: "source-2", source: "x", status: "failed", attempts: 2, next_retry_at: null, download_job_id: "download-2", reason_code: "import_failed", error: "Main import failed", outcome: { import_status: "failed" } },
        ],
      });
    }
    if (/^\/api\/v1\/tasks\/[^/]+\/cancel$/.test(path)) return json(route, {
      task_id: path.split("/").at(-2), status: "cancelled", cleanup_pending: true, cleanup_task_id: "cleanup-task",
    });
    if (path === "/api/v1/tasks") return json(route, { total: 0, items: [] });
    if (path === "/api/v1/system/workbench") return json(route, {
      updated_at: "2026-09-08T00:00:00Z",
      queue: {
        default: 0, scheduled: 0, failed: 0, active_download_count: 0, active_import_count: 0,
        failed_download_count: 0, failed_import_count: 0, stale_download_count: 0, stale_import_count: 0, stale_count: 0,
      },
      scheduler: { enabled: true, mode: "interval", timezone: "Asia/Shanghai", scan_interval_minutes: 15 },
      storage: {
        disk_total_bytes: 1_000_000, disk_free_bytes: 800_000, disk_used_bytes: 200_000,
        disk_used_percent: 20, disk_free_percent: 80, risk_level: "ok",
      },
      health: {},
      attention: {
        auth_unhealthy_count: 0, failed_download_count: 0, failed_import_count: 0, stale_job_count: 0,
        low_disk_warning: false, scheduler_disabled_warning: false,
      },
      recent: { download_jobs: [], import_jobs: [], tasks: [], works: [], successful_syncs: [] },
    });
    return json(route, { detail: `Unhandled scheduler fixture route: ${path}` }, 501);
  });
  return {
    posts,
    postAttempts: () => postAttempts,
    taskReads: () => taskReads,
    itemReads: () => itemReads,
    wsTicketReads: () => wsTicketReads,
  };
}

test("scheduler accepts a stable intent and restores durable partial progress after reload", async ({ context, page }) => {
  const consoleErrors: Array<{ text: string; url: string }> = [];
  page.on("console", (message) => {
    if (message.type() === "error") consoleErrors.push({ text: message.text(), url: message.location().url });
  });
  const fixture = await installSchedulerFixtures(context);
  await page.setViewportSize({ width: 1440, height: 960 });
  await page.goto("/admin/scheduler");
  await expect(page).toHaveTitle("auto-gallery Admin");
  await expect(page.getByRole("heading", { level: 1, name: "Scheduler & Sync" })).toBeVisible();
  await page.getByRole("button", { name: "Sync all enabled sources" }).click();

  await expect.poll(() => fixture.posts.length).toBe(1);
  expect(fixture.posts[0].mode).toBe("manual_all_enabled");
  expect(fixture.posts[0].request_id).toMatch(/^[0-9a-f-]{36}$/);
  await expect(page.getByRole("log").getByText("Batch accepted", { exact: true })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Current sync batch" })).toBeVisible();
  await expect(page.getByText("Downloading 2")).toBeVisible();

  await page.reload();
  await expect(page.getByRole("heading", { name: "Current sync batch" })).toBeVisible();
  await expect(page.getByText("Batch finished with failed sources")).toBeVisible();
  await expect(page.getByText("2 succeeded", { exact: true })).toBeVisible();
  await expect(page.getByText("1 skipped", { exact: true })).toBeVisible();
  await expect(page.getByText("Auth unhealthy: 1", { exact: true })).toBeVisible();
  await expect(page.getByText("1 failed", { exact: true })).toBeVisible();
  await expect(page.getByText("Main import failed")).toBeVisible();
  await expect(page.getByText("All sources synced successfully")).toHaveCount(0);
  await expect(page.getByText("Something went wrong")).toHaveCount(0);
  await page.screenshot({ path: "/evidence/frontend-task2/scheduler-partial-result.png", fullPage: false });
  const unexpectedConsoleErrors = consoleErrors.filter((message) => {
    const expectedFixtureFailure = message.url.includes("/api/v1/ws")
      || message.url.includes("/api/v1/auth/ws-ticket")
      || message.text.includes("WebSocket connection to 'ws://127.0.0.1:13000/api/v1/ws'");
    return !expectedFixtureFailure;
  });
  expect(unexpectedConsoleErrors).toEqual([]);
});

test("scheduler renders backend lifecycle phases in Chinese", async ({ context, page }) => {
  await installSchedulerFixtures(context, {
    language: "zh",
    taskStates: [{
      id: "task-accepted", kind: "admin", operation_type: "subscription-sync-batch", status: "running",
      progress_stage: "waiting", progress_current: 0, progress_total: 3,
      progress_data: { phase: "waiting", label: "Waiting for infrastructure recovery", current: 0, total: 3 },
      result_data: { status: "pending", mode: "due_scan", candidate_count: 3, waiting_count: 3, waiting_reason: "infrastructure_unavailable" },
    }],
  });
  await page.goto("/admin/scheduler");
  await page.getByRole("button", { name: "运行调度扫描" }).click();
  await expect(page.getByText("等待基础设施恢复", { exact: true })).toBeVisible();
  await expect(page.getByText("Waiting for infrastructure recovery", { exact: true })).toHaveCount(0);
});

test("scheduler loads a notable outcome beyond the first bounded item page", async ({ context, page }) => {
  const items = Array.from({ length: 51 }, (_, index) => ({
    id: `item-${index + 1}`,
    source_id: `source-${index + 1}`,
    source: "pixiv",
    status: index === 50 ? "failed" : "succeeded",
    attempts: 1,
    next_retry_at: null,
    download_job_id: `download-${index + 1}`,
    reason_code: index === 50 ? "child_failed" : null,
    error: index === 50 ? "Failure after first page" : null,
    outcome: { status: index === 50 ? "failed" : "complete" },
  }));
  const fixture = await installSchedulerFixtures(context, {
    taskStates: [{
      id: "task-accepted", kind: "admin", operation_type: "subscription-sync-batch", status: "failed",
      progress_stage: "failed", progress_current: 51, progress_total: 51,
      progress_data: { phase: "partial_error", label: "Some subscription sources failed", current: 51, total: 51 },
      result_data: { status: "partial_error", mode: "manual_all_enabled", candidate_count: 51, succeeded_count: 50, failed_count: 1 },
    }],
    itemPage: (offset, limit) => ({ total: items.length, items: items.slice(offset, offset + limit) }),
  });
  await page.goto("/admin/scheduler");
  await page.getByRole("button", { name: "Sync all enabled sources" }).click();
  await expect(page.getByText("50 of 51 item details loaded", { exact: true })).toBeVisible();
  await expect(page.getByText("Failure after first page")).toHaveCount(0);
  await page.getByRole("button", { name: "Load more batch details" }).click();
  await expect(page.getByText("Failure after first page")).toBeVisible();
  expect(fixture.itemReads()).toBeGreaterThanOrEqual(2);
});

test("scheduler exposes and recovers from a transient task tracking error", async ({ context, page }) => {
  const fixture = await installSchedulerFixtures(context, { taskReadError: { network: true, count: 1 } });
  await page.goto("/admin/scheduler");
  await page.getByRole("button", { name: "Run scheduler scan" }).click();
  await expect(page.getByText("Batch status could not be refreshed.", { exact: true })).toBeVisible();
  await expect(page.getByText("The network is unavailable. The saved batch reference was retained for a safe retry.", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Run scheduler scan" })).toBeDisabled();
  await page.getByRole("button", { name: "Retry batch status" }).click();
  await expect(page.getByText("Downloading 2", { exact: true })).toBeVisible();
  expect(fixture.posts).toHaveLength(1);
});

test("scheduler exposes and recovers from an item-detail read error", async ({ context, page }) => {
  const fixture = await installSchedulerFixtures(context, { itemReadError: { status: 503, count: 1 } });
  await page.goto("/admin/scheduler");
  await page.getByRole("button", { name: "Run scheduler scan" }).click();
  await expect(page.getByText("Batch outcome details could not be loaded.", { exact: true })).toBeVisible();
  await expect(page.getByText("The server rejected the tracking request (HTTP 503). The saved batch reference was retained.", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Retry batch details" }).click();
  await expect(page.getByText("Main import failed", { exact: true })).toBeVisible();
  expect(fixture.itemReads()).toBeGreaterThanOrEqual(2);
});

for (const status of [403, 404]) {
  test(`scheduler safely clears an irrecoverable ${status} stored task reference`, async ({ context, page }) => {
    const fixture = await installSchedulerFixtures(context, { taskReadError: { status } });
    await page.goto("/admin/scheduler");
    await page.getByRole("button", { name: "Run scheduler scan" }).click();
    await expect(page.getByText("This saved batch can no longer be accessed.", { exact: true })).toBeVisible();
    const startButton = page.getByRole("button", { name: "Run scheduler scan" });
    await expect(startButton).toBeDisabled();
    await page.getByRole("button", { name: "Clear saved batch reference" }).click();
    await expect(page.getByRole("heading", { name: "Current sync batch" })).toHaveCount(0);
    await expect(startButton).toBeEnabled();
    expect(fixture.posts).toHaveLength(1);
  });
}

test("scheduler creates and replays a stable UUID when randomUUID is unavailable", async ({ context, page }) => {
  await context.addInitScript(() => {
    Object.defineProperty(window.crypto, "randomUUID", { configurable: true, value: undefined });
  });
  const fixture = await installSchedulerFixtures(context, { post: "network-once" });
  await page.goto("/admin/scheduler");
  const button = page.getByRole("button", { name: "Run scheduler scan" });
  await button.click();
  await expect(page.getByText("Network error")).toBeVisible();
  await expect(button).toBeEnabled();
  await page.reload();
  await expect.poll(() => fixture.posts.length).toBe(2);
  expect(fixture.posts[0].request_id).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
  expect(fixture.posts[1].request_id).toBe(fixture.posts[0].request_id);
});

test("scheduler resolves the active batch mode from its task after an actual 409", async ({ context, page }) => {
  const fixture = await installSchedulerFixtures(context, {
    post: "conflict",
    taskReadDelayMs: 8_000,
    taskStates: [{
      id: "task-existing", kind: "admin", operation_type: "subscription-sync-batch", status: "enqueued",
      progress_stage: "enqueued", progress_current: null, progress_total: null,
      result_data: {},
      meta: {
        mode: "due_scan",
        entity: "subscription-sync-batch",
        admin_dispatch: { options: { mode: "due_scan" }, publication_state: "published" },
      },
    }],
  });
  await page.goto("/admin/scheduler");
  await page.getByRole("button", { name: "Sync all enabled sources" }).click();
  await expect(page.getByRole("log").getByText("Opened the active batch")).toBeVisible();
  const batch = page.locator("section").filter({ has: page.getByRole("heading", { name: "Current sync batch" }) });
  await expect(batch).toContainText("Determining batch scope");
  await expect(batch).not.toContainText("All enabled sources");
  const taskLink = page.getByRole("link", { name: "View task" });
  await expect(taskLink).toHaveAttribute("href", /task=task-existing/);
  await expect(batch).toContainText("Due scan", { timeout: 15_000 });
  const stored = await page.evaluate(() => JSON.parse(localStorage.getItem("auto-gallery-scheduler-batch-v1:7") || "null"));
  expect(stored).toMatchObject({ mode: "manual_all_enabled", trackedMode: "due_scan", taskId: "task-existing" });
  expect(stored.requestId).toBe(fixture.posts[0].request_id);
  await page.reload();
  await expect(batch).toContainText("Due scan", { timeout: 15_000 });
  await page.screenshot({ path: "/evidence/frontend-task3/scheduler-conflict-mode.png", fullPage: false });
  expect(fixture.posts).toHaveLength(1);
});

test("terminal settlement refreshes every loaded item page before polling stops", async ({ context, page }) => {
  const readsByOffset = new Map<number, number>();
  const fixture = await installSchedulerFixtures(context, {
    taskReadDelayMs: 3_000,
    taskStates: [{
      id: "task-accepted", kind: "admin", operation_type: "subscription-sync-batch", status: "running",
      progress_stage: "importing", progress_current: 50, progress_total: 51,
      result_data: { status: "pending", mode: "manual_all_enabled", candidate_count: 51, importing_count: 1, succeeded_count: 50 },
    }, {
      id: "task-accepted", kind: "admin", operation_type: "subscription-sync-batch", status: "failed",
      progress_stage: "failed", progress_current: 51, progress_total: 51,
      result_data: { status: "partial_error", mode: "manual_all_enabled", candidate_count: 51, succeeded_count: 50, failed_count: 1 },
    }],
    itemPage: (offset, limit) => {
      const read = (readsByOffset.get(offset) || 0) + 1;
      readsByOffset.set(offset, read);
      const items = Array.from({ length: 51 }, (_, index) => ({
        id: `terminal-item-${index + 1}`,
        source_id: `terminal-source-${index + 1}`,
        source: "pixiv",
        status: index === 50 && read >= 3 ? "failed" : index === 50 ? "importing" : "succeeded",
        attempts: 1,
        next_retry_at: null,
        download_job_id: `terminal-download-${index + 1}`,
        reason_code: index === 50 && read >= 3 ? "import_failed" : null,
        error: index === 50 && read >= 3 ? "Final failure after parent settled" : null,
        outcome: index === 50 && read >= 3 ? { import_status: "failed" } : null,
      }));
      return { total: items.length, items: items.slice(offset, offset + limit) };
    },
  });
  await page.goto("/admin/scheduler");
  await page.getByRole("button", { name: "Sync all enabled sources" }).click();
  await expect(page.getByText("50 of 51 item details loaded", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Load more batch details" }).click();
  await expect(page.getByText("51 of 51 item details loaded", { exact: true })).toBeVisible();
  await expect(page.getByText("Final failure after parent settled", { exact: true })).toBeVisible({ timeout: 25_000 });
  expect(readsByOffset.get(0)).toBeGreaterThanOrEqual(3);
  expect(readsByOffset.get(50)).toBeGreaterThanOrEqual(3);
  expect(fixture.wsTicketReads()).toBeGreaterThan(0);
  await page.screenshot({ path: "/evidence/frontend-task3/scheduler-final-item-refresh.png", fullPage: false });
});

test("cancelled batch remains tracked and refreshes final details after cleanup", async ({ context, page }) => {
  let itemPageReads = 0;
  const fixture = await installSchedulerFixtures(context, { taskReadDelayMs: 1_000, taskStates: [{
    id: "task-accepted", kind: "admin", operation_type: "subscription-sync-batch", status: "running",
    progress_stage: "downloading", progress_current: 1, progress_total: 3,
    result_data: { status: "pending", mode: "manual_all_enabled", candidate_count: 3, succeeded_count: 1, cancelled_count: 0, pending_count: 2, queued_count: 0, waiting_count: 0, downloading_count: 0, importing_count: 0, skipped_count: 0, failed_count: 0 },
  }, {
    id: "task-accepted", kind: "admin", operation_type: "subscription-sync-batch", status: "cancelled",
    progress_stage: "cancelling", progress_current: 1, progress_total: 3,
    result_data: { status: "cancelled", mode: "manual_all_enabled", candidate_count: 3, succeeded_count: 1, cancelled_count: 0, pending_count: 2, queued_count: 0, waiting_count: 0, downloading_count: 0, importing_count: 0, skipped_count: 0, failed_count: 0, cleanup_pending: true, cleanup_task_id: "cleanup-task" },
  }, {
    id: "task-accepted", kind: "admin", operation_type: "subscription-sync-batch", status: "cancelled",
    progress_stage: "cancelled", progress_current: 3, progress_total: 3,
    result_data: { status: "cancelled", mode: "manual_all_enabled", candidate_count: 3, succeeded_count: 1, cancelled_count: 2, pending_count: 0, queued_count: 0, waiting_count: 0, downloading_count: 0, importing_count: 0, skipped_count: 0, failed_count: 0, cleanup_pending: false, cleanup_task_id: "cleanup-task" },
  }], itemPage: (_offset, _limit) => {
    itemPageReads += 1;
    const final = itemPageReads >= 4;
    return { total: 1, items: [{
      id: "cleanup-item", source_id: "cleanup-source", source: "pixiv",
      status: final ? "cancelled" : "waiting", attempts: 1, next_retry_at: null, download_job_id: null,
      reason_code: final ? "batch_cancelled" : null,
      error: final ? "Cancelled after cleanup completed" : null,
      outcome: null,
    }] };
  } });
  await page.goto("/admin/scheduler");
  await page.getByRole("button", { name: "Sync all enabled sources" }).click();
  await expect(page.getByText("1 succeeded", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Cancel batch" }).click();
  await expect(page.getByText("Publication stopped; child cleanup is still running")).toBeVisible();
  await expect(page.getByRole("heading", { name: "Current sync batch" })).toBeVisible();
  await expect.poll(() => fixture.taskReads(), { timeout: 15_000 }).toBeGreaterThanOrEqual(3);
  await expect.poll(() => fixture.wsTicketReads(), { timeout: 15_000 }).toBeGreaterThanOrEqual(2);
  await expect(page.getByText("Publication stopped; child cleanup is still running")).toHaveCount(0);
  await expect(page.getByText("2 cancelled", { exact: true })).toBeVisible();
  await expect(page.getByText("Cancelled after cleanup completed", { exact: true })).toBeVisible();
  expect(itemPageReads).toBeGreaterThanOrEqual(4);
});

test("tasks-only users cannot enter or start the global scheduler", async ({ context, page }) => {
  await installSchedulerFixtures(context);
  await page.route("**/api/v1/auth/me", (route) => json(route, {
    ...systemUser, permissions: ["tasks"], modules: { system: false, tasks: true },
  }));
  await page.goto("/admin/scheduler");
  await expect(page.getByRole("heading", { name: "You don't have permission to access this page" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Run scheduler scan" })).toHaveCount(0);
});

test("scheduler plans page beyond 500 with bounded requests and global summary", async ({ context, page }) => {
  const reads: Array<{ view: string; offset: number; limit: number; q: string }> = [];
  const all = Array.from({ length: 809 }, (_, index) => ({
    subscription_id: `sub-${index}`, subscription_name: `Subscription ${index}`,
    subscription_active: true, subscription_sync_enabled: true,
    creator_id: `creator-${index}`, creator_name: `Creator ${index}`,
    source_id: `source-${index}`, source: "pixiv", source_display_name: "Pixiv",
    source_url: `https://pixiv.net/users/${index}`, source_creator_id: String(index), source_enabled: true,
    effective_mode: "interval", timezone: "Asia/Shanghai", scheduled_times: null, schedule_rule: null,
    sync_interval_hours: 24, last_synced_at: null, last_attempted_at: null,
    due: index % 2 === 0, decision: "eligible", reason: "due", suppression_reason: null,
    next_due_at: "2026-09-07T00:00:00Z", window_start: null, window_end: null,
    auth_healthy: true, url_valid: true, can_download: true, is_overdue: index < 99, is_attention: index < 123,
  }));
  await installSchedulerFixtures(context, { decisionsPage: (url) => {
    const view = url.searchParams.get("view") || "all";
    const q = url.searchParams.get("q") || "";
    const offset = Number(url.searchParams.get("offset") || 0);
    const limit = Number(url.searchParams.get("limit") || 25);
    reads.push({ view, q, offset, limit });
    const scoped = all.filter((row) => view !== "attention" || row.is_attention).filter((row) => !q || row.creator_name.includes(q));
    return { updated_at: "2026-09-08T00:00:00Z", scheduler_enabled: true, timezone: "Asia/Shanghai", view, total: scoped.length, offset, limit, next_offset: offset + limit < scoped.length ? offset + limit : null, summary: { blocked_count: 24, overdue_count: 99, oldest_overdue_at: "2026-09-01T00:00:00Z" }, suppressed_count: 7, items: scoped.slice(offset, offset + limit) };
  }});
  await page.goto("/admin/scheduler?page=21");
  await page.waitForTimeout(500);
  await expect(page).toHaveURL(/page=21/);
  await page.locator("details").filter({ hasText: "Healthy schedules" }).locator("summary").click({ force: true });
  await expect(page.getByText("Creator 500", { exact: true })).toBeVisible();
  expect(reads.every((read) => read.limit <= 500)).toBe(true);
  await expect(page.getByText("99", { exact: true }).first()).toBeVisible();
  await expect(page.getByText("24", { exact: true }).first()).toBeVisible();
});

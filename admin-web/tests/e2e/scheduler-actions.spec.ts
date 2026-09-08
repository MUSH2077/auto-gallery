import { expect, test, type BrowserContext, type Route } from "@playwright/test";

test.describe.configure({ timeout: 60_000 });

type FixtureOptions = {
  post?: "accepted" | "conflict" | "network-once";
  taskStates?: Array<Record<string, unknown>>;
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
  await context.addInitScript(() => {
    localStorage.setItem("ag_token", "scheduler-fixture-token");
    localStorage.setItem("auto-gallery-lang", "en");
    localStorage.setItem("auto-gallery-theme", "dark");
  });
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
    if (path === "/api/v1/system/scheduler-decisions") return json(route, {
      updated_at: "2026-09-08T00:00:00Z", scheduler_enabled: true, timezone: "Asia/Shanghai", total: 0, items: [],
    });
    if (path === "/api/v1/admin/scheduler/sync-now" && request.method() === "POST") {
      postAttempts += 1;
      posts.push(JSON.parse(request.postData() || "{}"));
      if (options.post === "network-once" && postAttempts === 1) return route.abort("connectionreset");
      if (options.post === "conflict") return json(route, {
        detail: { code: "batch_active", message: "A subscription sync batch is already active", task_id: "task-existing", mode: "due_scan" },
      }, 409);
      return json(route, {
        task_id: "task-accepted", job_id: "rq-accepted", status: "enqueued",
        operation_type: "subscription-sync-batch", mode: posts.at(-1)?.mode,
      }, 202);
    }
    if (/^\/api\/v1\/tasks\/[^/]+$/.test(path)) {
      const state = taskStates[Math.min(taskReads, taskStates.length - 1)];
      taskReads += 1;
      return json(route, { ...state, id: path.split("/").at(-1) });
    }
    if (/^\/api\/v1\/admin\/scheduler\/batches\/[^/]+\/items$/.test(path)) return json(route, {
      total: 2,
      items: [
        { id: "item-1", source_id: "source-1", source: "pixiv", status: "succeeded", attempts: 1, next_retry_at: null, download_job_id: "download-1", reason_code: null, error: null, outcome: { import_status: "complete" } },
        { id: "item-2", source_id: "source-2", source: "x", status: "failed", attempts: 2, next_retry_at: null, download_job_id: "download-2", reason_code: "import_failed", error: "Main import failed", outcome: { import_status: "failed" } },
      ],
    });
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
  await expect(page.getByText("Downloads and main imports reached a terminal outcome")).toBeVisible();
  await expect(page.getByText("2 succeeded", { exact: true })).toBeVisible();
  await expect(page.getByText("1 skipped", { exact: true })).toBeVisible();
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

test("scheduler replays a lost response after refresh with the same intent", async ({ context, page }) => {
  const fixture = await installSchedulerFixtures(context, { post: "network-once" });
  await page.goto("/admin/scheduler");
  const button = page.getByRole("button", { name: "Run scheduler scan" });
  await button.click();
  await expect(page.getByText("Network error")).toBeVisible();
  await expect(button).toBeEnabled();
  await page.reload();
  await expect.poll(() => fixture.posts.length).toBe(2);
  expect(fixture.posts[1].request_id).toBe(fixture.posts[0].request_id);
});

test("scheduler opens the durable task named by a 409 conflict", async ({ context, page }) => {
  await installSchedulerFixtures(context, { post: "conflict" });
  await page.goto("/admin/scheduler");
  await page.getByRole("button", { name: "Run scheduler scan" }).click();
  await expect(page.getByRole("log").getByText("Opened the active batch")).toBeVisible();
  await expect(page.getByRole("heading", { name: "Current sync batch" })).toBeVisible();
  const taskLink = page.getByRole("link", { name: "View task" });
  await expect(taskLink).toHaveAttribute("href", /task=task-existing/);
  await taskLink.click();
  await expect(page).toHaveURL(/\/admin\/jobs\?tab=admin&task=task-existing/, { timeout: 15_000 });
  await expect(page.getByRole("complementary", { name: "Task detail" })).toBeVisible();
});

test("cancelled batch remains tracked while child cleanup is pending", async ({ context, page }) => {
  const fixture = await installSchedulerFixtures(context, { taskStates: [{
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
  }] });
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

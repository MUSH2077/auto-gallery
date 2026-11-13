import { expect, test, type BrowserContext, type Route } from "@playwright/test";

const json = (route: Route, body: unknown, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
const baseUser = { id: 97, username: "maintenance-review", is_admin: false, is_active: true, permissions: ["tasks", "system"], modules: { tasks: true, system: true }, preferences: { lang: "en", theme: "light" }, nsfw_visible: true, must_change_password: false };
const workbench = { queue: { default: 0, scheduled: 0, failed_download_count: 0, failed_import_count: 0, active_download_count: 0, active_import_count: 0, stale_count: 0 }, recent: { download_jobs: [], import_jobs: [], works: [], successful_syncs: [] }, attention: {} };

async function setup(context: BrowserContext, user = baseUser, handler?: (route: Route, path: string) => Promise<unknown> | undefined) {
  const origin = process.env.PLAYWRIGHT_BASE_URL || "http://127.0.0.1:13000";
  await context.addCookies([{ name: "ag_session", value: "maintenance-session", url: origin }, { name: "ag_csrf", value: "maintenance-csrf", url: origin }]);
  await context.addInitScript(() => { localStorage.setItem("auto-gallery-lang", "en"); localStorage.setItem("auto-gallery-theme", "light"); });
  await context.route("**/api/v1/**", async route => {
    const path = new URL(route.request().url()).pathname;
    const handled = handler?.(route, path);
    if (handled !== undefined) return handled;
    if (path === "/api/v1/auth/me") return json(route, user);
    if (path === "/api/v1/auth/ws-ticket") return json(route, { detail: "fixture websocket unavailable" }, 503);
    if (path === "/api/v1/system/workbench") return json(route, workbench);
    if (path === "/api/v1/notifications") return json(route, { total: 0, items: [] });
    if (path === "/api/v1/search/assist") return json(route, { query: "", canonical_query: "", parsed: { tokens: [] }, diagnostics: [], suggestions: [], catalog: [] });
    if (path === "/api/v1/tasks") return json(route, { total: 0, items: [], summary: { all: 0, attention: 0, running: 0, waiting: 0 } });
    if (path === "/api/v1/download-jobs") return json(route, []);
    if (path === "/api/v1/import-jobs") return json(route, { total: 0, items: [] });
    return json(route, {});
  });
}

test("default TaskRun exposes collapsed records links without changing the main task view", async ({ page, context }) => {
  await setup(context, baseUser, (route, path) => {
    if (path === "/api/v1/tasks") return json(route, { total: 1, items: [{ id: "task-default", title: "Default task", kind: "admin", status: "failed", created_at: "2026-10-08T00:00:00Z", available_actions: [] }], summary: { all: 1, attention: 1, running: 0, waiting: 0 } });
  });
  await page.goto("/admin/jobs");
  await expect(page.getByRole("link", { name: "Default task", exact: true })).toBeVisible();
  const tools = page.getByText("Records and maintenance", { exact: true });
  await expect(tools).toBeVisible(); await tools.click();
  await expect(page.getByRole("link", { name: "Download Queue", exact: true })).toHaveAttribute("href", /records=download/);
  await expect(page.getByRole("link", { name: "Imports", exact: true })).toHaveAttribute("href", /records=import/);
});

test("system-only users see kill-stuck and compaction tools without raw record access", async ({ page, context }) => {
  const calls: string[] = [];
  const systemOnly = { ...baseUser, id: 98, username: "system-only", permissions: ["system"], modules: { system: true, tasks: false } };
  await setup(context, systemOnly, (route, path) => {
    calls.push(path);
    if (path === "/api/v1/tasks") return json(route, { total: 1, items: [{ id: "system-task", title: "System task", kind: "admin", status: "failed", created_at: "2026-10-08T00:00:00Z", available_actions: [] }], summary: { all: 1, attention: 1, running: 0, waiting: 0 } });
  });
  await page.goto("/admin/jobs");
  await page.getByText("Records and maintenance", { exact: true }).click();
  await expect(page.getByRole("button", { name: "Kill Stuck", exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Preview compaction", exact: true })).toBeVisible();
  await expect(page.getByRole("link", { name: "Download Queue", exact: true })).toHaveCount(0);
  await expect(page.getByRole("link", { name: "Imports", exact: true })).toHaveCount(0);
  expect(calls).not.toContain("/api/v1/download-jobs");
  expect(calls).not.toContain("/api/v1/import-jobs");
});

test("tasks-only users cannot see system maintenance controls", async ({ page, context }) => {
  const tasksOnly = { ...baseUser, id: 99, username: "tasks-only", permissions: ["tasks"], modules: { tasks: true, system: false } };
  await setup(context, tasksOnly);
  await page.goto("/admin/jobs");
  await page.getByText("Records and maintenance", { exact: true }).click();
  await expect(page.getByRole("button", { name: "Kill Stuck", exact: true })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Preview compaction", exact: true })).toHaveCount(0);
});

test("free-text q alone cannot expose retry-all-matching", async ({ page, context }) => {
  await setup(context, baseUser);
  await page.goto("/admin/jobs?q=source%20free%20text");
  await page.getByText("Records and maintenance", { exact: true }).click();
  await expect(page.getByRole("button", { name: "Batch current filter", exact: true })).toHaveCount(0);
});

test("structured record filters show frozen conditions and retain HTTP 200 partial errors", async ({ page, context }) => {
  const calls: Array<{ path: string; body?: unknown }> = [];
  const record = { id: "filtered-job", source_url: "https://pixiv.example/filtered", status: "failed", source: "pixiv", created_at: "2026-10-08T00:00:00Z", updated_at: "2026-10-08T00:00:00Z", available_actions: [] };
  await setup(context, baseUser, (route, path) => {
    if (path === "/api/v1/download-jobs") return json(route, [record]);
    if (path === "/api/v1/download-jobs/batch-by-filter") {
      calls.push({ path, body: route.request().postDataJSON() });
      return json(route, { total_matched: 3, succeeded: 2, failed: 1, errors: [{ id: "failed-job", error: { reason: "execution_unsettled" } }] });
    }
  });
  await page.goto("/admin/jobs?records=download&status=failed&source=pixiv");
  await page.getByText("Records and maintenance", { exact: true }).click();
  await page.getByRole("button", { name: "Batch current filter", exact: true }).click();
  const dialog = page.getByRole("dialog").last();
  await expect(dialog).toContainText("every record matching");
  await expect(dialog).toContainText("Failed");
  await expect(dialog).toContainText("pixiv");
  await dialog.getByRole("button", { name: /^Confirm$/i }).click();
  await expect.poll(() => calls.length).toBe(1);
  expect(calls[0].body).toEqual({ filters: { status: "failed", source: "pixiv" }, action: "retry", note: undefined });
  await expect(dialog).toContainText("execution_unsettled");
  await expect(dialog).toContainText("3 matched");
});

test("task groups are labelled as belonging to the current page", async ({ page, context }) => {
  await setup(context, baseUser, (route, path) => {
    if (path === "/api/v1/tasks") return json(route, {
      total: 2,
      items: [
        { id: "parent-task", title: "Parent task", kind: "admin", status: "running", created_at: "2026-10-08T00:00:00Z", available_actions: [], parent_task_id: null },
        { id: "child-task", title: "Child task", kind: "admin", status: "failed", created_at: "2026-10-08T00:00:00Z", available_actions: [], parent_task_id: "parent-task" },
      ],
      summary: { all: 2, attention: 1, running: 1, waiting: 0 },
    });
  });
  await page.goto("/admin/jobs");
  await expect(page.getByText("Related tasks on this page", { exact: true })).toBeVisible();
  await expect(page.getByText(/Parent task ·/)).toBeVisible();
  await page.getByText(/Parent task ·/).click();
  await expect(page.getByLabel("Related tasks on this page").getByRole("link", { name: "Child task", exact: true })).toBeVisible();
});


test("switching from TaskRun to a records route clears task selection", async ({ page, context }) => {
  await setup(context, baseUser, (route, path) => {
    if (path === "/api/v1/tasks") return json(route, { total: 1, items: [{ id: "selected-task", title: "Selected task", kind: "admin", status: "failed", created_at: "2026-10-08T00:00:00Z", available_actions: [] }], summary: { all: 1, attention: 1, running: 0, waiting: 0 } });
    if (path === "/api/v1/download-jobs") return json(route, [{ id: "record-after-switch", source_url: "https://pixiv.example/after-switch", status: "failed", created_at: "2026-10-08T00:00:00Z", updated_at: "2026-10-08T00:00:00Z", available_actions: [] }]);
  });
  await page.goto("/admin/jobs");
  await page.getByRole("button", { name: /Select tasks/i }).click();
  await page.getByRole("checkbox", { name: /selected-task/i }).check();
  await expect(page.locator(".layout-selection-bar")).toContainText("1");
  await page.getByText("Records and maintenance", { exact: true }).click();
  await page.getByRole("link", { name: "Download Queue", exact: true }).click();
  await expect(page).toHaveURL(/records=download/);
  await expect(page.locator(".layout-selection-bar")).toHaveCount(0);
  await expect(page.getByRole("link", { name: "https://pixiv.example/after-switch", exact: true })).toBeVisible();
});

test("maintenance action changes clear stale retry errors", async ({ page, context }) => {
  await setup(context, baseUser, (route, path) => {
    if (path === "/api/v1/download-jobs") return json(route, [{ id: "failed-job", source_url: "https://pixiv.example/failed", status: "failed", created_at: "2026-10-08T00:00:00Z", updated_at: "2026-10-08T00:00:00Z", available_actions: [] }]);
    if (path === "/api/v1/download-jobs/retry-all") return json(route, { detail: "retry service unavailable" }, 503);
    if (path === "/api/v1/download-jobs/clear") return json(route, { total_matched: 0, succeeded: 0, failed: 0, errors: [] });
  });
  await page.goto("/admin/jobs?records=download");
  await page.getByText("Records and maintenance", { exact: true }).click();
  await page.getByRole("button", { name: "Retry All Failed", exact: true }).click();
  await page.getByRole("dialog").getByRole("button", { name: /^Confirm$/i }).click();
  await expect(page.getByRole("dialog").getByRole("alert")).toContainText("retry service unavailable");
  await page.getByRole("dialog").getByRole("button", { name: "Cancel", exact: true }).click();
  await page.getByRole("button", { name: "Clear Complete", exact: true }).click();
  await expect(page.getByRole("dialog")).not.toContainText("retry service unavailable");
});

test("a pending second compaction preview disables confirmation and cannot reuse the old token", async ({ page, context }) => {
  let previewCalls = 0;
  let releaseSecond!: () => void;
  const secondHeld = new Promise<void>(resolve => { releaseSecond = resolve; });
  const compactBodies: Array<{ dry_run?: boolean; preview_token?: string }> = [];
  await setup(context, baseUser, (route, path) => {
    if (path !== "/api/v1/tasks/compact") return undefined;
    const body = route.request().postDataJSON() as { dry_run?: boolean; preview_token?: string };
    compactBodies.push(body);
    previewCalls += 1;
    if (previewCalls === 1) return json(route, { dry_run: true, matched: 3, preview_token: "token-old" }).then(() => true);
    return secondHeld.then(() => json(route, { dry_run: true, matched: 4, preview_token: "token-new" }).then(() => true));
  });
  await page.goto("/admin/jobs");
  await page.getByText("Records and maintenance", { exact: true }).click();
  await page.getByRole("button", { name: "Preview compaction", exact: true }).click();
  await expect(page.getByRole("dialog")).toContainText("3");
  await page.getByRole("dialog").getByRole("button", { name: "Cancel", exact: true }).click();
  await page.getByRole("button", { name: "Preview compaction", exact: true }).click();
  await expect.poll(() => previewCalls).toBe(2);
  await expect(page.getByRole("dialog").getByRole("button", { name: /^Confirm$/i })).toBeDisabled();
  releaseSecond();
  await expect(page.getByRole("dialog")).toContainText("4");
  expect(compactBodies[0]).toEqual({ dry_run: true, limit: 1000 });
  expect(compactBodies[1]).toEqual({ dry_run: true, limit: 1000 });
});

import { expect, test, type BrowserContext, type Route } from "@playwright/test";

test.describe.configure({ timeout: 60_000 });

const me = {
  id: 1,
  username: "curator",
  display_name: "Curator",
  is_admin: true,
  is_active: true,
  permissions: ["curation"],
  modules: {},
  preferences: {},
  nsfw_visible: true,
  upload_used_bytes: 0,
  must_change_password: false,
};

const workbench = {
  updated_at: "2026-09-20T00:00:00Z",
  queue: { default: 0, scheduled: 0, failed: 0, active_download_count: 0, active_import_count: 0, failed_download_count: 0, failed_import_count: 0, stale_download_count: 0, stale_import_count: 0, stale_count: 0 },
  scheduler: { enabled: true, mode: "interval", timezone: "UTC", scan_interval_minutes: 60 },
  storage: { disk_total_bytes: 1, disk_free_bytes: 1, disk_used_bytes: 0, risk_level: "ok" },
  health: {},
  attention: { auth_unhealthy_count: 0, failed_download_count: 0, failed_import_count: 0, stale_job_count: 0, low_disk_warning: false, scheduler_disabled_warning: false },
  recent: { download_jobs: [], import_jobs: [], works: [], successful_syncs: [] },
};

const commit = {
  id: "11111111-1111-4111-8111-111111111111",
  parent_commit_id: null,
  actor_type: "user",
  actor_id: "1",
  message: "Trash six review works",
  trigger: "work_trash",
  occurred_at: "2026-09-20T00:00:00Z",
  reverts_commit_id: null,
  status: "active",
  stats: {},
  metadata: {},
  is_baseline: false,
  revertible: true,
  created_at: "2026-09-20T00:00:00Z",
  updated_at: "2026-09-20T00:00:00Z",
  changes: Array.from({ length: 6 }, (_, index) => ({
    id: `change-${index}`,
    commit_id: "11111111-1111-4111-8111-111111111111",
    subject_type: "work",
    subject_id: `work-${index}`,
    action: "trash",
    before_state: { visibility: "visible" },
    after_state: { visibility: "trashed" },
    diff: { visibility: { before: "visible", after: "trashed" } },
    created_at: "2026-09-20T00:00:00Z",
  })),
};

function json(route: Route, body: unknown, status = 200) {
  return route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
}

async function auth(context: BrowserContext, principal = me) {
  const baseUrl = process.env.PLAYWRIGHT_BASE_URL || "http://127.0.0.1:13000";
  await context.addCookies([{ name: "ag_token", value: "fixture", url: baseUrl }]);
  await context.addInitScript(() => {
    localStorage.setItem("ag_token", "fixture");
    localStorage.setItem("auto-gallery-lang", "en");
  });
  await context.route("https://fonts.loli.net/**", (route) => route.fulfill({ status: 200, body: "" }));
}

test("curation members can use ledger actions while purge remains admin-only", async ({ context, page }) => {
  await auth(context, { ...me, is_admin: false });
  let purgePreviewReads = 0;
  await context.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (url.pathname === "/api/v1/auth/me") return json(route, { ...me, is_admin: false });
    if (url.pathname === "/api/v1/system/workbench") return json(route, workbench);
    if (url.pathname === "/api/v1/curation/commits") return json(route, { items: [commit], total: 1 });
    if (url.pathname === "/api/v1/curation/purge/preview") {
      purgePreviewReads += 1;
      return json(route, { work_count: 1, asset_count: 0, bytes_reclaimable: 0, works: [{ id: "purge-1", title: "One" }], assets: [] });
    }
    if (url.pathname === "/api/v1/curation/rule-suggestions") return json(route, []);
    if (url.pathname === "/api/v1/curation/backfill/status") return json(route, { is_complete: false, expected: { creators: 2, repositories: 0, work_groups: 0 }, existing: { creators: 1, repositories: 0, work_groups: 0 }, missing: { creators: 1, repositories: 0, work_groups: 0 } });
    if (url.pathname === "/api/v1/curation/backfill/latest") return json(route, { snapshot: null, current: null });
    if (url.pathname === "/api/v1/search") return json(route, { groups: { works: { total: 1, items: [] } } });
    return json(route, { items: [], total: 0 });
  });

  await page.goto("/admin/data-mgmt/curation");
  await expect(page.getByText("Trash six review works")).toBeVisible({ timeout: 15_000 });
  await expect(page.getByRole("article").getByRole("button", { name: "Revert", exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Run baseline backfill", exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: /Purge current batch/ })).toHaveCount(0);
  expect(purgePreviewReads).toBe(0);
});

test("filters and expansion are local while revert cancel is inert and partial completion is explicit", async ({ context, page }) => {
  await auth(context);
  let revertPosts = 0;
  await context.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (url.pathname === "/api/v1/auth/me") return json(route, me);
    if (url.pathname === "/api/v1/system/workbench") return json(route, workbench);
    if (url.pathname === "/api/v1/curation/commits" && request.method() === "GET") return json(route, { items: [commit], total: 1 });
    if (url.pathname === `/api/v1/curation/commits/${commit.id}/revert`) {
      revertPosts += 1;
      return json(route, { status: "partial", commit: null, reverted: 5, skipped: 1, conflicts: [{ subject_id: "work-5", reason: "changed" }] });
    }
    if (url.pathname === "/api/v1/curation/purge/preview") return json(route, { work_count: 0, asset_count: 0, bytes_reclaimable: 0, works: [], assets: [] });
    if (url.pathname === "/api/v1/curation/rule-suggestions") return json(route, []);
    if (url.pathname === "/api/v1/curation/backfill/status") return json(route, { is_complete: true, expected: { creators: 1, repositories: 0, work_groups: 0 }, existing: { creators: 1, repositories: 0, work_groups: 0 }, missing: { creators: 0, repositories: 0, work_groups: 0 } });
    if (url.pathname === "/api/v1/curation/backfill/latest") return json(route, { snapshot: null, current: null });
    if (url.pathname === "/api/v1/search") return json(route, { groups: { works: { total: 0, items: [] } } });
    return json(route, { items: [], total: 0 });
  });

  await page.goto("/admin/data-mgmt/curation");
  await expect(page.getByText("Trash six review works")).toBeVisible({ timeout: 15_000 });
  await expect(page.getByText("+1 more changes")).toBeVisible();
  await page.getByRole("button", { name: "Details" }).click();
  await expect(page.getByText("+1 more changes")).toHaveCount(0);
  await expect(page.getByText(/"before": "visible"/).first()).toBeVisible();

  await page.getByRole("button", { name: "Trash", exact: true }).click();
  await expect(page).toHaveURL(/trigger=work_trash/);
  await page.getByRole("button", { name: "Hide baseline" }).click();
  await expect(page).toHaveURL(/include_baseline=false/);

  const revertButton = page.getByRole("article").getByRole("button", { name: "Revert", exact: true });
  await revertButton.click();
  await expect(page.getByRole("dialog")).toContainText("Trash six review works");
  await page.getByRole("dialog").getByRole("button", { name: "Cancel" }).click();
  expect(revertPosts).toBe(0);
  await revertButton.click();
  await page.getByRole("dialog").getByRole("button", { name: "Confirm" }).click();
  await expect(page.getByRole("status")).toContainText("5");
  await expect(page.getByRole("status")).toContainText("1");
  expect(revertPosts).toBe(1);
});

test("backfill tracks accepted work to completion and purge applies only the frozen preview batch", async ({ context, page }) => {
  await auth(context);
  const purgeBodies: unknown[] = [];
  let taskReads = 0;
  let backfillPosts = 0;
  await context.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (url.pathname === "/api/v1/auth/me") return json(route, me);
    if (url.pathname === "/api/v1/system/workbench") return json(route, workbench);
    if (url.pathname === "/api/v1/curation/commits") return json(route, { items: [commit], total: 1 });
    if (url.pathname === "/api/v1/curation/purge/preview") return json(route, {
      work_count: 2,
      asset_count: 2,
      bytes_reclaimable: 3072,
      works: [{ id: "purge-1", title: "One" }, { id: "purge-2", title: "Two" }],
      assets: [{ id: "asset-1", file_name: "one.jpg", file_size: 1024 }, { id: "asset-2", file_name: "two.jpg", file_size: 2048 }],
    });
    if (url.pathname === "/api/v1/curation/purge") {
      purgeBodies.push(request.postDataJSON());
      return json(route, { ...commit, id: "purge-commit", trigger: "work_purge", message: "Purge current preview batch" });
    }
    if (url.pathname === "/api/v1/curation/rule-suggestions") return json(route, []);
    if (url.pathname === "/api/v1/curation/backfill/status") return json(route, { is_complete: false, expected: { creators: 2, repositories: 0, work_groups: 0 }, existing: { creators: 1, repositories: 0, work_groups: 0 }, missing: { creators: 1, repositories: 0, work_groups: 0 } });
    if (url.pathname === "/api/v1/curation/backfill/latest") return json(route, { snapshot: null, current: null });
    if (url.pathname === "/api/v1/curation/backfill" && request.method() === "POST") {
      backfillPosts += 1;
      return json(route, { task_id: "task-backfill", job_id: "job-backfill", status: "enqueued", operation_type: "admin-curation-backfill" }, 202);
    }
    if (url.pathname === "/api/v1/admin/operations/task-backfill") {
      taskReads += 1;
      return json(route, taskReads < 2
        ? { task_id: "task-backfill", job_id: "job-backfill", status: "running", operation_type: "admin-curation-backfill", progress: { phase: "creators", label: "Backfilling creators", current: 1, total: 2 } }
        : { task_id: "task-backfill", job_id: "job-backfill", status: "complete", operation_type: "admin-curation-backfill", result: { status: "complete", created: { creators: 1 } } });
    }
    if (url.pathname === "/api/v1/search") return json(route, { groups: { works: { total: 2, items: [] } } });
    return json(route, { items: [], total: 0 });
  });

  await page.goto("/admin/data-mgmt/curation");
  await expect(page.getByRole("button", { name: "Run baseline backfill" })).toBeEnabled({ timeout: 15_000 });
  await page.getByRole("button", { name: "Run baseline backfill" }).click();
  await expect(page.getByText("Operation in progress")).toBeVisible();
  await expect(page.getByText("Operation complete")).toBeVisible({ timeout: 10_000 });
  await expect(page.getByRole("link", { name: "Task detail" })).toHaveAttribute("href", "/admin/jobs?tab=admin&task=task-backfill");
  expect(backfillPosts).toBe(1);

  const purgeButton = page.getByRole("button", { name: /Purge current batch \(2\)/ });
  await purgeButton.click();
  await expect(page.getByRole("dialog")).toContainText("2 works");
  await page.getByRole("dialog").getByRole("button", { name: "Cancel" }).click();
  expect(purgeBodies).toEqual([]);
  await purgeButton.click();
  await page.getByRole("dialog").getByRole("button", { name: "Confirm" }).click();
  await expect(page.getByRole("status")).toContainText("2 works");
  expect(purgeBodies).toEqual([{ work_ids: ["purge-1", "purge-2"], message: "Purge current preview batch" }]);
});

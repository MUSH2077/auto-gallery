import { expect, test, type Route } from "@playwright/test";

test.describe.configure({ timeout: 90_000 });
const json = (route: Route, body: unknown, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
const asset = (id: string) => ({ id, file_name: `${id}.png`, file_size: 4, mime_type: "image/png", width: 1, height: 1, source: "pixiv", source_work_id: id, work_title: id, creator_name: "Fixture", preview_url: `/media/preview/${id}`, thumb_url: `/media/thumb/${id}`, is_representative: false });
const dedupCase = { id: "case-1", status: "pending", revision: 4, left: asset("asset-left"), right: asset("asset-right"), suggested_representative_asset_id: "asset-left", created_at: "2026-09-12T00:00:00Z", evidence: { id: "evidence-1", algorithm_version: "v1", sha256_equal: false, phash_distance: 1, ssim_score: 0.99, aspect_ratio_delta: 0, visual_score: 90, metadata_score: 5, total_score: 95, hard_gate_passed: true, facts: { metadata: {}, scope: {}, thresholds: {} } } };
const admin = { id: 7, username: "task5", display_name: "Task 5", is_admin: true, is_active: true, permissions: ["curation"], modules: { curation: true }, preferences: {}, nsfw_visible: true, upload_used_bytes: 0, must_change_password: false };
const workbench = { updated_at: "2026-09-12T00:00:00Z", queue: { default: 0, scheduled: 0, failed: 0, active_download_count: 0, active_import_count: 0, failed_download_count: 0, failed_import_count: 0, stale_download_count: 0, stale_import_count: 0, stale_count: 0 }, scheduler: { enabled: true, mode: "interval", timezone: "UTC", scan_interval_minutes: 60 }, storage: { disk_total_bytes: 1, disk_free_bytes: 1, disk_used_bytes: 0, risk_level: "ok" }, health: {}, attention: { auth_unhealthy_count: 0, failed_download_count: 0, failed_import_count: 0, stale_job_count: 0, low_disk_warning: false, scheduler_disabled_warning: false }, recent: { download_jobs: [], import_jobs: [], tasks: [], works: [], successful_syncs: [] } };

test("dedup caller submits its HTTP-origin UUID and exact selected representative", async ({ context, page }) => {
  const unhandled: string[] = [];
  let decision: any = null;
  const baseUrl = process.env.PLAYWRIGHT_BASE_URL || "http://127.0.0.1:13000";
  await context.addCookies([{ name: "ag_token", value: "fixture", url: baseUrl }]);
  await context.addInitScript(() => { localStorage.setItem("ag_token", "fixture"); localStorage.setItem("auto-gallery-lang", "en"); });
  await context.route("**/media/**", (route) => route.fulfill({ status: 200, contentType: "image/png", body: "pixel" }));
  await context.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path === "/api/v1/auth/me") return json(route, admin);
    if (path === "/api/v1/auth/ws-ticket") return json(route, { detail: "fixture websocket unavailable" }, 503);
    if (path === "/api/v1/system/workbench") return json(route, workbench);
    if (path === "/api/v1/admin/dedup/cases" && request.method() === "GET") return json(route, { items: decision ? [] : [dedupCase], total: decision ? 0 : 1, offset: 0, limit: 20 });
    if (path === "/api/v1/admin/dedup/scans/latest") return json(route, { snapshot: null, current: null });
    if (path === "/api/v1/admin/dedup/cases/case-1/decisions" && request.method() === "POST") { decision = request.postDataJSON(); return json(route, { decision_id: "decision-1", case_id: "case-1", action: "merge", status: "accepted", revision: 5, representative_asset_id: "asset-right" }); }
    if (path === "/api/v1/tasks") return json(route, { total: 0, items: [] });
    if (path === "/api/v1/system/scheduler-decisions") return json(route, { total: 0, items: [], summary: { blocked_count: 0 } });
    if (path === "/api/v1/operations/overview") return json(route, { summary: { attention: 0 }, items: [] });
    unhandled.push(`${request.method()} ${path}`);
    return json(route, { detail: `Unhandled fixture request: ${path}` }, 501);
  });

  await page.goto("/admin/data-mgmt/dedup");
  await page.getByRole("button", { name: /Keep this image/ }).nth(1).click();
  await expect(page.getByRole("dialog")).toContainText("Group as one visual asset");
  await page.getByRole("button", { name: "Confirm" }).click();
  await expect.poll(() => decision).not.toBeNull();
  expect(decision).toMatchObject({ expected_revision: 4, action: "merge", representative_asset_id: "asset-right" });
  expect(decision.idempotency_key).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
  expect(unhandled).toEqual([]);
});

test("separate requires confirmation while defer is immediate and both report completion", async ({ context, page }) => {
  const baseUrl = process.env.PLAYWRIGHT_BASE_URL || "http://127.0.0.1:13000";
  await context.addCookies([{ name: "ag_token", value: "fixture", url: baseUrl }]);
  await context.addInitScript(() => { localStorage.setItem("ag_token", "fixture"); localStorage.setItem("auto-gallery-lang", "en"); });
  await context.route("**/media/**", (route) => route.fulfill({ status: 200, contentType: "image/png", body: "pixel" }));
  const decisions: { caseId: string; body: any }[] = [];
  const cases = [
    { ...dedupCase, id: "case-separate", left: asset("case-separate-left"), right: asset("case-separate-right") },
    { ...dedupCase, id: "case-defer", left: asset("case-defer-left"), right: asset("case-defer-right") },
  ];
  await context.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path === "/api/v1/auth/me") return json(route, admin);
    if (path === "/api/v1/auth/ws-ticket") return json(route, { detail: "fixture websocket unavailable" }, 503);
    if (path === "/api/v1/system/workbench") return json(route, workbench);
    if (path === "/api/v1/admin/dedup/scans/latest") return json(route, { snapshot: null, current: null });
    if (path === "/api/v1/admin/dedup/cases" && request.method() === "GET") {
      const decided = new Set(decisions.map((item) => item.caseId));
      const items = cases.filter((item) => !decided.has(item.id));
      return json(route, { items, total: items.length, offset: 0, limit: 25 });
    }
    const match = path.match(/^\/api\/v1\/admin\/dedup\/cases\/(case-(?:separate|defer))\/decisions$/);
    if (match && request.method() === "POST") {
      const body = request.postDataJSON();
      decisions.push({ caseId: match[1], body });
      return json(route, { decision_id: `${match[1]}-decision`, case_id: match[1], action: body.action, status: "accepted", revision: 5, representative_asset_id: null });
    }
    if (path === "/api/v1/tasks") return json(route, { total: 0, items: [] });
    if (path === "/api/v1/system/scheduler-decisions") return json(route, { total: 0, items: [], summary: { blocked_count: 0 } });
    if (path === "/api/v1/operations/overview") return json(route, { summary: { attention: 0 }, items: [] });
    return json(route, { detail: `Unhandled fixture request: ${path}` }, 501);
  });

  await page.goto("/admin/data-mgmt/dedup");
  const separateCard = page.getByRole("article").filter({ hasText: "case-separate-left" });
  await separateCard.getByRole("button", { name: "Not the same image", exact: true }).click();
  await expect(page.getByRole("dialog")).toContainText("Keep these images separate");
  await page.getByRole("dialog").getByRole("button", { name: "Cancel" }).click();
  expect(decisions).toEqual([]);
  await separateCard.getByRole("button", { name: "Not the same image", exact: true }).click();
  await page.getByRole("dialog").getByRole("button", { name: "Confirm" }).click();
  await expect(page.getByRole("status")).toContainText("Kept the images separate");

  const deferCard = page.getByRole("article").filter({ hasText: "case-defer-left" });
  await deferCard.getByRole("button", { name: "Review later", exact: true }).click();
  await expect(page.getByRole("status")).toContainText("Deferred the image pair");
  expect(decisions.map((item) => item.body.action)).toEqual(["separate", "defer"]);
  for (const item of decisions) {
    expect(item.body.idempotency_key).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
    expect(item.body.representative_asset_id).toBeUndefined();
  }
});

test("asset scan follows its durable task through reload and refreshes cases on completion", async ({ context, page }) => {
  const baseUrl = process.env.PLAYWRIGHT_BASE_URL || "http://127.0.0.1:13000";
  await context.addCookies([{ name: "ag_token", value: "fixture", url: baseUrl }]);
  await context.addInitScript(() => { localStorage.setItem("ag_token", "fixture"); localStorage.setItem("auto-gallery-lang", "en"); });
  let scanPosts = 0;
  let taskReads = 0;
  let caseReads = 0;
  await context.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path === "/api/v1/auth/me") return json(route, admin);
    if (path === "/api/v1/auth/ws-ticket") return json(route, { detail: "fixture websocket unavailable" }, 503);
    if (path === "/api/v1/system/workbench") return json(route, workbench);
    if (path === "/api/v1/admin/dedup/cases") { caseReads += 1; return json(route, { items: [], total: 0, offset: 0, limit: 25 }); }
    if (path === "/api/v1/admin/dedup/scans/latest") return json(route, taskReads < 2
      ? { snapshot: null, current: null }
      : { snapshot: { task_id: "task-scan-1", job_id: "job-scan-1", status: "complete", operation_type: "asset-dedup-scan", progress: { phase: "complete", label: "Asset scan complete" }, result: { scan_id: "scan-1", status: "complete", assets_scanned: 2, candidates_evaluated: 1, cases_created: 1, assets_grouped: 0, bytes_reclaimable: 4 }, completed_at: "2026-09-20T00:00:00Z" }, current: null });
    if (path === "/api/v1/admin/dedup/scans" && request.method() === "POST") {
      scanPosts += 1;
      return json(route, { scan_id: "scan-1", task_id: "task-scan-1", job_id: "job-scan-1", status: "enqueued", operation_type: "asset-dedup-scan" }, 202);
    }
    if (path === "/api/v1/admin/operations/task-scan-1") {
      taskReads += 1;
      return json(route, taskReads < 2
        ? { task_id: "task-scan-1", job_id: "job-scan-1", status: "running", operation_type: "asset-dedup-scan", progress: { phase: "scanning", label: "Scanned 2 assets", current: 2 } }
        : { task_id: "task-scan-1", job_id: "job-scan-1", status: "complete", operation_type: "asset-dedup-scan", progress: { phase: "complete", label: "Asset scan complete" }, result: { scan_id: "scan-1", status: "complete", assets_scanned: 2, candidates_evaluated: 1, cases_created: 1, assets_grouped: 0, bytes_reclaimable: 4 } });
    }
    if (path === "/api/v1/tasks") return json(route, { total: 0, items: [] });
    if (path === "/api/v1/system/scheduler-decisions") return json(route, { total: 0, items: [], summary: { blocked_count: 0 } });
    if (path === "/api/v1/operations/overview") return json(route, { summary: { attention: 0 }, items: [] });
    return json(route, { detail: `Unhandled fixture request: ${path}` }, 501);
  });

  await page.goto("/admin/data-mgmt/dedup");
  await page.getByRole("button", { name: "Scan image assets", exact: true }).click();
  await expect(page.getByText("Operation complete", { exact: true })).toBeVisible({ timeout: 10_000 });
  await expect(page.getByRole("link", { name: "Task detail", exact: true })).toHaveAttribute("href", "/admin/jobs?tab=admin&task=task-scan-1");
  await page.reload();
  await expect(page.getByText("Operation complete", { exact: true })).toBeVisible({ timeout: 10_000 });
  expect(scanPosts).toBe(1);
  expect(caseReads).toBeGreaterThan(1);
});

test("failed asset scan retries the same durable task instead of creating another scan", async ({ context, page }) => {
  const baseUrl = process.env.PLAYWRIGHT_BASE_URL || "http://127.0.0.1:13000";
  await context.addCookies([{ name: "ag_token", value: "fixture", url: baseUrl }]);
  await context.addInitScript(() => { localStorage.setItem("ag_token", "fixture"); localStorage.setItem("auto-gallery-lang", "en"); });
  let scanPosts = 0;
  let retryPosts = 0;
  await context.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path === "/api/v1/auth/me") return json(route, admin);
    if (path === "/api/v1/auth/ws-ticket") return json(route, { detail: "fixture websocket unavailable" }, 503);
    if (path === "/api/v1/system/workbench") return json(route, workbench);
    if (path === "/api/v1/admin/dedup/cases") return json(route, { items: [], total: 0, offset: 0, limit: 25 });
    if (path === "/api/v1/admin/dedup/scans/latest") return json(route, scanPosts > 0 && retryPosts === 0
      ? { snapshot: null, current: { task_id: "task-scan-failed", job_id: "job-scan-failed-1", status: "failed", operation_type: "asset-dedup-scan", progress: { phase: "failed", label: "Asset scan failed" } } }
      : { snapshot: null, current: null });
    if (path === "/api/v1/admin/dedup/scans" && request.method() === "POST") {
      scanPosts += 1;
      return json(route, { scan_id: "scan-failed", task_id: "task-scan-failed", job_id: "job-scan-failed-1", status: "enqueued", operation_type: "asset-dedup-scan" }, 202);
    }
    if (path === "/api/v1/admin/operations/task-scan-failed/retry" && request.method() === "POST") {
      retryPosts += 1;
      return json(route, { task_id: "task-scan-failed", job_id: "job-scan-failed-2", status: "enqueued", operation_type: "asset-dedup-scan" }, 202);
    }
    if (path === "/api/v1/admin/operations/task-scan-failed") {
      return json(route, retryPosts === 0
        ? { task_id: "task-scan-failed", job_id: "job-scan-failed-1", status: "failed", operation_type: "asset-dedup-scan", error: "fixture scan failed", reason_code: "scan_failed" }
        : { task_id: "task-scan-failed", job_id: "job-scan-failed-2", status: "complete", operation_type: "asset-dedup-scan", progress: { phase: "complete", label: "Asset scan complete" }, result: { scan_id: "scan-failed", status: "complete", assets_scanned: 2, candidates_evaluated: 1, cases_created: 0, assets_grouped: 0, bytes_reclaimable: 0 } });
    }
    if (path === "/api/v1/tasks") return json(route, { total: 0, items: [] });
    if (path === "/api/v1/system/scheduler-decisions") return json(route, { total: 0, items: [], summary: { blocked_count: 0 } });
    if (path === "/api/v1/operations/overview") return json(route, { summary: { attention: 0 }, items: [] });
    return json(route, { detail: `Unhandled fixture request: ${path}` }, 501);
  });

  await page.goto("/admin/data-mgmt/dedup");
  await page.getByRole("button", { name: "Scan image assets", exact: true }).click();
  const failure = page.getByRole("alert").filter({ hasText: "fixture scan failed" });
  await expect(failure).toContainText("fixture scan failed");
  await page.reload();
  await expect(failure).toContainText("fixture scan failed");
  await failure.getByRole("button", { name: "Retry", exact: true }).click();
  await expect(page.getByText("Operation complete", { exact: true })).toBeVisible({ timeout: 10_000 });
  expect(scanPosts).toBe(1);
  expect(retryPosts).toBe(1);
});

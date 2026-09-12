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
  await context.addCookies([{ name: "ag_token", value: "fixture", domain: "127.0.0.1", path: "/" }]);
  await context.addInitScript(() => { localStorage.setItem("ag_token", "fixture"); localStorage.setItem("auto-gallery-lang", "en"); });
  await context.route("**/media/**", (route) => route.fulfill({ status: 200, contentType: "image/png", body: "pixel" }));
  await context.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path === "/api/v1/auth/me") return json(route, admin);
    if (path === "/api/v1/auth/ws-ticket") return json(route, { detail: "fixture websocket unavailable" }, 503);
    if (path === "/api/v1/system/workbench") return json(route, workbench);
    if (path === "/api/v1/admin/dedup/cases" && request.method() === "GET") return json(route, { items: decision ? [] : [dedupCase], total: decision ? 0 : 1, offset: 0, limit: 20 });
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

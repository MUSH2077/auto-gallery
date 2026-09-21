import { expect, test, type BrowserContext, type Route } from "@playwright/test";
test.describe.configure({ timeout: 60_000 });

const me = { id: 1, username: "task5", display_name: "Task 5", is_admin: true, is_active: true, permissions: [], modules: {}, preferences: {}, nsfw_visible: true, upload_used_bytes: 0, must_change_password: false };
const creator = { id: "creator-1", name: "Creator One", display_name: "Creator One", created_at: "2026-09-08T00:00:00Z", updated_at: "2026-09-08T00:00:00Z" };
const workbench = { updated_at: "2026-09-08T00:00:00Z", queue: { default: 0, scheduled: 0, failed: 0, active_download_count: 0, active_import_count: 0, failed_download_count: 0, failed_import_count: 0, stale_download_count: 0, stale_import_count: 0, stale_count: 0 }, scheduler: { enabled: true, mode: "interval", timezone: "UTC", scan_interval_minutes: 60 }, storage: { disk_total_bytes: 1, disk_free_bytes: 1, disk_used_bytes: 0, risk_level: "ok" }, health: {}, attention: { auth_unhealthy_count: 0, failed_download_count: 0, failed_import_count: 0, stale_job_count: 0, low_disk_warning: false, scheduler_disabled_warning: false }, recent: { download_jobs: [], import_jobs: [], works: [], successful_syncs: [] } };

function json(route: Route, body: unknown, status = 200) {
  return route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
}

async function auth(context: BrowserContext) {
  await context.addCookies([{ name: "ag_token", value: "fixture", domain: "127.0.0.1", path: "/" }]);
  await context.addInitScript(() => { localStorage.setItem("ag_token", "fixture"); localStorage.setItem("auto-gallery-lang", "en"); });
  await context.route("https://fonts.loli.net/**", (route) => route.fulfill({ status: 200, body: "" }));
}

test("creator merge keeps one group, excludes its target, and retains partial failures", async ({ context, page }) => {
  await auth(context);
  const bodies: unknown[] = [];
  let duplicateReads = 0;
  await context.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path === "/api/v1/auth/me") return json(route, me);
    if (path === "/api/v1/system/workbench") return json(route, workbench);
    if (path === "/api/v1/creators/duplicates") {
      duplicateReads += 1;
      return json(route, { total: 2, duplicates: [
        { reason: "same_identity", description: "Group one", creator_ids: ["a-target", "a-source"], creator_names: ["Alpha Target", "Alpha Source"] },
        duplicateReads === 1
          ? { reason: "same_identity", description: "Group two", creator_ids: ["b-target", "b-source", "g-source"], creator_names: ["Beta Target", "Beta Source", "Gamma Source"] }
          : { reason: "same_identity", description: "Group two", creator_ids: ["g-source", "b-target"], creator_names: ["Gamma Source", "Beta Target"] },
      ] });
    }
    if (path === "/api/v1/creators/merge") {
      bodies.push(request.postDataJSON());
      return json(route, { status: "ok", results: [
        { source_id: "b-source", status: "merged", error: null },
        { source_id: "g-source", status: "error", error: "merge_rejected" },
      ] });
    }
    return json(route, { items: [], total: 0 });
  });
  await page.goto("/admin/creators/duplicates");
  await expect(page.getByRole("checkbox", { name: /Alpha Source/ })).toBeVisible({ timeout: 15_000 });
  await page.waitForTimeout(500);
  await page.getByRole("checkbox", { name: /Alpha Source/ }).click();
  await page.getByRole("checkbox", { name: /Beta Source/ }).click();
  await page.getByRole("checkbox", { name: /Gamma Source/ }).click();
  await expect(page.getByRole("button", { name: "Beta Target" })).toBeVisible();
  await expect(page.getByText(/2 sources? selected/)).toBeVisible();
  await page.getByRole("button", { name: /Merge 2/ }).click();
  await expect(page.getByRole("dialog")).toContainText("Beta Target");
  await expect(page.getByRole("dialog")).toContainText("Beta Source");
  await expect(page.getByRole("dialog")).toContainText("Gamma Source");
  await page.getByRole("button", { name: "Confirm" }).click();
  await expect(page.getByRole("dialog").getByRole("alert")).toContainText("Gamma Source");
  expect(bodies).toEqual([{ target_id: "b-target", source_ids: ["b-source", "g-source"] }]);
  expect(duplicateReads).toBeGreaterThan(1);
  await expect(page.getByRole("checkbox", { name: /Beta Source/ })).toHaveCount(0);
  await expect(page.getByRole("checkbox", { name: /Gamma Source/ })).toBeChecked();
  await expect(page.getByRole("checkbox", { name: /Beta Target/ })).not.toBeChecked();
});

test("two-link setup failure survives unrelated success and reload without re-verification", async ({ context, page }) => {
  await auth(context);
  const verified = new Set<string>();
  let patchCount = 0;
  let sourceCount = 0;
  await context.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path === "/api/v1/auth/me") return json(route, me);
    if (path === "/api/v1/system/workbench") return json(route, workbench);
    if (path === "/api/v1/creators/creator-1") return json(route, creator);
    if (path === "/api/v1/creators/creator-1/links" && request.method() === "GET") return json(route, [
      { id: "link-1", creator_id: "creator-1", link_type: "pixiv", url: "https://pixiv.net/users/7", confidence: verified.has("link-1") ? 1 : 0.8, is_verified: verified.has("link-1"), source: "pixiv" },
      { id: "link-2", creator_id: "creator-1", link_type: "pixiv", url: "https://pixiv.net/users/8", confidence: verified.has("link-2") ? 1 : 0.8, is_verified: verified.has("link-2"), source: "pixiv" },
    ]);
    if (path.startsWith("/api/v1/creators/creator-1/links/link-")) { patchCount += 1; verified.add(path.endsWith("link-1") ? "link-1" : "link-2"); return json(route, { status: "ok" }); }
    if (path === "/api/v1/subscriptions") return json(route, [{ id: "sub-1", creator_id: "creator-1", name: "Creator One" }]);
    if (path === "/api/v1/subscriptions/sub-1/sources") {
      sourceCount += 1;
      if (sourceCount === 1) return json(route, { detail: "Repository authorization failed" }, 403);
      return json(route, { id: "repo-1", subscription_id: "sub-1", source: "pixiv", source_url: "https://pixiv.net/users/7", is_enabled: true });
    }
    return json(route, { items: [], total: 0 });
  });
  await page.goto("/admin/creators/creator-1/mapping");
  await page.getByRole("button", { name: "Approve" }).first().click({ force: true });
  await page.getByRole("button", { name: "Confirm" }).click();
  const partial = page.getByRole("alert").filter({ hasText: "Repository authorization failed" });
  await expect(partial).toBeVisible();
  await page.getByRole("button", { name: "Approve" }).click();
  await page.getByRole("button", { name: "Confirm" }).click();
  await expect(partial).toBeVisible();
  await page.reload();
  await expect(partial).toBeVisible();
  await page.getByRole("button", { name: /Retry repository setup/ }).click();
  await expect(partial).toHaveCount(0);
  expect(patchCount).toBe(2);
  expect(sourceCount).toBe(3);
});

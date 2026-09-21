import { expect, test, type Route } from "@playwright/test";

test.describe.configure({ timeout: 90_000 });

const json = (route: Route, body: unknown, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
const admin = { id: 7, username: "task5", display_name: "Task 5", is_admin: true, is_active: true, permissions: ["system", "tasks"], modules: { system: true, tasks: true }, preferences: {}, nsfw_visible: true, upload_used_bytes: 0, must_change_password: false };
const workbench = { updated_at: "2026-09-12T00:00:00Z", queue: { default: 0, scheduled: 0, failed: 0, active_download_count: 0, active_import_count: 0, failed_download_count: 0, failed_import_count: 0, stale_download_count: 0, stale_import_count: 0, stale_count: 0 }, scheduler: { enabled: true, mode: "interval", timezone: "UTC", scan_interval_minutes: 60 }, storage: { disk_total_bytes: 1, disk_free_bytes: 1, disk_used_bytes: 0, risk_level: "ok" }, health: {}, attention: { auth_unhealthy_count: 0, failed_download_count: 0, failed_import_count: 0, stale_job_count: 0, low_disk_warning: false, scheduler_disabled_warning: false }, recent: { download_jobs: [], import_jobs: [], tasks: [], works: [], successful_syncs: [] } };
const capabilities = Object.fromEntries(["automatic_projection", "reconcile", "backfill", "rebuild", "push", "pull", "verify", "commit"].map((name) => [name, { enabled: name === "verify", reason: name === "verify" ? null : "gitllery_shadow_only" }]));
const gitllery = {
  product_name: "Gitllery", product_version: "v1", format_id: "gitllery-segment", format_revision: 1,
  projection_mode: "shadow", build_generation: "fixture", managed_by: "deployment_environment", read_only: true,
  capabilities,
  cli: { max_works_per_commit: 25, max_operations_per_commit: 100, token_storage: "client_only", server_stores_cli_token: false, examples: { config: "gitllery config exact", login: "gitllery login", status: "gitllery status", log: "gitllery log", verify: "gitllery verify", commit: "gitllery commit" } },
  governance_scope: { observation: "host_and_auto_gallery", enforcement: "auto_gallery_only", modifies_other_projects: false, modifies_host_configuration: false },
  status: { repositories: [], missing_repos: 0, behind_total: 0, product_version: "v1", format_id: "gitllery-segment", format_revision: 1, projection_mode: "shadow" },
};

test("Gitllery caller serializes exact clipboard writes and localizes rejection", async ({ context, page }) => {
  const unhandled: string[] = [];
  await context.addCookies([{ name: "ag_token", value: "fixture", domain: "127.0.0.1", path: "/" }]);
  await context.addInitScript(() => {
    localStorage.setItem("ag_token", "fixture");
    localStorage.setItem("auto-gallery-lang", "en");
    (window as any).__clipboardWrites = [];
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText: (text: string) => {
      (window as any).__clipboardWrites.push(text);
      return new Promise<void>((resolve) => { (window as any).__resolveClipboard = resolve; });
    } } });
  });
  await context.route("**/api/v1/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/api/v1/auth/me") return json(route, admin);
    if (path === "/api/v1/auth/ws-ticket") return json(route, { detail: "fixture websocket unavailable" }, 503);
    if (path === "/api/v1/system/workbench") return json(route, workbench);
    if (path === "/api/v1/admin/gitllery/settings") return json(route, gitllery);
    if (path === "/api/v1/tasks") return json(route, { total: 0, items: [] });
    if (path === "/api/v1/system/scheduler-decisions") return json(route, { total: 0, items: [], summary: { blocked_count: 0 } });
    if (path === "/api/v1/operations/overview") return json(route, { summary: { attention: 0 }, items: [] });
    unhandled.push(`${route.request().method()} ${path}`);
    return json(route, { detail: `Unhandled fixture request: ${path}` }, 501);
  });

  await page.goto("/admin/settings/gitllery");
  const copyButtons = page.getByRole("button", { name: /Copy .* command/ });
  await expect(copyButtons).toHaveCount(6);
  await copyButtons.first().click();
  await expect(copyButtons.nth(1)).toBeDisabled();
  expect(await page.evaluate(() => (window as any).__clipboardWrites)).toEqual(["gitllery config exact"]);
  await page.evaluate(() => (window as any).__resolveClipboard());
  await expect(page.getByText("Copied")).toBeVisible();

  await page.evaluate(() => Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText: async () => { throw new Error("denied"); } } }));
  await copyButtons.nth(1).click();
  await expect(page.getByText("Clipboard access failed; copy the command manually.")).toBeVisible();
  expect(unhandled).toEqual([]);
});

import { expect, test, type BrowserContext, type Route } from "@playwright/test";

test.describe.configure({ timeout: 90_000 });

const admin = { id: 7, username: "task5", display_name: "Task 5", is_admin: true, is_active: true, permissions: ["library", "curation", "tasks", "system", "users"], modules: { library: true, curation: true, tasks: true, system: true, users: true }, preferences: {}, nsfw_visible: true, upload_used_bytes: 0, must_change_password: false };
const workbench = { updated_at: "2026-09-12T00:00:00Z", queue: { default: 0, scheduled: 0, failed: 0, active_download_count: 0, active_import_count: 0, failed_download_count: 0, failed_import_count: 0, stale_download_count: 0, stale_import_count: 0, stale_count: 0 }, scheduler: { enabled: true, mode: "interval", timezone: "UTC", scan_interval_minutes: 60 }, storage: { disk_total_bytes: 1, disk_free_bytes: 1, disk_used_bytes: 0, risk_level: "ok" }, health: {}, attention: { auth_unhealthy_count: 0, failed_download_count: 0, failed_import_count: 0, stale_job_count: 0, low_disk_warning: false, scheduler_disabled_warning: false }, recent: { download_jobs: [], import_jobs: [], tasks: [], works: [], successful_syncs: [] } };
const settings = { dedup: {}, subscription_defaults: { default_sync_interval_hours: 6, scheduler_scan_interval_minutes: 60, scheduler_enabled: true, schedule_mode: "interval", scheduled_times: "", timezone: "UTC" }, download_defaults: { timeout_seconds: 600, stall_timeout_seconds: 120, max_retries: 3, retry_backoff_base_seconds: 60, max_posts: 200, skip_ai_generated: false, gallerydl_retries: 3, gallerydl_timeout: 30, gallerydl_abort: 5, download_concurrency: 2, auto_resolve_upstream_conflicts: true }, proxy: { http_proxy: "", https_proxy: "", no_proxy: "", enabled: false } };

const json = (route: Route, body: unknown, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });

async function fixture(context: BrowserContext, language: "en" | "zh", handler: (route: Route, path: string) => Promise<boolean>) {
  const unhandled: string[] = [];
  await context.addCookies([{ name: "ag_session", value: "fixture", domain: "127.0.0.1", path: "/" }, { name: "ag_csrf", value: "fixture-csrf", url: process.env.PLAYWRIGHT_BASE_URL || "http://127.0.0.1:13000" }]);
  await context.addInitScript((lang) => { localStorage.setItem("ag_token", "fixture"); localStorage.setItem("auto-gallery-lang", lang); }, language);
  await context.route("https://fonts.loli.net/**", (route) => route.fulfill({ status: 200, body: "" }));
  await context.route("**/api/v1/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (await handler(route, path)) return;
    if (path === "/api/v1/auth/me") return json(route, admin);
    if (path === "/api/v1/auth/ws-ticket") return json(route, { detail: "fixture websocket unavailable" }, 503);
    if (path === "/api/v1/system/workbench") return json(route, workbench);
    if (path === "/api/v1/tasks") return json(route, { total: 0, items: [] });
    if (path === "/api/v1/system/scheduler-decisions") return json(route, { updated_at: "2026-09-12T00:00:00Z", scheduler_enabled: true, timezone: "UTC", view: "attention", total: 0, offset: 0, limit: 100, next_offset: null, summary: { blocked_count: 0, overdue_count: 0, oldest_overdue_at: null }, suppressed_count: 0, items: [] });
    if (path === "/api/v1/operations/overview") return json(route, { view: "attention", summary: { critical: 0, warning: 0, active: 0, resource_limited: 0, attention: 0 }, items: [] });
    unhandled.push(`${route.request().method()} ${path}`);
    await json(route, { detail: `Unhandled fixture request: ${path}` }, 501);
  });
  return unhandled;
}

test("download defaults preserves zero through the actual Save caller", async ({ context, page }) => {
  let saved: any = null;
  const unhandled = await fixture(context, "en", async (route, path) => {
    if (path === "/api/v1/admin/settings" && route.request().method() === "GET") { await json(route, settings); return true; }
    if (path === "/api/v1/admin/settings" && route.request().method() === "PUT") { saved = route.request().postDataJSON(); await json(route, { status: "ok", message: "saved" }); return true; }
    if (path === "/api/v1/system/health") { await json(route, { status: "ok", resource_pressure: { download_concurrency: 2 } }); return true; }
    return false;
  });
  await page.goto("/admin/settings/download-defaults");
  const retries = page.getByRole("spinbutton", { name: "Max Retries" });
  await retries.fill("0");
  await page.getByRole("button", { name: "Save" }).click();
  await expect.poll(() => saved?.download_defaults?.max_retries).toBe(0);
  expect(unhandled).toEqual([]);
});

test("mobile tag dialog contains long labels and owns focus, Escape, and scroll restoration", async ({ context, page }) => {
  const longName = "极长标签".repeat(35);
  const unhandled = await fixture(context, "zh", async (route, path) => {
    if (path === "/api/v1/tags/tag-long") { await json(route, { id: "tag-long", normalized_name: longName, category: "general", usage_count: 0, created_at: "2026-09-12T00:00:00Z", top_creators: [] }); return true; }
    if (path === "/api/v1/search") { await json(route, { groups: { works: { total: 0, items: [] } }, parsed: { tokens: [] }, diagnostics: [] }); return true; }
    return false;
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/admin/tags/tag-long");
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  const edit = page.getByRole("button", { name: "编辑" });
  await edit.click();
  await expect(page.getByRole("dialog")).toBeVisible();
  await expect.poll(() => page.getByRole("dialog").evaluate((dialog) => dialog.contains(document.activeElement))).toBe(true);
  await expect.poll(() => page.evaluate(() => document.body.style.overflow)).toBe("hidden");
  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await expect.poll(() => page.evaluate(() => document.body.style.overflow)).toBe("");
  await expect(edit).toBeFocused();
  expect(unhandled).toEqual([]);
});

test("command palette keyboard, pointer, and Escape agree with mounted destination content", async ({ context, page }) => {
  const unhandled = await fixture(context, "en", async (route, path) => {
    if (path === "/api/v1/search/assist") { await new Promise((resolve) => setTimeout(resolve, 120)); await json(route, { query: route.request().postDataJSON().before_cursor, canonical_query: route.request().postDataJSON().before_cursor, parsed: { tokens: [] }, diagnostics: [], suggestions: [] }); return true; }
    if (path === "/api/v1/tags/page") { await json(route, { total: 0, offset: 0, limit: 100, items: [] }); return true; }
    if (path === "/api/v1/search") { await json(route, { groups: { works: { total: 0, items: [] } }, parsed: { tokens: [] }, diagnostics: [] }); return true; }
    if (path === "/api/v1/works/derivative-progress") { await json(route, { queued: 0, running: 0, failed: 0, complete: 0, total: 0 }); return true; }
    return false;
  });
  await page.goto("/admin");
  const openPalette = page.getByRole("button", { name: /Search works/ }).first();
  await openPalette.click();
  const search = page.getByRole("combobox", { name: /Search works/ });
  await expect(search).toBeFocused();
  await search.fill("tags");
  await expect(page.getByRole("option", { name: /^Tags\b/ }).first()).toHaveAttribute("aria-selected", "true");
  await page.keyboard.press("Enter");
  await expect(page).toHaveURL(/\/admin\/tags$/);
  await expect(page.getByRole("heading", { level: 1, name: "Tags" })).toBeVisible();
  await expect(page.getByRole("dialog")).toHaveCount(0);

  await openPalette.click();
  await expect(search).toBeFocused();
  await search.fill("works");
  await page.getByRole("option", { name: /^Works\b/ }).first().click();
  await expect(page).toHaveURL(/\/admin\/works$/, { timeout: 20_000 });
  await expect(page.getByRole("heading", { level: 1, name: "Works" })).toBeVisible({ timeout: 20_000 });
  await expect(page.getByRole("dialog")).toHaveCount(0);

  await openPalette.click();
  await expect(search).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await expect(openPalette).toBeFocused();
  expect(unhandled).toEqual([]);
});

test("self-delete stays unavailable while current-principal resolution fails", async ({ context, page }) => {
  const unhandled = await fixture(context, "en", async (route, path) => {
    if (path === "/api/v1/auth/me") { await json(route, { detail: "principal unavailable" }, 503); return true; }
    if (path === "/api/v1/admin/users/7") { await json(route, admin); return true; }
    return false;
  });
  await page.goto("/admin/settings/users/7");
  await expect(page.getByRole("button", { name: "Delete User" })).toHaveCount(0);
  expect(unhandled).toEqual([]);
});

test("filtered tag creation follows the created category through the actual caller", async ({ context, page }) => {
  const listQueries: string[] = [];
  const unhandled = await fixture(context, "en", async (route, path) => {
    if (path === "/api/v1/tags/page" && route.request().method() === "GET") {
      listQueries.push(new URL(route.request().url()).search);
      await json(route, { total: 0, offset: 0, limit: 100, items: [] });
      return true;
    }
    if (path === "/api/v1/tags" && route.request().method() === "POST") {
      await json(route, { id: "created-general", normalized_name: "reachable-tag", category: "general", usage_count: 0, created_at: "2026-09-12T00:00:00Z" });
      return true;
    }
    return false;
  });
  await page.goto("/admin/tags?category=artist");
  await page.getByRole("button", { name: "New Tag" }).click();
  const dialog = page.getByRole("dialog");
  await dialog.locator("input").fill("Reachable Tag");
  await dialog.locator("select").selectOption("general");
  await dialog.getByRole("button", { name: "Create" }).click();
  await expect(page).toHaveURL(/category=general/);
  await expect(page).toHaveURL(/q=reachable-tag/);
  await expect.poll(() => listQueries.some((query) => query.includes("category=general") && query.includes("q=reachable-tag"))).toBe(true);
  expect(unhandled).toEqual([]);
});

test("works debounce preserves a rapid view navigation through settlement", async ({ context, page }) => {
  const searches: string[] = [];
  const unhandled = await fixture(context, "en", async (route, path) => {
    if (path === "/api/v1/search") {
      const query = new URL(route.request().url()).searchParams.get("q") || "";
      searches.push(query);
      await json(route, { query, canonical_query: query, parsed: { tokens: [] }, diagnostics: [], next_cursor: null, groups: { works: { total: 0, offset: 0, limit: 30, items: [] } } });
      return true;
    }
    if (path === "/api/v1/search/assist") { await json(route, { query: "", canonical_query: "", parsed: { tokens: [] }, diagnostics: [], suggestions: [] }); return true; }
    if (path === "/api/v1/works/derivative-progress") { await json(route, { remaining: 0, queued: 0, running: 0, failed: 0, complete: 0, total: 0 }); return true; }
    return false;
  });
  await page.goto("/admin/works?q=is%3Afavorite&view=list");
  const search = page.getByRole("combobox", { name: "Search title..." });
  await search.fill("is:favorite sky");
  await page.getByRole("button", { name: "Display", exact: true }).click();
  await page.getByRole("dialog", { name: "Display settings" }).getByRole("button", { name: "Grid", exact: true }).click();
  await page.waitForTimeout(700);
  await expect(page).toHaveURL(/q=is%3Afavorite(?:\+|%20)sky/);
  expect(new URL(page.url()).searchParams.has("view")).toBe(false);
  expect(searches).toContain("is:favorite sky");
  expect(unhandled).toEqual([]);
});

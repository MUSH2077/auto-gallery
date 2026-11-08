import { expect, test, type BrowserContext, type Route } from "@playwright/test";

const origin = process.env.PLAYWRIGHT_BASE_URL || "http://127.0.0.1:13000";
const json = (route: Route, body: unknown, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
async function account(context: BrowserContext, preferences: Record<string, unknown> = {}) {
  let saved = preferences;
  await context.addCookies([{ name: "ag_session", value: "theme-session", url: origin }, { name: "ag_csrf", value: "theme-csrf", url: origin }]);
  await context.route("**/api/v1/**", route => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/api/v1/auth/me/preferences") { saved = route.request().postDataJSON().preferences; return json(route, { preferences: saved }); }
    if (path === "/api/v1/auth/me") return json(route, { id: 84, username: "theme-review", is_admin: true, is_active: true, permissions: ["library", "tasks"], preferences: saved, must_change_password: false });
    if (path === "/api/v1/system/workbench") return json(route, {updated_at:"2026-10-24T00:00:00Z",queue:{default:0,scheduled:0,failed:0,active_download_count:0,active_import_count:0,failed_download_count:0,failed_import_count:0,stale_download_count:0,stale_import_count:0,stale_count:0},scheduler:{enabled:true},storage:{},health:{},attention:{},recent:{download_jobs:[],import_jobs:[],works:[],successful_syncs:[]}});
    if (path === "/api/v1/tasks") return json(route, { total: 0, items: [], summary: { all: 0, attention: 0, running: 0, waiting: 0 } });
    if (path === "/api/v1/search") return json(route, { parsed: { tokens: [], diagnostics: [] }, groups: { works: { total: 0, items: [] } }, total: 0, next_cursor: null });
    if (path === "/api/v1/notifications") return json(route, { total: 0, items: [] });
    return json(route, {});
  });
  return { readPreferences: () => saved };
}
const canvas = (page: import("@playwright/test").Page) => page.locator(".gallery-ui").evaluate(element => getComputedStyle(element).getPropertyValue("--canvas").trim());

test("new login stays in the approved light palette even when the system is dark", async ({ page }) => {
  await page.emulateMedia({ colorScheme: "dark" });
  await page.goto("/admin/login"); await expect(page.locator("#layout-login-username")).toBeVisible();
  await expect.poll(() => canvas(page)).toBe("#edf1ed");
  await expect(page.locator("html")).not.toHaveClass(/dark/);
});

test("an account without a theme uses light instead of following the system", async ({ page, context }) => {
  await account(context); await page.emulateMedia({ colorScheme: "dark" });
  await page.goto("/admin/works"); await expect(page.locator(".gallery-account-trigger")).toBeVisible();
  await expect.poll(() => canvas(page)).toBe("#edf1ed");
  await page.emulateMedia({ colorScheme: "light" }); await page.emulateMedia({ colorScheme: "dark" });
  await expect(page.locator("html")).not.toHaveClass(/dark/);
});

test("manual dark and light choices persist through navigation and reload", async ({ page, context }) => {
  const state = await account(context, { lang: "en" }); await page.emulateMedia({ colorScheme: "dark" });
  await page.goto("/admin/works"); await page.locator(".gallery-account-trigger").click();
  await page.getByRole("button", { name: "Light — Dark", exact: true }).click();
  await expect.poll(() => canvas(page)).toBe("#29322c");
  await expect.poll(() => page.evaluate(() => localStorage.getItem("auto-gallery-theme"))).toBe("dark");
  await expect.poll(async () => state.readPreferences().theme).toBe("dark");
  await page.locator(".layout-bottom-nav").getByRole("link", { name: "Jobs", exact: true }).click(); await page.reload();
  await expect.poll(() => canvas(page)).toBe("#29322c"); await page.locator(".gallery-account-trigger").click();
  await page.getByRole("button", { name: "Dark — Light", exact: true }).click();
  await expect.poll(() => canvas(page)).toBe("#edf1ed");
  await expect.poll(async () => state.readPreferences().theme).toBe("light");
  await page.reload(); await expect.poll(() => canvas(page)).toBe("#edf1ed");
});

test("saved account light preference overrides stale browser dark preference", async ({ page, context }) => {
  await account(context, { theme: "light" });
  await context.addInitScript(() => { localStorage.setItem("auto-gallery-theme", "dark"); });
  await page.emulateMedia({ colorScheme: "dark" }); await page.goto("/admin/works");
  await expect.poll(() => canvas(page)).toBe("#edf1ed");
  await expect.poll(() => page.evaluate(() => localStorage.getItem("auto-gallery-theme"))).toBe("light");
});


test("the selected theme is applied before client hydration", async ({ context }) => {
  for (const stored of [null, "light", "dark"]) {
    const page = await context.newPage();
    try {
      await page.emulateMedia({ colorScheme: "dark" });
      await page.addInitScript(value => { if (value) localStorage.setItem("auto-gallery-theme", value); else localStorage.removeItem("auto-gallery-theme"); }, stored);
      await page.route("**/_next/static/chunks/**.js", route => route.abort());
      await page.goto("/admin/login", { waitUntil: "domcontentloaded" });
      const expected = stored === "dark";
      expect(await page.locator("html").evaluate(element => element.classList.contains("dark"))).toBe(expected);
      expect(await page.locator("html").evaluate(element => element.style.colorScheme)).toBe(expected ? "dark" : "light");
    } finally { await page.close(); }
  }
});


test("task technical content shares the approved local typography without a fifth font", async ({ page, context }) => {
  const externalFonts: string[] = [];
  page.on("request", request => {
    if (request.url().includes("JetBrains")) externalFonts.push(request.url());
  });
  await account(context, { lang: "en", theme: "light" });
  await context.route("**/api/v1/tasks/font-task", route => json(route, {
    id: "font-task", title: "Typography check", kind: "download", status: "failed",
    created_at: "2026-10-28T00:00:00Z", available_actions: [],
    progress_current: 12, progress_total: 34,
  }));
  await page.goto("/admin/jobs?task=font-task");
  const detail = page.getByRole("dialog", { name: "Task detail", exact: true });
  await detail.getByText("Technical details", { exact: true }).click();
  const code = detail.locator("pre");
  await expect(code).toBeVisible();
  await expect(code).toHaveCSS("font-family", /Fraunces.*LXGW WenKai/);
  await expect(detail.locator(".detail-id")).toHaveCSS("font-family", /Fraunces.*LXGW WenKai/);
  expect(externalFonts).toEqual([]);
  await page.keyboard.press("Escape");
  await expect(detail).toHaveCount(0);
});


test("account language switches visible text and logout follows change password", async ({ page, context }) => {
  await account(context, { lang: "zh" });
  await page.goto("/admin/works");
  await page.locator(".gallery-account-trigger").click();
  const header = page.locator(".gallery-account-header");
  await expect(header.getByText("theme-review")).toBeVisible();
  await expect(page.locator(".gallery-account-menu").getByRole("button", { name: "退出登录" })).toBeVisible();
  await page.getByRole("button", { name: "Switch to English" }).click();
  await expect(page.locator(".gallery-account-trigger")).toHaveText("Account");
  await expect(page.locator(".gallery-account-menu").getByRole("button", { name: /log out|sign out/i })).toBeVisible();
  await page.getByRole("button", { name: "切换到中文" }).click();
  await expect(page.locator(".gallery-account-trigger")).toHaveText("账户");
});

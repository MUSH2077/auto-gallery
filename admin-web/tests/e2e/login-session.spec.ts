import { expect, test, type Route } from "@playwright/test";

const principal = {
  id: 81, username: "account-review", display_name: "Review",
  is_admin: false, is_active: true, permissions: ["library", "tasks"],
  modules: {}, preferences: { theme: "light", lang: "en" },
  nsfw_visible: true, upload_used_bytes: 0, must_change_password: false,
};
const reply = (route: Route, body: unknown, status = 200) =>
  route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });

async function mockSession(page: import("@playwright/test").Page, status = 200, user = principal) {
  if (status !== 401) {
    await page.context().addCookies([
      { name: "ag_session", value: "session-fixture", url: process.env.PLAYWRIGHT_BASE_URL || "http://127.0.0.1:13000" },
    ]);
  }
  await page.context().addInitScript(() => {
    localStorage.setItem("auto-gallery-theme", "light");
    localStorage.setItem("auto-gallery-lang", "en");
  });
  await page.context().route("**/api/v1/**", async route => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/api/v1/auth/me") return reply(route, status === 200 ? user : { detail: "Session unavailable" }, status);
    if (path === "/api/v1/search") return reply(route, {
      q: "", parsed: { tokens: [], diagnostics: [] }, groups: { works: { total: 0, items: [] } },
      total: 0, items: [], next_cursor: null,
    });
    if (path === "/api/v1/works/derivative-progress") return reply(route, { total: 0, remaining: 0, status: "idle" });
    if (path === "/api/v1/tasks" || path === "/api/v1/notifications" || path === "/api/v1/import-jobs") return reply(route, { total: 0, items: [] });
    if (path === "/api/v1/download-jobs") return reply(route, []);
    return reply(route, {});
  });
}

for (const entry of ["/admin/works"]) {
  test(entry + " redirects a missing session to the existing login", async ({ page }) => {
    await mockSession(page, 401);
    await page.goto(entry);
    await expect(page).toHaveURL(/\/admin\/login(?:\?|$)/);
    await expect(page.getByRole("textbox").first()).toBeVisible();
  });

  test(entry + " shows session-service recovery rather than protected content", async ({ page }) => {
    await mockSession(page, 503);
    await page.goto(entry);
    await expect(page.getByRole("alert").filter({ hasText: /Sign-in service/i })).toBeVisible();
    await expect(page.getByRole("button", { name: /retry/i })).toBeVisible();
    await expect(page).toHaveURL(new RegExp(entry.replaceAll("/", "\\/") + "(?:\\?|$)"));
  });

  test(entry + " requires password change using the existing account route", async ({ page }) => {
    await mockSession(page, 200, { ...principal, must_change_password: true });
    await page.goto(entry);
    await expect(page).toHaveURL(/\/admin\/profile(?:\?|$)/);
  });
}

test("sign-in restores the protected page URL including filters", async ({ page }) => {
  await mockSession(page, 401);
  const target = "/admin/works?q=review";
  await page.goto(target);
  await expect(page).toHaveURL(/\/admin\/login\?/);
  expect(new URL(page.url()).searchParams.get("redirect")).toBe(target);
  let signedIn = false;
  await page.route("**/api/v1/auth/me", route =>
    reply(route, signedIn ? principal : { detail: "Session unavailable" }, signedIn ? 200 : 401));
  await page.route("**/api/v1/auth/browser/login", async route => {
    await page.context().addCookies([{ name: "ag_session", value: "session-fixture", url: page.url() }]);
    signedIn = true;
    return reply(route, { ok: true });
  });
  await page.getByLabel("Username", { exact: true }).fill("review");
  await page.getByLabel("Password", { exact: true }).fill("test-password");
  await page.locator('button[type="submit"]').click();
  await expect(page).toHaveURL(new RegExp("/admin/works\\?q=review$"));
});

test("sign-in keeps password change ahead of the return target", async ({ page }) => {
  await mockSession(page, 401);
  await page.goto("/admin/login?redirect=%2Fadmin%2Fworks");
  let signedIn = false;
  await page.route("**/api/v1/auth/me", route =>
    reply(route, signedIn ? { ...principal, must_change_password: true } : { detail: "Session unavailable" }, signedIn ? 200 : 401));
  await page.route("**/api/v1/auth/browser/login", async route => {
    await page.context().addCookies([{ name: "ag_session", value: "session-fixture", url: page.url() }]);
    signedIn = true;
    return reply(route, { ok: true });
  });
  await page.getByLabel("Username", { exact: true }).fill("review");
  await page.getByLabel("Password", { exact: true }).fill("test-password");
  await page.locator('button[type="submit"]').click();
  await expect(page).toHaveURL(/\/admin\/profile(?:\?|$)/);
});

test("sign-in prevents duplicate submission and recovers from service failure", async ({ page }) => {
  await mockSession(page, 401);
  await page.goto("/admin/login?redirect=%2Fadmin%2Fworks");
  let requests = 0;
  let finish!: () => void;
  const pending = new Promise<void>(resolve => { finish = resolve; });
  await page.route("**/api/v1/auth/browser/login", async route => {
    requests += 1;
    await pending;
    return reply(route, { detail: "Service unavailable" }, 503);
  });
  await page.getByLabel("Username", { exact: true }).fill("review");
  await page.getByLabel("Password", { exact: true }).fill("test-password");
  await page.locator('button[type="submit"]').click();
  await expect(page.locator('button[type="submit"]')).toBeDisabled();
  await page.locator("form").evaluate(form => { (form as HTMLFormElement).requestSubmit(); (form as HTMLFormElement).requestSubmit(); });
  expect(requests).toBe(1);
  finish();
  await expect(page.locator('p[role="alert"]')).toHaveText(/Sign-in service is temporarily unavailable/);
  await expect(page.locator('p[role="alert"]')).toBeFocused();
  await expect(page.getByLabel("Username", { exact: true })).toHaveValue("review");
  await expect(page.locator('button[type="submit"]')).toBeEnabled();
});


test("sign-in survives legacy-token storage SecurityError", async ({ page }) => {
  await page.addInitScript(() => {
    const removeItem = Storage.prototype.removeItem;
    Storage.prototype.removeItem = function (key: string) {
      if (key === "ag_token") throw new DOMException("Storage disabled", "SecurityError");
      removeItem.call(this, key);
    };
  });
  await mockSession(page, 401);
  const target = "/admin/works?storage=disabled";
  await page.goto(target);
  await expect(page).toHaveURL(/\/admin\/login\?/);
  let signedIn = false;
  await page.route("**/api/v1/auth/me", route =>
    reply(route, signedIn ? principal : { detail: "Session unavailable" }, signedIn ? 200 : 401));
  await page.route("**/api/v1/auth/browser/login", async route => {
    await page.context().addCookies([{ name: "ag_session", value: "session-fixture", url: page.url() }]);
    signedIn = true;
    return reply(route, { ok: true });
  });
  await page.getByLabel("Username", { exact: true }).fill("review");
  await page.getByLabel("Password", { exact: true }).fill("test-password");
  await page.locator('button[type="submit"]').click();
  await expect(page).toHaveURL(/\/admin\/works\?storage=disabled$/);
});

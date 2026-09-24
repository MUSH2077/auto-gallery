import { expect, test, type Browser, type Route } from "@playwright/test";

test.describe.configure({ timeout: 60_000 });

const json = (route: Route, body: unknown, status = 200) => route.fulfill({
  status,
  contentType: "application/json",
  body: JSON.stringify(body),
});

const modules = {
  library: "Library",
  curation: "Curation",
  upload: "Upload",
  subscriptions: "Subscriptions",
  tasks: "Tasks",
  system: "System",
};

const user = {
  id: 42,
  username: "managed-user",
  display_name: "Managed User",
  is_admin: false,
  is_active: true,
  permissions: ["library"],
  nsfw_visible: true,
  upload_quota_bytes: null,
  upload_used_bytes: 0,
  must_change_password: false,
  last_login_at: null,
  created_at: "2026-09-13T00:00:00Z",
};

const me = (permissions: string[], isAdmin = false) => ({
  ...user,
  id: isAdmin ? 1 : 2,
  username: isAdmin ? "admin" : "non-admin",
  display_name: isAdmin ? "Admin" : "Non-admin",
  is_admin: isAdmin,
  permissions,
  modules,
  preferences: {},
});

async function openWithRole(
  browser: Browser,
  pathname: string,
  principal: ReturnType<typeof me>,
) {
  const context = await browser.newContext();
  await context.addCookies([{ name: "ag_token", value: "fixture", domain: "127.0.0.1", path: "/" }]);
  await context.addInitScript(() => {
    localStorage.setItem("ag_token", "fixture");
    localStorage.setItem("auto-gallery-lang", "en");
  });
  const adminRequests: string[] = [];
  const unknownRequests: string[] = [];
  await context.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const apiPath = new URL(request.url()).pathname;
    if (apiPath === "/api/v1/auth/me" && request.method() === "GET") {
      return json(route, principal);
    }
    if (apiPath === "/api/v1/users" || apiPath.startsWith("/api/v1/users/")) {
      adminRequests.push(`${request.method()} ${apiPath}`);
      if (request.method() === "GET" && principal.is_admin) {
        return json(route, apiPath === "/api/v1/users" ? [user] : user);
      }
      return json(route, { detail: "Admin access required" }, 403);
    }
    if (apiPath === "/api/v1/auth/ws-ticket") {
      return json(route, { detail: "fixture websocket unavailable" }, 503);
    }
    if (apiPath === "/api/v1/system/workbench") {
      return json(route, { updated_at: "2026-09-13T00:00:00Z", queue: {}, scheduler: {}, storage: {}, health: {}, attention: {}, recent: {} });
    }
    if (apiPath === "/api/v1/tasks") return json(route, { total: 0, items: [] });
    if (apiPath === "/api/v1/system/scheduler-decisions") return json(route, { total: 0, items: [], summary: { blocked_count: 0 } });
    if (apiPath === "/api/v1/operations/overview") return json(route, { summary: { attention: 0 }, items: [] });
    unknownRequests.push(`${request.method()} ${apiPath}`);
    return route.abort("blockedbyclient");
  });
  const page = await context.newPage();
  await page.goto(pathname);
  return { context, page, adminRequests, unknownRequests };
}

test("system, library, and denied users cannot mount users list or detail admin hooks", async ({ browser }) => {
  const roles = [
    { name: "system", value: me(["system"]) },
    { name: "library", value: me(["library"]) },
    { name: "denied", value: me([]) },
  ];
  for (const role of roles) {
    for (const pathname of ["/admin/settings/users", "/admin/settings/users/42"]) {
      const opened = await openWithRole(browser, pathname, role.value);
      try {
        await expect(opened.page.getByText("You don't have permission to access this page", { exact: true })).toBeVisible();
        await expect(opened.page.getByRole("button", { name: /New|Create|Save|Reset Password|Delete User/ })).toHaveCount(0);
        expect(opened.adminRequests, `${role.name} ${pathname}`).toEqual([]);
        expect(opened.unknownRequests, `${role.name} ${pathname}`).toEqual([]);
      } finally {
        await opened.context.close();
      }
    }
  }
});

test("an administrator still mounts list and detail controls", async ({ browser }) => {
  const list = await openWithRole(browser, "/admin/settings/users", me([], true));
  try {
    await expect(list.page.getByRole("heading", { name: "User Management", exact: true })).toBeVisible();
    await expect(list.page.getByRole("button", { name: "+ New", exact: true })).toBeVisible();
    await expect(list.page.getByRole("link", { name: "Open Managed User", exact: true })).toBeVisible();
    expect(list.adminRequests).toEqual(["GET /api/v1/users"]);
    expect(list.unknownRequests).toEqual([]);
  } finally {
    await list.context.close();
  }

  const detail = await openWithRole(browser, "/admin/settings/users/42", me([], true));
  try {
    await expect(detail.page.getByRole("heading", { name: "Managed User", exact: true })).toBeVisible();
    await expect(detail.page.locator("#user-display-name")).toBeVisible();
    await expect(detail.page.getByRole("button", { name: "Reset Password", exact: true })).toBeVisible();
    await expect(detail.page.getByRole("button", { name: "Delete User", exact: true })).toBeVisible();
    expect(detail.adminRequests).toEqual(["GET /api/v1/users/42"]);
    expect(detail.unknownRequests).toEqual([]);
  } finally {
    await detail.context.close();
  }
});

test("validated session permissions are reused without a duplicate lookup", async ({ context, page }) => {
  await context.addCookies([{ name: "ag_token", value: "fixture", domain: "127.0.0.1", path: "/" }]);
  await context.addInitScript(() => {
    localStorage.setItem("ag_token", "fixture");
    localStorage.setItem("auto-gallery-lang", "en");
  });
  let sessionRequests = 0;
  let duplicatePermissionRequests = 0;
  let userRequests = 0;
  const unknownRequests: string[] = [];
  await context.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const apiPath = new URL(request.url()).pathname;
    if (apiPath === "/api/v1/auth/me") {
      if (!request.headers()["content-type"]) {
        sessionRequests += 1;
        return json(route, me([], true));
      }
      duplicatePermissionRequests += 1;
      return json(route, { detail: "duplicate permission lookup" }, 503);
    }
    if (apiPath === "/api/v1/users") {
      userRequests += 1;
      return json(route, [user]);
    }
    if (apiPath === "/api/v1/auth/ws-ticket") return json(route, { detail: "fixture websocket unavailable" }, 503);
    if (apiPath === "/api/v1/system/workbench") return json(route, { updated_at: "2026-09-13T00:00:00Z", queue: {}, scheduler: {}, storage: {}, health: {}, attention: {}, recent: {} });
    if (apiPath === "/api/v1/tasks") return json(route, { total: 0, items: [] });
    if (apiPath === "/api/v1/system/scheduler-decisions") return json(route, { total: 0, items: [], summary: { blocked_count: 0 } });
    if (apiPath === "/api/v1/operations/overview") return json(route, { summary: { attention: 0 }, items: [] });
    unknownRequests.push(`${request.method()} ${apiPath}`);
    return route.abort("blockedbyclient");
  });

  await page.goto("/admin/settings/users");
  await expect(page.getByRole("button", { name: "+ New", exact: true })).toBeVisible();
  expect(sessionRequests).toBe(1);
  expect(duplicatePermissionRequests).toBe(0);
  expect(userRequests).toBe(1);
  expect(unknownRequests).toEqual([]);
});

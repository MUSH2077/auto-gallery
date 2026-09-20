import { expect, test, type Browser, type BrowserContext, type Route } from "@playwright/test";

test.describe.configure({ timeout: 90_000 });

const ADMIN_TASK_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const DOWNLOAD_TASK_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";

const principal = {
  id: 62,
  username: "notification-operator",
  display_name: "Notification Operator",
  is_admin: false,
  is_active: true,
  permissions: ["tasks"],
  modules: { tasks: "Tasks" },
  preferences: {},
  nsfw_visible: true,
  upload_used_bytes: 0,
  must_change_password: false,
};

const json = (route: Route, body: unknown, status = 200) => route.fulfill({
  status,
  contentType: "application/json",
  body: JSON.stringify(body),
});

function task(id: string, kind: string, title: string, createdAt = "2026-09-20T12:00:00Z") {
  return {
    id,
    kind,
    operation_type: kind === "admin" ? "admin-integrity-scan" : kind,
    status: "complete",
    title,
    source: kind === "download" ? "pixiv" : null,
    created_at: createdAt,
  };
}

async function openContext(browser: Browser) {
  const context = await browser.newContext();
  await context.routeWebSocket("**/api/v1/ws", (socket) => socket.close({ code: 1000, reason: "fixture" }));
  await context.addCookies([{ name: "ag_token", value: "fixture", domain: "127.0.0.1", path: "/" }]);
  await context.addInitScript(() => {
    localStorage.setItem("ag_token", "fixture");
    localStorage.setItem("auto-gallery-lang", "en");
  });
  return context;
}

test("task filter excludes account events at the server and every task kind opens its detail", async ({ browser }) => {
  const context = await openContext(browser);
  const taskRequests: string[] = [];
  await context.route("**/api/v1/**", async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname === "/api/v1/auth/me") return json(route, principal);
    if (url.pathname === "/api/v1/auth/ws-ticket") return json(route, { detail: "fixture websocket unavailable" }, 503);
    if (url.pathname === "/api/v1/tasks") {
      taskRequests.push(url.search);
      if (url.searchParams.get("include_account") === "true") {
        return json(route, { total: 1, items: [task("cccccccc-cccc-4ccc-8ccc-cccccccccccc", "account", "Account event")] });
      }
      return json(route, {
        total: 2,
        items: [
          task(ADMIN_TASK_ID, "admin", "Integrity scan"),
          task(DOWNLOAD_TASK_ID, "download", "Pixiv download"),
        ],
      });
    }
    return json(route, { total: 0, items: [] });
  });

  const page = await context.newPage();
  try {
    await page.goto("/admin/notifications");
    await expect(page.getByText("Account event", { exact: true })).toBeVisible();
    await page.getByRole("button", { name: "Tasks", exact: true }).click();
    await expect(page.getByText("Integrity scan", { exact: true })).toBeVisible();
    expect(taskRequests.some((search) => !new URLSearchParams(search).has("include_account"))).toBe(true);

    await page.getByText("Integrity scan", { exact: true }).click();
    await expect(page).toHaveURL(new RegExp(`/admin/jobs\\?tab=admin&task=${ADMIN_TASK_ID}$`));
  } finally {
    await context.close();
  }
});

test("account filter can load the next server page", async ({ browser }) => {
  const context = await openContext(browser);
  const accountItems = Array.from({ length: 51 }, (_, index) => task(
    `${String(index + 1).padStart(8, "0")}-1111-4111-8111-111111111111`,
    "account",
    `Account event ${index + 1}`,
    `2026-09-20T11:${String(59 - Math.min(index, 59)).padStart(2, "0")}:00Z`,
  ));
  await context.route("**/api/v1/**", async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname === "/api/v1/auth/me") return json(route, principal);
    if (url.pathname === "/api/v1/auth/ws-ticket") return json(route, { detail: "fixture websocket unavailable" }, 503);
    if (url.pathname === "/api/v1/tasks") {
      if (url.searchParams.get("kind") !== "account") return json(route, { total: 0, items: [] });
      const offset = Number(url.searchParams.get("offset") || 0);
      return json(route, { total: accountItems.length, items: accountItems.slice(offset, offset + 50) });
    }
    return json(route, { total: 0, items: [] });
  });

  const page = await context.newPage();
  try {
    await page.goto("/admin/notifications");
    await page.getByRole("button", { name: "Account", exact: true }).click();
    await expect(page.getByText("Account event 1", { exact: true })).toBeVisible();
    await page.getByRole("button", { name: "Load more", exact: true }).click();
    await expect(page.getByText("Account event 51", { exact: true })).toBeVisible();
  } finally {
    await context.close();
  }
});

test("notification bell opens an administrator task from the durable feed", async ({ browser }) => {
  const context = await openContext(browser);
  await context.route("**/api/v1/**", async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname === "/api/v1/auth/me") return json(route, principal);
    if (url.pathname === "/api/v1/auth/ws-ticket") return json(route, { detail: "fixture websocket unavailable" }, 503);
    if (url.pathname === "/api/v1/tasks") {
      return json(route, { total: 1, items: [task(ADMIN_TASK_ID, "admin", "Bell integrity scan")] });
    }
    if (url.pathname === `/api/v1/tasks/${ADMIN_TASK_ID}`) {
      return json(route, task(ADMIN_TASK_ID, "admin", "Bell integrity scan"));
    }
    if (url.pathname === "/api/v1/search/assist") {
      return json(route, { query: "", canonical_query: "", parsed: { tokens: [] }, suggestions: [] });
    }
    return json(route, { total: 0, items: [] });
  });

  const page = await context.newPage();
  try {
    await page.goto("/admin/profile");
    await page.getByRole("button", { name: "Notifications", exact: true }).click();
    await page.getByText("Bell integrity scan", { exact: true }).click();
    await expect(page).toHaveURL(new RegExp(`/admin/jobs\\?tab=admin&task=${ADMIN_TASK_ID}$`));
  } finally {
    await context.close();
  }
});

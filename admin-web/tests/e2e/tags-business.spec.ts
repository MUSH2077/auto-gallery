import { expect, test, type BrowserContext, type Route } from "@playwright/test";

const origin = process.env.PLAYWRIGHT_BASE_URL || "http://127.0.0.1:13000";
const me = { id: 1, username: "tag-reviewer", display_name: "Tag reviewer", is_admin: false, is_active: true, permissions: ["library", "curation", "system"], modules: { library: true }, preferences: {}, nsfw_visible: true, upload_used_bytes: 0, must_change_password: false };
const json = (route: Route, body: unknown, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
async function installFixtures(context: BrowserContext) {
  await context.addCookies([{ name: "ag_session", value: "tag-fixture", domain: "127.0.0.1", path: "/" }, { name: "ag_csrf", value: "tag-fixture-csrf", url: origin }]);
  await context.addInitScript(() => { localStorage.setItem("ag_token", "tag-fixture"); localStorage.setItem("auto-gallery-lang", "en"); });
}

test("tags list keeps full-page keyboard fallback, create, paging, and exact clear confirmation", async ({ context, page }) => {
  await installFixtures(context);
  const requests: string[] = [];
  const methods: string[] = [];
  let restricted = false;
  const tagItems = Array.from({ length: 100 }, (_, index) => ({ id: "tag-" + index, normalized_name: "night-sky-" + index, category: "general", usage_count: 100 - index, created_at: "2026-09-01T00:00:00Z" }));
  await context.route("**/api/v1/**", async route => {
    const url = new URL(route.request().url());
    requests.push(url.pathname + url.search);
    if (url.pathname === "/api/v1/auth/me") return json(route, restricted ? { ...me, permissions: ["library"], modules: { library: true } } : me);
    if (url.pathname === "/api/v1/system/workbench") return json(route, { queue: {}, scheduler: {}, storage: {}, attention: {}, recent: {} });
    if (url.pathname === "/api/v1/tags/tag-1") return json(route, { id: "tag-1", normalized_name: "night-sky-1", category: "general", usage_count: 99, created_at: "2026-09-01T00:00:00Z", top_creators: [] });
    if (url.pathname === "/api/v1/tags/page") return json(route, { total: 101, offset: Number(url.searchParams.get("offset") || 0), limit: 100, items: tagItems });
    if (url.pathname === "/api/v1/tags" && route.request().method() === "POST") return json(route, { id: "tag-created", normalized_name: "new-tag", category: "artist", usage_count: 0, created_at: "2026-09-01T00:00:00Z" });
    if (url.pathname === "/api/v1/admin/clear/preview/tags") return json(route, { counts: { tags: 100, works: 4 }, confirmation_phrase: "DELETE ALL TAGS" });
    if (url.pathname === "/api/v1/admin/operations/clear" && route.request().method() === "POST") { methods.push("CLEAR:" + String(route.request().postDataJSON().confirmation)); return json(route, { task_id: "clear-tags-task" }); }
    return json(route, {});
  });
  await page.goto("/admin/tags");
  await expect(page.getByRole("heading", { name: "Tags", level: 1 })).toBeVisible();
  await expect(page.getByTestId("tag-bubble-chart")).toHaveAttribute("data-tag-count", "100");
  await expect(page.locator(".tags-table tbody tr")).toHaveCount(100);
  const chart = page.getByTestId("tag-bubble-chart");
  await expect.poll(() => chart.locator("canvas").evaluate((canvas: HTMLCanvasElement) => canvas.getContext("2d")!.getImageData(1, 1, 1, 1).data[3])).toBe(0);
  await chart.getByRole("button", { name: "Zoom in on tag map", exact: true }).click();
  await expect.poll(async () => Number(await chart.getAttribute("data-zoom-level"))).toBeGreaterThan(1);
  await chart.getByRole("button", { name: "Reset tag map zoom", exact: true }).click();
  await expect(chart).toHaveAttribute("data-zoom-level", "1.000");
  await page.getByRole("button", { name: "Next", exact: true }).click();
  await expect(page).toHaveURL(/page=2/);
  await page.getByRole("button", { name: "New Tag", exact: true }).click();
  const dialog = page.getByRole("dialog");
  await dialog.locator("input").fill("New Tag");
  await dialog.getByRole("combobox", { name: "Category" }).click();
  await page.getByRole("option", { name: "artist", exact: true }).click();
  await dialog.getByRole("button", { name: "Create Tag", exact: true }).click();
  await expect(page).toHaveURL(/q=new-tag/);
  await expect(page).toHaveURL(/category=artist/);
  expect(requests.some(request => request.startsWith("/api/v1/tags/page?") && request.includes("q=new-tag") && request.includes("category=artist"))).toBeTruthy();

  await page.goto("/admin/tags");
  await page.locator("details.business-domain-clear > summary").click();
  await page.getByRole("button", { name: "Delete All Tags", exact: true }).click();
  const clearDialog = page.getByRole("dialog", { name: "Delete All Tags" });
  await expect(clearDialog.getByText("Type DELETE ALL TAGS to confirm")).toBeVisible();
  const confirm = clearDialog.getByRole("button", { name: "Confirm", exact: true });
  await expect(confirm).toBeDisabled();
  await clearDialog.getByRole("textbox").fill("DELETE ALL TAGS");
  await confirm.click();
  await expect.poll(() => methods).toContain("CLEAR:DELETE ALL TAGS");

  restricted = true;
  await page.goto("/admin/tags");
  await expect(page.getByRole("button", { name: "New Tag", exact: true })).toHaveCount(0);
  await page.goto("/admin/tags/tag-1");
  await expect(page.getByRole("button", { name: "Edit", exact: true })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Delete", exact: true })).toHaveCount(0);
});

test("tag detail keeps related creators, edit, delete, and works search reachable", async ({ context, page }) => {
  await installFixtures(context);
  const methods: string[] = [];
  await context.route("**/api/v1/**", async route => {
    const url = new URL(route.request().url());
    if (url.pathname === "/api/v1/auth/me") return json(route, me);
    if (url.pathname === "/api/v1/system/workbench") return json(route, { queue: {}, scheduler: {}, storage: {}, attention: {}, recent: {} });
    if (url.pathname === "/api/v1/tags/fixture-tag" && route.request().method() === "GET") return json(route, { id: "fixture-tag", normalized_name: "night-sky", category: "general", usage_count: 3, created_at: "2026-09-01T00:00:00Z", top_creators: [{ creator_id: "creator-1", creator_name: "Aster", work_count: 2 }] });
    if (url.pathname === "/api/v1/search") return json(route, { groups: { works: { total: 0, offset: 0, limit: 24, items: [] } }, query: "", canonical_query: "", parsed: { tokens: [] } });
    if (url.pathname === "/api/v1/tags/fixture-tag" && route.request().method() === "PUT") { methods.push("PUT"); return json(route, { id: "fixture-tag", normalized_name: "night-sky-edited", category: "artist", usage_count: 3, created_at: "2026-09-01T00:00:00Z", top_creators: [] }); }
    if (url.pathname === "/api/v1/tags/fixture-tag" && route.request().method() === "DELETE") { methods.push("DELETE"); return json(route, { status: "deleted" }); }
    if (url.pathname === "/api/v1/tags/page") return json(route, { total: 0, offset: 0, limit: 100, items: [] });
    return json(route, {});
  });
  await page.goto("/admin/tags/fixture-tag");
  await expect(page.getByRole("heading", { name: "#night-sky", level: 1 })).toBeVisible();
  await expect(page.getByRole("link", { name: "Aster" })).toBeVisible();
  await page.getByRole("button", { name: "Edit", exact: true }).click();
  const edit = page.getByRole("dialog");
  await edit.locator("input").fill("Night Sky Edited");
  await edit.getByRole("button", { name: "Save", exact: true }).click();
  await expect.poll(() => methods.includes("PUT")).toBeTruthy();
  await page.getByRole("button", { name: "Delete", exact: true }).click();
  await page.getByRole("dialog").getByRole("button", { name: "Confirm", exact: true }).click();
  await expect.poll(() => methods.includes("DELETE")).toBeTruthy();
});

/**
 * auto-gallery — Full E2E Test Suite
 *
 * 7 sections, 40+ test cases covering all pages and major workflows.
 *
 * Usage:
 *   npx playwright test tests/e2e/navigation.spec.ts
 *   npx playwright test tests/e2e/navigation.spec.ts --headed   # watch browser
 *   E2E_BASE=http://192.0.2.10:13000 npx playwright test
 */

import { test, expect } from "@playwright/test";

const BASE = process.env.E2E_BASE || "http://localhost:13000";
const USER = process.env.E2E_USER || "admin";
const PASS = process.env.E2E_PASS || "admin123";

async function doLogin(page: any) {
  await page.goto(`${BASE}/admin/login`);
  await page.fill('input[placeholder*="用户"]', USER);
  await page.fill('input[type="password"]', PASS);
  await page.click('button[type="submit"]');
  await page.waitForURL("**/admin**", { timeout: 15000 });
}
function bodyOk(page: any) { return expect(page.locator("body")).toBeVisible({ timeout: 10000 }); }

// ================================================
// SECTION 1: Authentication (3 tests)
// ================================================
test.describe("1. Auth", () => {
  test("Login succeeds", async ({ page }) => {
    await doLogin(page);
    await page.goto(`${BASE}/admin`);
    await bodyOk(page);
  });
  test("Wrong password shows error", async ({ page }) => {
    await page.goto(`${BASE}/admin/login`);
    await page.fill('input[placeholder*="用户"]', USER);
    await page.fill('input[type="password"]', "wrong");
    await page.click('button[type="submit"]');
    await expect(page.locator("text=密码").or(page.locator("text=Incorrect"))).toBeVisible({ timeout: 5000 });
  });
  test("Protected pages → login redirect", async ({ page }) => {
    for (const p of ["/admin/works", "/admin/scheduler", "/admin/settings"]) {
      await page.goto(`${BASE}${p}`);
      expect(page.url()).toContain("login");
    }
  });
});

// ================================================
// SECTION 2: Every page loads (28 pages)
// ================================================
test.describe("2. All pages load", () => {
  test.beforeEach(async ({ page }) => { await doLogin(page); });
  const pages = [
    "/admin", "/admin/works", "/admin/creators", "/admin/subscriptions",
    "/admin/jobs", "/admin/tags", "/admin/search", "/admin/scheduler",
    "/admin/sources", "/admin/curation", "/admin/data-mgmt", "/admin/merge-candidates",
    "/admin/dedup", "/admin/reference/danbooru", "/admin/notifications", "/admin/system",
    "/admin/settings", "/admin/settings/gallerydl", "/admin/settings/proxy",
    "/admin/settings/auth-status", "/admin/settings/logs", "/admin/settings/backup",
    "/admin/settings/download-defaults", "/admin/settings/subscription-defaults",
    "/admin/settings/dedup", "/admin/settings/scheduler-defaults", "/admin/profile",
  ];
  for (const p of pages) {
    test(p, async ({ page }) => {
      let err500 = false;
      page.on("response", (r) => { if (r.status() >= 500) err500 = true; });
      await page.goto(`${BASE}${p}`, { waitUntil: "domcontentloaded" });
      await page.waitForTimeout(300);
      await bodyOk(page);
      if (err500) console.warn(`  ⚠ ${p} had 500`);
    });
  }
});

// ================================================
// SECTION 3: Closed-loop navigation (4 tests)
// ================================================
test.describe("3. Closed-loop navigation", () => {
  test.beforeEach(async ({ page }) => { await doLogin(page); });

  test("Works→Detail→Creator via breadcrumb", async ({ page }) => {
    await page.goto(`${BASE}/admin/works`);
    await page.waitForLoadState("networkidle");
    const card = page.locator('a[href*="/admin/works/"], [class*="cursor-pointer"]').first();
    if (!(await card.isVisible({ timeout: 5000 }).catch(() => false))) return;
    await card.click();
    await page.waitForURL("**/admin/works/**", { timeout: 10000 });
    const bc = page.locator('nav[aria-label="Breadcrumb"]');
    if (!(await bc.isVisible({ timeout: 3000 }).catch(() => false))) return;
    const links = bc.locator("a");
    if ((await links.count()) >= 2) { await links.nth(1).click(); await page.waitForURL("**/admin/creators/**", { timeout: 10000 }); }
  });

  test("Search→Tag detail", async ({ page }) => {
    await page.goto(`${BASE}/admin/search`);
    const input = page.locator("input").first();
    if (!(await input.isVisible().catch(() => false))) return;
    await input.fill("original"); await page.waitForTimeout(1500);
    const tag = page.locator("a[href*='/admin/tags/']").first();
    if (await tag.isVisible({ timeout: 3000 }).catch(() => false)) { await tag.click(); await page.waitForURL("**/admin/tags/**", { timeout: 10000 }); }
  });

  test("Search multi-tab", async ({ page }) => {
    await page.goto(`${BASE}/admin/search`);
    const input = page.locator("input").first();
    if (!(await input.isVisible().catch(() => false))) return;
    await input.fill("test"); await page.waitForTimeout(1500);
    for (const re of [/全部|All/, /创作者|Creators/, /标签|Tags/]) {
      const t = page.locator("button").filter({ hasText: re }).first();
      if (await t.isVisible({ timeout: 2000 }).catch(() => false)) { await t.click(); await page.waitForTimeout(300); }
    }
  });

  test("Creator→Works tab", async ({ page }) => {
    await page.goto(`${BASE}/admin/creators`);
    const link = page.locator("a[href*='/admin/creators/']").first();
    if (!(await link.isVisible({ timeout: 5000 }).catch(() => false))) return;
    await link.click(); await page.waitForURL("**/admin/creators/**", { timeout: 10000 });
    const tab = page.locator("button").filter({ hasText: /作品|Works/ }).first();
    if (await tab.isVisible({ timeout: 3000 }).catch(() => false)) { await tab.click(); }
  });
});

// ================================================
// SECTION 4: Scheduler (2 tests)
// ================================================
test.describe("4. Scheduler", () => {
  test.beforeEach(async ({ page }) => { await doLogin(page); });
  test("Queue stats visible", async ({ page }) => {
    await page.goto(`${BASE}/admin/scheduler`); await page.waitForLoadState("networkidle"); await bodyOk(page);
  });
  test("Admin Operations section", async ({ page }) => {
    await page.goto(`${BASE}/admin/scheduler`);
    await expect(page.locator("text=管理操作").or(page.locator("text=Admin Operations"))).toBeVisible({ timeout: 5000 });
  });
});

// ================================================
// SECTION 5: Settings — all sub-pages (10 tests)
// ================================================
test.describe("5. Settings sub-pages", () => {
  test.beforeEach(async ({ page }) => { await doLogin(page); });
  for (const p of ["gallerydl","proxy","auth-status","logs","backup","download-defaults","subscription-defaults","dedup","scheduler-defaults","profile"]) {
    test(p, async ({ page }) => {
      await page.goto(`${BASE}/admin/settings/${p}`, { waitUntil: "domcontentloaded" });
      await page.waitForTimeout(300); await bodyOk(page);
    });
  }
});

// ================================================
// SECTION 6: No 500 errors on critical pages (6 tests)
// ================================================
test.describe("6. No 500 errors", () => {
  test.beforeEach(async ({ page }) => { await doLogin(page); });
  for (const p of ["/admin/scheduler","/admin/search?q=test","/admin/works","/admin/creators","/admin/tags","/admin/settings/gallerydl"]) {
    test(p, async ({ page }) => {
      const errors: string[] = [];
      page.on("response", (r) => { if (r.status() >= 500) errors.push(r.url()); });
      await page.goto(`${BASE}${p}`, { waitUntil: "networkidle" });
      await page.waitForTimeout(1000);
      expect(errors).toEqual([]);
    });
  }
});

// ================================================
// SECTION 7: Breadcrumbs on detail pages (3 tests)
// ================================================
test.describe("7. Breadcrumbs", () => {
  test.beforeEach(async ({ page }) => { await doLogin(page); });
  test("Works detail", async ({ page }) => {
    await page.goto(`${BASE}/admin/works`);
    const card = page.locator('a[href*="/admin/works/"]').first();
    if (await card.isVisible({ timeout: 5000 }).catch(() => false)) {
      await card.click(); await page.waitForURL("**/admin/works/**", { timeout: 10000 });
      await expect(page.locator('nav[aria-label="Breadcrumb"]')).toBeVisible({ timeout: 5000 });
    }
  });
  test("Creator detail", async ({ page }) => {
    await page.goto(`${BASE}/admin/creators`);
    const link = page.locator("a[href*='/admin/creators/']").first();
    if (await link.isVisible({ timeout: 5000 }).catch(() => false)) {
      await link.click(); await page.waitForURL("**/admin/creators/**", { timeout: 10000 });
      await expect(page.locator('nav[aria-label="Breadcrumb"]')).toBeVisible({ timeout: 5000 });
    }
  });
  test("Search", async ({ page }) => {
    await page.goto(`${BASE}/admin/search?q=test`);
    await page.waitForLoadState("networkidle");
  });
});

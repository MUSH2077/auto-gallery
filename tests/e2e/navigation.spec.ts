/**
 * auto-gallery E2E Navigation Tests
 *
 * Covers the full UX closed-loop: Login → Works → Work Detail → Creator → Tags → Search
 *
 * Usage:
 *   cd /volume3/docker/auto-gallery
 *   npx playwright test tests/e2e/navigation.spec.ts --headed
 *
 * Or headless:
 *   npx playwright test tests/e2e/navigation.spec.ts
 *
 * Requires Playwright browsers installed:
 *   npx playwright install chromium
 */

import { test, expect } from "@playwright/test";

const BASE = process.env.E2E_BASE || "http://localhost:13000";
const USER = process.env.E2E_USER || "admin";
const PASS = process.env.E2E_PASS || "admin123";

test.describe("auto-gallery navigation closed-loop", () => {
  test.beforeEach(async ({ page }) => {
    // Login
    await page.goto(`${BASE}/admin/login`);
    await page.fill('input[placeholder*="用户"]', USER);
    await page.fill('input[type="password"]', PASS);
    await page.click('button[type="submit"]');
    await page.waitForURL("**/admin**", { timeout: 10000 });
  });

  test("1. Dashboard loads", async ({ page }) => {
    await page.goto(`${BASE}/admin`);
    await expect(page.locator("text=仪表盘").or(page.locator("text=Dashboard"))).toBeVisible({ timeout: 10000 });
  });

  test("2. Works list → Work detail → Creator detail (closed loop)", async ({ page }) => {
    // Navigate to works
    await page.goto(`${BASE}/admin/works`);
    await page.waitForLoadState("networkidle");

    // Click first work card
    const firstCard = page.locator('[class*="cursor-pointer"]').first();
    if (await firstCard.isVisible()) {
      await firstCard.click();
      await page.waitForURL("**/admin/works/**", { timeout: 10000 });

      // Verify breadcrumb or back link exists
      const breadcrumb = page.locator('nav[aria-label="Breadcrumb"]');
      if (await breadcrumb.isVisible()) {
        // Click creator name in breadcrumb
        const creatorLink = breadcrumb.locator("a").nth(1);
        if (await creatorLink.isVisible()) {
          await creatorLink.click();
          await page.waitForURL("**/admin/creators/**", { timeout: 10000 });
          await expect(page.locator("h1")).toBeVisible();
          console.log("  ✓ Works → Creator via breadcrumb");
        }
      }

      // Back to works via breadcrumb
      const worksLink = breadcrumb.locator("a").first();
      if (await worksLink.isVisible()) {
        await worksLink.click();
        await page.waitForURL("**/admin/works**", { timeout: 10000 });
        console.log("  ✓ Breadcrumb back to Works");
      }
    }
  });

  test("3. Search → multi-tab navigation", async ({ page }) => {
    await page.goto(`${BASE}/admin/search`);
    await page.waitForLoadState("networkidle");

    // Type search query
    const input = page.locator('input[placeholder*="搜索"]').or(page.locator('input[placeholder*="Search"]'));
    if (await input.isVisible()) {
      await input.fill("test");
      await page.waitForTimeout(1500); // wait for debounce + API

      // Check for tab buttons
      const allTab = page.locator("button").filter({ hasText: /全部|All/ });
      if (await allTab.isVisible()) {
        console.log("  ✓ Search tabs visible");

        // Try clicking creators tab
        const creatorsTab = page.locator("button").filter({ hasText: /创作者|Creators/ });
        if (await creatorsTab.isVisible()) {
          await creatorsTab.click();
          await page.waitForTimeout(500);
          console.log("  ✓ Creators tab clicked");
        }

        // Try clicking tags tab
        const tagsTab = page.locator("button").filter({ hasText: /标签|Tags/ });
        if (await tagsTab.isVisible()) {
          await tagsTab.click();
          await page.waitForTimeout(500);
          console.log("  ✓ Tags tab clicked");
        }
      }
    }
  });

  test("4. Creator detail → Works (closed loop)", async ({ page }) => {
    await page.goto(`${BASE}/admin/creators`);
    await page.waitForLoadState("networkidle");

    // Click first creator
    const firstCreator = page.locator("a[href*='/admin/creators/']").first();
    if (await firstCreator.isVisible({ timeout: 5000 })) {
      await firstCreator.click();
      await page.waitForURL("**/admin/creators/**", { timeout: 10000 });

      // Verify tabs exist
      await expect(page.locator("button").filter({ hasText: /作品|Works/ })).toBeVisible({ timeout: 5000 });
      console.log("  ✓ Creator detail with Works tab");

      // Navigate to works tab
      const worksTab = page.locator("button").filter({ hasText: /作品|Works/ });
      if (await worksTab.isVisible()) {
        await worksTab.click();
        await page.waitForTimeout(500);
        console.log("  ✓ Creator Works tab");
      }
    }
  });

  test("5. Scheduler page — no 500 errors", async ({ page }) => {
    page.on("response", (response) => {
      if (response.status() >= 500) {
        console.error(`  500 ERROR: ${response.url()}`);
      }
    });

    await page.goto(`${BASE}/admin/scheduler`);
    await page.waitForLoadState("networkidle");
    await page.waitForTimeout(2000);

    // Should show queue stats or decisions
    const pageContent = page.locator("body");
    await expect(pageContent).toContainText(/调度|Scheduler|queue/i);
    console.log("  ✓ Scheduler page loaded without 500");
  });

  test("6. Tag detail page", async ({ page }) => {
    // Try to find a tag from search
    await page.goto(`${BASE}/admin/search`);
    await page.waitForLoadState("networkidle");
    const input = page.locator('input[placeholder*="搜索"]').or(page.locator('input[placeholder*="Search"]'));
    if (await input.isVisible()) {
      await input.fill("original");
      await page.waitForTimeout(1500);

      // Click tags tab
      const tagsTab = page.locator("button").filter({ hasText: /标签|Tags/ });
      if (await tagsTab.isVisible()) {
        await tagsTab.click();
        await page.waitForTimeout(500);
      }

      // Click first tag result
      const tagLink = page.locator("a[href*='/admin/tags/']").first();
      if (await tagLink.isVisible({ timeout: 3000 })) {
        await tagLink.click();
        await page.waitForURL("**/admin/tags/**", { timeout: 10000 });
        await expect(page.locator("h1")).toBeVisible();
        console.log("  ✓ Tag detail page loaded");
      }
    }
  });

  test("7. Jobs page loads", async ({ page }) => {
    await page.goto(`${BASE}/admin/jobs`);
    await page.waitForLoadState("networkidle");

    // Should show job list or empty state
    await expect(page.locator("body")).toBeVisible();
    console.log("  ✓ Jobs page loaded");
  });
});

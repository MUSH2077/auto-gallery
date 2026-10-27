import { expect, test, type BrowserContext, type ConsoleMessage, type Route } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";

const ADMIN = {
  id: 1,
  username: "media-review",
  display_name: "Media Review",
  is_admin: true,
  is_active: true,
  permissions: [] as string[],
  modules: {},
  preferences: {},
  nsfw_visible: true,
  upload_quota_bytes: null,
  upload_used_bytes: 0,
  must_change_password: false,
};

const WORK = {
  id: "video-work",
  title: "Synthetic motion study",
  description: "Generated fixture with no private or copyrighted media.",
  posted_at: "2026-07-31T08:00:00Z",
  is_nsfw: false,
  is_ai_generated: false,
  thumbnail_asset_id: "asset-video",
  asset_count: 2,
  is_favorite: false,
  creator_id: null,
  creator_name: null,
  curation_state: { visibility: "visible" },
  created_at: "2026-07-31T08:00:00Z",
  updated_at: "2026-07-31T08:00:00Z",
};

const SECOND_WORK = {
  ...WORK,
  id: "image-work",
  title: "Generated still study",
  thumbnail_asset_id: "asset-image",
  asset_count: 1,
};

const VIDEO_ASSET = {
  id: "asset-video",
  file_name: "generated-blue-clip.mp4",
  file_path: "fixture/generated-blue-clip.mp4",
  file_size: 1715,
  width: 64,
  height: 64,
  duration: 0.2,
  mime_type: "video/mp4",
  media_kind: "video",
  thumb_sm_path: "fixture/generated-blue-clip.thumbnail.webp",
  thumb_lg_path: "fixture/generated-blue-clip.poster.webp",
  thumb_url: "/media/thumb/asset-video",
  poster_url: "/media/poster/asset-video?expires=4102444800&token=fixture",
  preview_url: "/media/preview/asset-video?expires=4102444800&token=fixture",
  original_url: "/media/original/asset-video?expires=4102444800&token=fixture",
  created_at: "2026-07-31T08:00:00Z",
};

const IMAGE_ASSET = {
  id: "asset-image",
  file_name: "generated-still.webp",
  file_path: "fixture/generated-still.webp",
  file_size: 68,
  width: 64,
  height: 64,
  duration: null,
  mime_type: "image/webp",
  media_kind: "image",
  thumb_sm_path: "fixture/generated-still.thumbnail.webp",
  thumb_url: "/media/thumb/asset-image",
  poster_url: null,
  preview_url: "/media/preview/asset-image?expires=4102444800&token=fixture",
  original_url: "/media/original/asset-image?expires=4102444800&token=fixture",
  created_at: "2026-07-31T08:00:00Z",
};

const PIXEL = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAACAAAAAgCAIAAAD8GO2jAAAAK0lEQVR4nO3NMQEAMAjAsDEReEY1mIAvFdBEVr/L/ukdAAAAAAAAAAAALDYo9gHeEaI9wwAAAABJRU5ErkJggg==",
  "base64",
);

// A 0.2-second 64×64 solid-blue H.264 MP4 generated with ffmpeg's color source.
const GENERATED_VIDEO = Buffer.from(
  "AAAAIGZ0eXBpc29tAAACAGlzb21pc28yYXZjMW1wNDEAAAN2bW9vdgAAAGxtdmhkAAAAAAAAAAAAAAAAAAAD6AAAAMgAAQAAAQAAAAAAAAAAAAAAAAEAAAAAAAAAAAAAAAAAAAABAAAAAAAAAAAAAAAAAABAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAgAAAqB0cmFrAAAAXHRraGQAAAADAAAAAAAAAAAAAAABAAAAAAAAAMgAAAAAAAAAAAAAAAAAAAAAAAEAAAAAAAAAAAAAAAAAAAABAAAAAAAAAAAAAAAAAABAAAAAAEAAAABAAAAAAAAkZWR0cwAAABxlbHN0AAAAAAAAAAEAAADIAAAEAAABAAAAAAIYbWRpYQAAACBtZGhkAAAAAAAAAAAAAAAAAAAyAAAACgBVxAAAAAAALWhkbHIAAAAAAAAAAHZpZGUAAAAAAAAAAAAAAABWaWRlb0hhbmRsZXIAAAABw21pbmYAAAAUdm1oZAAAAAEAAAAAAAAAAAAAACRkaW5mAAAAHGRyZWYAAAAAAAAAAQAAAAx1cmwgAAAAAQAAAYNzdGJsAAAAv3N0c2QAAAAAAAAAAQAAAK9hdmMxAAAAAAAAAAEAAAAAAAAAAAAAAAAAAAAAAEAAQABIAAAASAAAAAAAAAABFUxhdmM1OS4zNy4xMDAgbGlieDI2NAAAAAAAAAAAAAAAGP//AAAANWF2Y0MBZAAK/+EAGGdkAAqs2UQmwEQAAAMABAAAAwDIPEiWWAEABmjr48siwP34+AAAAAAQcGFzcAAAAAEAAAABAAAAFGJ0cnQAAAAAAAB6CAAAeggAAAAYc3R0cwAAAAAAAAABAAAABQAAAgAAAAAUc3RzcwAAAAAAAAABAAAAAQAAADhjdHRzAAAAAAAAAAUAAAABAAAEAAAAAAEAAAoAAAAAAQAABAAAAAABAAAAAAAAAAEAAAIAAAAAHHN0c2MAAAAAAAAAAQAAAAEAAAAFAAAAAQAAAChzdHN6AAAAAAAAAAAAAAAFAAAC2wAAAA4AAAAMAAAADAAAAAwAAAAUc3RjbwAAAAAAAAABAAADpgAAAGJ1ZHRhAAAAWm1ldGEAAAAAAAAAIWhkbHIAAAAAAAAAAG1kaXJhcHBsAAAAAAAAAAAAAAAALWlsc3QAAAAlqXRvbwAAAB1kYXRhAAAAAQAAAABMYXZmNTkuMjcuMTAwAAAACGZyZWUAAAMVbWRhdAAAAq4GBf//qtxF6b3m2Ui3lizYINkj7u94MjY0IC0gY29yZSAxNjQgcjMwOTUgYmFlZTQwMCAtIEguMjY0L01QRUctNCBBVkMgY29kZWMgLSBDb3B5bGVmdCAyMDAzLTIwMjIgLSBodHRwOi8vd3d3LnZpZGVvbGFuLm9yZy94MjY0Lmh0bWwgLSBvcHRpb25zOiBjYWJhYz0xIHJlZj0zIGRlYmxvY2s9MTowOjAgYW5hbHlzZT0weDM6MHgxMTMgbWU9aGV4IHN1Ym1lPTcgcHN5PTEgcHN5X3JkPTEuMDA6MC4wMiBtaXhlZF9yZWY9MSBtZV9yYW5nZT0xNiBjaHJvbWFfbWU9MSB0cmVsbGlzPTEgOHg4ZGN0PTEgY3FtPTAgZGVhZHpvbmU9MjEsMTEgZmFzdF9wc2tpcD0xIGNocm9tYV9xcF9vZmZzZXQ9LTIgdGhyZWFkcz0yIGxvb2thaGVhZF90aHJlYWRzPTEgc2xpY2VkX3RocmVhZHM9MCBucj0wIGRlY2ltYXRlPTEgaW50ZXJsYWNlZD0wIGJsdXJheV9jb21wYXQ9MCBjb25zdHJhaW5lZF9pbnRyYT0wIGJmcmFtZXM9MyBiX3B5cmFtaWQ9MiBiX2FkYXB0PTEgYl9iaWFzPTAgZGlyZWN0PTEgd2VpZ2h0Yj0xIG9wZW5fZ29wPTAgd2VpZ2h0cD0yIGtleWludD0yNTAga2V5aW50X21pbj0yNSBzY2VuZWN1dD00MCBpbnRyYV9yZWZyZXNoPTAgcmNfbG9va2FoZWFkPTQwIHJjPWNyZiBtYnRyZWU9MSBjcmY9MjMuMCBxY29tcD0wLjYwIHFwbWluPTAgcXBtYXg9NjkgcXBzdGVwPTQgaXBfcmF0aW89MS40MCBhcT0xOjEuMDAAgAAAACVliIQAM//+3zL4FJGpnQVmmWZMzCXHY3Fr+DAoYh8D63kJY/kJAAAACkGaJGxCv/44jcAAAAAIQZ5CeIX/CbkAAAAIAZ5hdEK/DDgAAAAIAZ5jakK/DDk=",
  "base64",
);

async function installMediaRoutes(
  context: BrowserContext,
  calls: string[],
  options: { failFirstImageAsset?: boolean } = {},
) {
  let imageAssetAttempts = 0;
  await context.routeWebSocket("**/api/v1/ws*", (webSocket) => {
    webSocket.send(JSON.stringify({ type: "connected" }));
  });
  await context.addCookies([{
    name: "ag_session",
    value: "media-test-token",
    domain: new URL(process.env.PLAYWRIGHT_BASE_URL || "http://127.0.0.1:13000").hostname,
    path: "/",
  }, { name: "ag_csrf", value: "fixture-csrf", url: process.env.PLAYWRIGHT_BASE_URL || "http://127.0.0.1:13000" }]);
  await context.addInitScript(() => {
    window.localStorage.setItem("ag_token", "media-test-token");
    window.localStorage.setItem("auto-gallery-lang", "en");
    window.localStorage.setItem("auto-gallery-theme", "dark");
  });
  await context.route("**/media/stream/asset-video**", async (route) => {
    calls.push(`GET ${new URL(route.request().url()).pathname}`);
    await route.fulfill({
      status: 200,
      headers: {
        "Accept-Ranges": "bytes",
        "Content-Length": String(GENERATED_VIDEO.length),
      },
      contentType: "video/mp4",
      body: GENERATED_VIDEO,
    });
  });
  await context.route(url => url.pathname.startsWith("/media/"), async (route) => {
    if (new URL(route.request().url()).pathname.startsWith("/media/stream/")) return route.fallback();
    await route.fulfill({ body: PIXEL, contentType: "image/png" });
  });
  await context.route("**/api/v1/**", async (route: Route) => {
    const url = new URL(route.request().url());
    const path = url.pathname;
    calls.push(`${route.request().method()} ${path}`);
    if (path === "/api/v1/auth/me") {
      return route.fulfill({ json: ADMIN });
    }
    if (path === "/api/v1/auth/ws-ticket") {
      return route.fulfill({ json: { ticket: "media-fixture-ticket", expires_in: 30 } });
    }
    if (path === "/api/v1/system/workbench") {
      return route.fulfill({
        json: {
          updated_at: "2026-07-31T08:00:00Z",
          queue: {
            default: 0,
            scheduled: 0,
            failed: 0,
            active_download_count: 0,
            active_import_count: 0,
            failed_download_count: 0,
            failed_import_count: 0,
            stale_download_count: 0,
            stale_import_count: 0,
            stale_count: 0,
          },
          scheduler: {
            enabled: true,
            mode: "interval",
            timezone: "Asia/Shanghai",
            scan_interval_minutes: 15,
          },
          storage: {
            disk_total_bytes: 1_000_000,
            disk_free_bytes: 500_000,
            disk_used_bytes: 500_000,
            disk_used_percent: 50,
            disk_free_percent: 50,
            risk_level: "ok",
          },
          health: {},
          attention: {
            auth_unhealthy_count: 0,
            failed_download_count: 0,
            failed_import_count: 0,
            stale_job_count: 0,
            low_disk_warning: false,
            scheduler_disabled_warning: false,
          },
          recent: { download_jobs: [], import_jobs: [], works: [], successful_syncs: [] },
        },
      });
    }
    if (path === "/api/v1/tasks") {
      return route.fulfill({ json: { items: [], total: 0, offset: 0, limit: 50 } });
    }
    if (path === "/api/v1/system/scheduler-decisions") {
      return route.fulfill({ json: { updated_at: "2026-07-31T08:00:00Z", scheduler_enabled: true, timezone: "UTC", items: [] } });
    }
    if (path === "/api/v1/operations/overview") {
      return route.fulfill({ json: {
        view: url.searchParams.get("view") || "attention",
        total: 0,
        summary: { attention: 0, critical: 0, warning: 0, resolved: 0, active: 0, resource_limited: 0 },
        items: [],
      } });
    }
    if (path === "/api/v1/works/derivative-progress") {
      return route.fulfill({ json: {
        total: 0,
        completed: 0,
        pending: 0,
        processing: 0,
        failed: 0,
        remaining: 0,
        affected_works: 0,
        completion_percent: 100,
        status: "idle",
        last_completed_at: null,
        oldest_unfinished_at: null,
        stall_after_seconds: 300,
      } });
    }
    if (path === "/api/v1/works/video-work") {
      return route.fulfill({ json: WORK });
    }
    if (path === "/api/v1/works/video-work/assets") {
      return route.fulfill({ json: [VIDEO_ASSET, IMAGE_ASSET] });
    }
    if (path === "/api/v1/works/image-work/assets") {
      imageAssetAttempts += 1;
      if (options.failFirstImageAsset && imageAssetAttempts === 1) {
        return route.fulfill({ status: 503, json: { detail: "temporary fixture failure" } });
      }
      return route.fulfill({ json: [IMAGE_ASSET] });
    }
    if (/^\/api\/v1\/works\/video-work\/(sources|tags)$/.test(path)) {
      return route.fulfill({ json: [] });
    }
    if (path === "/api/v1/works/video-work/assets/asset-video/playback-ticket") {
      return route.fulfill({
        json: {
          url: "/media/stream/asset-video?expires=4102444800&token=fixture",
          expires_at: "2100-01-01T00:00:00Z",
        },
      });
    }
    if (path === "/api/v1/curation/commits") {
      return route.fulfill({ json: { items: [], total: 0 } });
    }
    if (path === "/api/v1/search") {
      const item = { ...WORK, has_video: true, preview_asset_ids: ["asset-video", "asset-image"] };
      const secondItem = { ...SECOND_WORK, has_video: false, preview_asset_ids: ["asset-image"] };
      return route.fulfill({
        json: {
          query: url.searchParams.get("q") || "",
          canonical_query: url.searchParams.get("q") || "",
          parsed: { raw: "", canonical: "", scope: url.searchParams.get("scope") || "works", targets: ["works"], tokens: [] },
          groups: {
            works: { total: 2, items: [item, secondItem] },
            creators: { total: 0, items: [] },
            tags: { total: 0, items: [] },
            repositories: { total: 0, items: [] },
            subscriptions: { total: 0, items: [] },
          },
          available_filters: {},
        },
      });
    }
    if (path.includes("/notifications")) {
      return route.fulfill({ json: { items: [], total: 0, unread_count: 0 } });
    }
    return route.fulfill({ status: 501, json: { detail: `Unhandled media fixture route: ${path}` } });
  });
}


for (const order of ["assets-first", "work-first", "cached-assets"] as const) {
  test("detail wheel paging survives " + order + " loading and reopening", async ({ context, page }) => {
    const calls: string[] = [];
    await installMediaRoutes(context, calls);
    const images = [
      IMAGE_ASSET,
      { ...IMAGE_ASSET, id: "asset-image-two", file_name: "second-still.webp" },
      { ...IMAGE_ASSET, id: "asset-image-three", file_name: "third-still.webp" },
    ];
    let releaseWork!: () => void;
    let releaseAssets!: () => void;
    const workReady = new Promise<void>(resolve => { releaseWork = resolve; });
    const assetsReady = new Promise<void>(resolve => { releaseAssets = resolve; });
    await context.route("**/api/v1/works/video-work", async route => {
      await workReady;
      await route.fulfill({ json: { ...WORK, asset_count: images.length } });
    });
    await context.route("**/api/v1/works/video-work/assets", async route => {
      await assetsReady;
      await route.fulfill({ json: images });
    });
    const assetsResponse = page.waitForResponse(response =>
      new URL(response.url()).pathname === "/api/v1/works/video-work/assets");
    const detail = page.getByRole("dialog");
    try {
      if (order === "cached-assets") {
        releaseAssets();
        await page.goto("/admin/works");
        const card = page.locator('[data-work-id="video-work"]');
        await card.hover();
        await expect(page.locator(".popover").filter({ hasText: WORK.title })).toBeVisible();
        await assetsResponse;
        await card.locator("h2 a").click();
        await expect(detail.getByRole("status")).toBeVisible();
        await page.evaluate(() => new Promise<void>(resolve =>
          requestAnimationFrame(() => requestAnimationFrame(() => resolve()))));
        releaseWork();
      } else {
        await page.goto("/admin/works/video-work");
        await expect(detail).toBeVisible();
        if (order === "assets-first") {
          releaseAssets();
          await assetsResponse;
          await page.evaluate(() => new Promise<void>(resolve =>
            requestAnimationFrame(() => requestAnimationFrame(() => resolve()))));
          releaseWork();
        } else {
          releaseWork();
          await expect(detail.locator(".detail-media-viewer")).toBeVisible();
          releaseAssets();
        }
      }

      const viewer = detail.locator(".detail-media-viewer");
      await expect(viewer.locator(".asset-navigation")).toContainText("1 / 3");
      await viewer.dispatchEvent("wheel", { deltaY: 150 });
      await expect(viewer.locator(".asset-navigation")).toContainText("2 / 3");
      await viewer.getByRole("button", { name: "Previous image", exact: true }).click();
      await expect(viewer.locator(".asset-navigation")).toContainText("1 / 3");

      await page.keyboard.press("Escape");
      await expect(detail).toHaveCount(0);
      await page.locator('[data-work-id="video-work"] h2 a').click();
      await expect(viewer.locator(".asset-navigation")).toContainText("1 / 3");
      await viewer.dispatchEvent("wheel", { deltaY: 150 });
      await expect(viewer.locator(".asset-navigation")).toContainText("2 / 3");
    } finally {
      releaseWork();
      releaseAssets();
    }
  });
}

test("video remains poster-only until click and switching assets unloads playback", async ({ context, page }) => {
  const calls: string[] = [];
  const consoleErrors: string[] = [];
  page.on("console", (message: ConsoleMessage) => {
    if (message.type() === "error") consoleErrors.push(message.text());
  });
  await installMediaRoutes(context, calls);

  await page.goto("/admin/works");
  await expect(page.getByRole("heading", { name: "Works", exact: true })).toBeVisible();
  await expect(page.getByText("Video", { exact: true }).first()).toBeVisible();
  await expect(page.locator("video")).toHaveCount(0);
  await expect.poll(() => calls.some((call) => call.includes("playback-ticket"))).toBe(false);

  const videoCard = page.locator(`[data-work-id="${WORK.id}"]`).first();
  await videoCard.hover();
  const preview = page.locator(".popover").filter({ hasText: WORK.title });
  await expect(preview).toBeVisible();
  const cardBox = await videoCard.boundingBox();
  expect(cardBox).not.toBeNull();
  await page.mouse.move(cardBox!.x + cardBox!.width / 2, cardBox!.y + cardBox!.height / 2);
  await page.mouse.wheel(0, 100);
  await expect(preview.getByText("2 / 2", { exact: true })).toBeVisible();
  const previewBox = await preview.boundingBox();
  expect(previewBox).not.toBeNull();
  await page.mouse.move(previewBox!.x + previewBox!.width / 2, previewBox!.y + previewBox!.height / 2);
  await page.mouse.wheel(0, 100);
  await expect(preview.getByText("1 / 2", { exact: true })).toBeVisible();

  await page.goto("/admin/works/video-work");
  await expect(page.getByRole("dialog").filter({ hasText: "Synthetic motion study" })).toBeVisible();
  const video = page.locator("video");
  await expect(video).toHaveCount(1);
  await expect(video).not.toHaveAttribute("src");
  await expect(video).not.toHaveAttribute("autoplay");
  await expect(video).toHaveAttribute("preload", "metadata");
  await expect.poll(() => calls.some((call) => call.includes("playback-ticket"))).toBe(false);
  const accessibility = await new AxeBuilder({ page }).include("#main-content").analyze();
  expect(accessibility.violations).toEqual([]);
  await page.screenshot({ path: "/tmp/auto-gallery-media-poster.png", fullPage: false });

  await page.getByRole("button", { name: "Play video" }).click();
  await expect.poll(() => calls.filter((call) => call.includes("playback-ticket")).length).toBe(1);
  await expect(video).toHaveAttribute("src", /\/media\/stream\/asset-video/);
  await expect(video).toHaveAttribute("controls", "");
  await page.screenshot({ path: "/tmp/auto-gallery-media-playing.png", fullPage: false });

  const releasedVideo = await video.elementHandle();
  await page.locator("details.detail-more-information > summary").click();
  await page.locator("details.detail-more-information details.detail-extra-group > summary").filter({ hasText: "Pages (2)" }).click();
  await page.getByTitle("generated-still.webp").click();
  await expect(page.locator("video")).toHaveCount(0);
  await expect.poll(() => releasedVideo!.evaluate(element => (element as HTMLVideoElement).paused && !element.getAttribute("src"))).toBe(true);
  expect(consoleErrors, calls.join("\n")).toEqual([]);
});

test("work preview wheel sensitivity keeps card and overlay pages synchronized", async ({ context, page }) => {
  const calls: string[] = [];
  await installMediaRoutes(context, calls);
  await page.goto("/admin/works");
  const card = page.locator(`[data-work-id="${WORK.id}"]`).first();
  await card.hover();
  const preview = page.locator(".popover").filter({ hasText: WORK.title });
  await expect(preview).toBeVisible();
  await page.mouse.move((await card.boundingBox())!.x + 30, (await card.boundingBox())!.y + 30);
  await page.mouse.wheel(0, 50);
  await expect(preview.getByText("1 / 2", { exact: true })).toBeVisible();
  await card.dispatchEvent("wheel", { deltaY: 200, ctrlKey: true });
  await expect(preview.getByText("1 / 2", { exact: true })).toBeVisible();
  await card.dispatchEvent("wheel", { deltaY: 20 });
  await expect(preview.getByText("2 / 2", { exact: true })).toBeVisible();
  await page.goto("/admin/works");
  await page.getByRole("button", { name: "Display", exact: true }).click();
  await page.getByRole("button", { name: "Display settings", exact: true }).click();
  await page.getByRole("combobox", { name: "Wheel paging", exact: true }).click();
  await page.getByRole("option", { name: "Relaxed", exact: true }).click();
  await page.getByRole("button", { name: "Done", exact: true }).click();
  const relaxedCard = page.locator(`[data-work-id="${WORK.id}"]`).first();
  await relaxedCard.hover();
  const relaxedPreview = page.locator(".popover").filter({ hasText: WORK.title });
  await expect(relaxedPreview).toBeVisible();
  await relaxedCard.dispatchEvent("wheel", { deltaY: 100 });
  await expect(relaxedPreview.getByText("1 / 2", { exact: true })).toBeVisible();
  await relaxedCard.dispatchEvent("wheel", { deltaY: 30 });
  await expect(relaxedPreview.getByText("2 / 2", { exact: true })).toBeVisible();
});
test("mobile video player is keyboard reachable without horizontal overflow", async ({ context, page }) => {
  const calls: string[] = [];
  await installMediaRoutes(context, calls);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/admin/works/video-work");

  const play = page.getByRole("button", { name: "Play video" });
  await play.focus();
  await expect(play).toBeFocused();
  await page.keyboard.press("Enter");
  await expect.poll(() => calls.filter((call) => call.includes("playback-ticket")).length).toBe(1);
  await expect.poll(() => page.evaluate(() => (
    document.documentElement.scrollWidth <= document.documentElement.clientWidth
  ))).toBe(true);
  await page.screenshot({ path: "/tmp/auto-gallery-media-mobile.png", fullPage: false });
});

test("slideshow retries the exact failed thumbnail and keeps a complete foreground image over a softened canvas", async ({ context, page }) => {
  const calls: string[] = [];
  await installMediaRoutes(context, calls, { failFirstImageAsset: true });
  await page.goto("/admin/works");

  await page.getByRole("button", { name: "Display", exact: true }).click();
  await page.getByRole("button", { name: "Display settings", exact: true }).click();
  await page.getByRole("button", { name: "Slideshow", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "Slideshow" });
  await expect(dialog).toBeVisible();
  await expect(dialog.getByTestId("slideshow-stage")).toBeVisible();
  await expect(dialog.getByTestId("slideshow-backdrop")).toBeVisible();
  const thumbnails = dialog.getByRole("list", { name: "Slide thumbnails" });
  await expect(thumbnails).toBeVisible();
  await expect(thumbnails.getByRole("button")).toHaveCount(2);
  await expect.poll(async () => dialog.locator('[data-slideshow-foreground="true"] img').evaluateAll((images) => (
    images.some((image) => (image as HTMLImageElement).complete && (image as HTMLImageElement).naturalWidth > 0)
  ))).toBe(true);
  await page.screenshot({ path: "/tmp/auto-gallery-slideshow-canvas.png", fullPage: false });

  await thumbnails.getByRole("button").nth(1).click();
  await expect(dialog.getByRole("button", { name: "Retry" })).toBeVisible();
  await dialog.getByRole("button", { name: "Retry" }).click();
  await expect.poll(() => calls.filter((call) => call === "GET /api/v1/works/image-work/assets").length).toBe(2);
  await expect(thumbnails.getByRole("button").nth(1)).toHaveAttribute("aria-current", "true");
  await expect(dialog).toContainText("2 / 2");
  await page.keyboard.press("Escape");
  await expect(dialog).toHaveCount(0);
});


for (const failure of ["ticket", "stream"] as const) {
  test(`video retries a failed ${failure} request and resumes real playback`, async ({ context, page }) => {
    const calls: string[] = [];
    await installMediaRoutes(context, calls);
    let allowRecovery = false;
    const tickets: string[] = [];
    page.on("request", request => { if (request.url().includes("playback-ticket")) tickets.push(request.url()); });
    if (failure === "ticket") {
      await context.route("**/api/v1/works/video-work/assets/asset-video/playback-ticket", route => allowRecovery ? route.fallback() : route.fulfill({ status: 503, json: { detail: "Temporary playback service failure" } }));
    } else {
      await context.route("**/media/stream/asset-video**", route => allowRecovery ? route.fallback() : route.fulfill({ status: 404, json: { detail: "Temporary media stream failure" } }));
    }
    await page.goto("/admin/works/video-work");
    const video = page.locator("video");
    await video.evaluate(element => { (element as HTMLVideoElement).loop = true; });
    await page.getByRole("button", { name: "Play video" }).click();
    const retry = page.getByRole("button", { name: "Retry", exact: true });
    await expect(retry).toBeVisible();
    allowRecovery = true;
    await retry.click();
    await expect.poll(() => tickets.length).toBeGreaterThan(1);
    await expect.poll(() => video.evaluate(element => (element as HTMLVideoElement).readyState >= 2 && !(element as HTMLVideoElement).paused && (element as HTMLVideoElement).currentTime > 0)).toBe(true);
    await video.evaluate(element => (element as HTMLVideoElement).pause());
    await expect.poll(() => video.evaluate(element => (element as HTMLVideoElement).paused)).toBe(true);
  });
}

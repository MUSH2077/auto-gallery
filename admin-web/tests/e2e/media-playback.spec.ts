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

async function installMediaRoutes(context: BrowserContext, calls: string[]) {
  await context.addCookies([{
    name: "ag_token",
    value: "media-test-token",
    domain: new URL(process.env.PLAYWRIGHT_BASE_URL || "http://127.0.0.1:13000").hostname,
    path: "/",
  }]);
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
  await context.route("**/media/**", async (route) => {
    await route.fulfill({ body: PIXEL, contentType: "image/png" });
  });
  await context.route("**/api/v1/**", async (route: Route) => {
    const url = new URL(route.request().url());
    const path = url.pathname;
    calls.push(`${route.request().method()} ${path}`);
    if (path === "/api/v1/auth/me") {
      return route.fulfill({ json: ADMIN });
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
    if (path === "/api/v1/works/video-work") {
      return route.fulfill({ json: WORK });
    }
    if (path === "/api/v1/works/video-work/assets") {
      return route.fulfill({ json: [VIDEO_ASSET, IMAGE_ASSET] });
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
      return route.fulfill({
        json: {
          query: url.searchParams.get("q") || "",
          canonical_query: url.searchParams.get("q") || "",
          parsed: { raw: "", canonical: "", scope: url.searchParams.get("scope") || "works", targets: ["works"], tokens: [] },
          groups: {
            works: { total: 1, items: [item] },
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

test("video remains poster-only until click and switching assets unloads playback", async ({ context, page }) => {
  const calls: string[] = [];
  const consoleErrors: string[] = [];
  page.on("console", (message: ConsoleMessage) => {
    if (message.type() === "error") consoleErrors.push(message.text());
  });
  await installMediaRoutes(context, calls);

  await page.goto("/admin/works");
  await expect(page.getByRole("heading", { name: "Works" })).toBeVisible();
  await expect(page.getByText("Video", { exact: true }).first()).toBeVisible();
  await expect(page.locator("video")).toHaveCount(0);
  await expect.poll(() => calls.some((call) => call.includes("playback-ticket"))).toBe(false);

  await page.goto("/admin/works/video-work");
  await expect(page.getByRole("heading", { level: 1, name: "Synthetic motion study" })).toBeVisible();
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

  await page.getByTitle("generated-still.webp").click();
  await expect(page.locator("video")).toHaveCount(0);
  expect(consoleErrors, calls.join("\n")).toEqual([]);
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

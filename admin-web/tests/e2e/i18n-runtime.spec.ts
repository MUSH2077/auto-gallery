import { expect, test, type Page } from "@playwright/test";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";

// Render the real provider and both public import paths with React. Next's
// existing webpack/TypeScript dependencies compile this temporary fixture;
// no product route or alternative i18n implementation is added for testing.
let fixtureRoot: string;
let englishChunk: string;
const project = process.cwd();
const origin = "http://i18n.fixture";

test.beforeAll(async () => {
  fixtureRoot = await mkdtemp(path.join(tmpdir(), "auto-gallery-i18n-"));
  const sourceRoot = path.join(project, "src");
  const config = JSON.parse(await readFile(path.join(project, "tsconfig.json"), "utf8"));
  const alias = config.compilerOptions.paths["@/lib/i18n"]?.[0];
  await writeFile(path.join(fixtureRoot, "loader.cjs"), `
    const ts = require(${JSON.stringify(require.resolve("typescript"))});
    module.exports = source => ts.transpileModule(source, { compilerOptions: {
      module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2020,
      jsx: ts.JsxEmit.ReactJSX,
    }}).outputText;
  `);
  await writeFile(path.join(fixtureRoot, "entry.tsx"), `
    import { createRoot } from ${JSON.stringify(require.resolve("react-dom/client"))};
    import { useState } from ${JSON.stringify(require.resolve("react"))};
    import { I18nProvider, useT } from "@/lib/i18n";
    import { useI18n } from ${JSON.stringify(path.join(sourceRoot, "lib/i18n.tsx"))};
    function Probe() {
      const formal = useT();
      const { lang, t, setLang } = useI18n();
      const [tick, setTick] = useState(0);
      return <div>
        <button onClick={() => setLang("zh")}>Choose Chinese</button>
        <button onClick={() => setLang("en")}>Choose English</button>
        <button onClick={() => setTick(tick + 1)}>Render again</button>
        <span data-testid="tick">{tick}</span>
        <span data-testid="lang">{lang}</span>
        <span data-testid="formal">{formal("nav.dashboard")}</span>
        <span data-testid="relative">{t("nav.dashboard")}</span>
        <span data-testid="interpolation">{t("dashboard.file_count", { count: 3 })}</span>
        <span data-testid="fallback">{t("fixture.fallback", "中文回退 {count}", { count: 3 })}</span>
        <span data-testid="missing">{t("fixture.missing")}</span>
      </div>;
    }
    createRoot(document.getElementById("root")).render(<I18nProvider><Probe /></I18nProvider>);
  `);
  const bundled = require("next/dist/compiled/webpack/webpack");
  const stats = await new Promise<{ toJson: (options: object) => { chunks: Array<{ files: string[]; modules?: Array<{ name?: string }> }> } }>((resolve, reject) => {
    bundled.webpack({
      mode: "development", target: "web", context: project,
      entry: path.join(fixtureRoot, "entry.tsx"),
      output: { path: fixtureRoot, filename: "app.js", publicPath: `${origin}/` },
      resolve: { extensions: [".tsx", ".ts", ".js", ".json"], modules: [path.join(project, "node_modules")],
        alias: { ...(alias ? { "@/lib/i18n$": path.resolve(project, alias) } : {}), "@": sourceRoot } },
      module: { rules: [{ test: /\.tsx?$/, use: path.join(fixtureRoot, "loader.cjs") }] },
    }, (error: Error | null, result: { hasErrors: () => boolean; toString: () => string; toJson: (options: object) => never }) => {
      if (error || result?.hasErrors()) reject(error || new Error(result.toString()));
      else resolve(result);
    });
  });
  const chunk = stats.toJson({ all: false, chunks: true, chunkModules: true }).chunks.find(
    (entry) => entry.modules?.some((module) => module.name?.endsWith("/locales/en.json")),
  );
  if (!chunk) throw new Error("English must remain an independently loaded catalog");
  englishChunk = chunk.files.find((file) => file.endsWith(".js"))!;
});

test.afterAll(async () => {
  if (fixtureRoot) await rm(fixtureRoot, { recursive: true, force: true });
});

async function installFixture(page: Page, language: "en" | "zh") {
  const preferences: Array<{ lang: string }> = [];
  await page.context().addCookies([{ name: "ag_csrf", value: "fixture-csrf", url: origin }]);
  await page.addInitScript((initial) => {
    if (!localStorage.getItem("auto-gallery-lang")) localStorage.setItem("auto-gallery-lang", initial);
  }, language);
  await page.route(`${origin}/**`, async (route) => {
    const pathname = new URL(route.request().url()).pathname;
    if (pathname.startsWith("/api/")) {
      expect(pathname).toBe("/api/v1/auth/me/preferences");
      expect(route.request().method()).toBe("PUT");
      expect(route.request().headers()["x-csrf-token"]).toBe("fixture-csrf");
      preferences.push(route.request().postDataJSON().preferences);
      return route.fulfill({ json: {} });
    }
    if (pathname === "/") return route.fulfill({ contentType: "text/html; charset=utf-8", body: '<meta charset="utf-8"><div id="root"></div><script src="/app.js"></script>' });
    return route.fulfill({ contentType: "application/javascript; charset=utf-8", body: await readFile(path.join(fixtureRoot, path.basename(pathname)), "utf8") });
  });
  return preferences;
}

test("one provider serves both import paths, restores preferences and preserves translation behavior", async ({ page }) => {
  const missing: string[] = [];
  page.on("console", (event) => { if (event.text().includes("[i18n] Missing")) missing.push(event.text()); });
  const preferences = await installFixture(page, "en");
  await page.goto(origin);
  await expect(page.getByTestId("lang")).toHaveText("en");
  await expect(page.getByTestId("formal")).toHaveText("Dashboard");
  await expect(page.getByTestId("relative")).toHaveText("Dashboard");
  await expect(page.getByTestId("interpolation")).toHaveText("3 files");
  await expect(page.getByTestId("fallback")).toHaveText("fixture.fallback");
  await expect(page.getByTestId("missing")).toHaveText("fixture.missing");
  await page.getByRole("button", { name: "Render again" }).click();
  await expect(page.getByTestId("tick")).toHaveText("1");
  expect(missing.filter((message) => message.includes("en translation: fixture.missing"))).toHaveLength(1);
  await page.getByRole("button", { name: "Choose Chinese" }).click();
  await expect(page.getByTestId("relative")).toHaveText("仪表盘");
  await expect(page.getByTestId("fallback")).toHaveText("中文回退 3");
  await expect.poll(() => preferences.at(-1)?.lang).toBe("zh");
  await page.reload();
  await expect(page.getByTestId("lang")).toHaveText("zh");
  await expect(page.getByTestId("formal")).toHaveText("仪表盘");
});

test("a delayed earlier catalog cannot overwrite the latest language choice", async ({ page }) => {
  await installFixture(page, "zh");
  let release!: () => void;
  const held = new Promise<void>((resolve) => { release = resolve; });
  let requested = false;
  await page.route(`${origin}/${englishChunk}`, async (route) => {
    requested = true;
    await held;
    await route.fulfill({ contentType: "application/javascript; charset=utf-8", body: await readFile(path.join(fixtureRoot, englishChunk), "utf8") });
  });
  await page.goto(origin);
  await expect(page.getByTestId("relative")).toHaveText("仪表盘");
  try {
    await page.getByRole("button", { name: "Choose English" }).click();
    await expect.poll(() => requested).toBe(true);
    await page.getByRole("button", { name: "Choose Chinese" }).click();
    const response = page.waitForResponse(`${origin}/${englishChunk}`);
    release();
    await (await response).finished();
    await page.evaluate(() => new Promise<void>((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))));
    await expect(page.getByTestId("lang")).toHaveText("zh");
    await expect(page.getByTestId("relative")).toHaveText("仪表盘");
    expect(await page.evaluate(() => localStorage.getItem("auto-gallery-lang"))).toBe("zh");
  } finally { release(); }
});

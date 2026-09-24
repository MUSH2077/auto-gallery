import assert from "node:assert/strict";
import { mkdtemp, mkdir, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";

import {
  BUNDLE_BUDGETS,
  findLocaleChunkFiles,
  readRouteBundle,
  validateBundleBudgets,
} from "../scripts/check-bundle-budgets.mjs";

async function writeFixtureBuild() {
  const buildDir = await mkdtemp(path.join(tmpdir(), "auto-gallery-bundle-budget-"));
  const chunksDir = path.join(buildDir, "static", "chunks");
  const manifestDir = path.join(buildDir, "server", "app", "admin", "login");
  await mkdir(chunksDir, { recursive: true });
  await mkdir(manifestDir, { recursive: true });
  await writeFile(path.join(chunksDir, "shared.js"), "x".repeat(40));
  await writeFile(path.join(chunksDir, "login.js"), "x".repeat(25));
  const manifest = {
    entryJSFiles: {
      "[project]/src/app/layout": ["static/chunks/shared.js"],
      "[project]/src/app/admin/login/page": [
        "static/chunks/shared.js",
        "static/chunks/login.js",
      ],
    },
  };
  await writeFile(
    path.join(manifestDir, "page_client-reference-manifest.js"),
    `globalThis.__RSC_MANIFEST = globalThis.__RSC_MANIFEST || {};\n`
      + `globalThis.__RSC_MANIFEST["/admin/login/page"] = ${JSON.stringify(manifest)};\n`,
  );
  return buildDir;
}

test("route bundle accounting de-duplicates client chunks", async () => {
  const buildDir = await writeFixtureBuild();
  const result = await readRouteBundle(buildDir, "/admin/login");

  assert.equal(result.bytes, 65);
  assert.deepEqual(result.files.sort(), [
    "static/chunks/login.js",
    "static/chunks/shared.js",
  ]);
});

test("locale accounting finds the two always-loaded catalog chunks", async () => {
  const buildDir = await mkdtemp(path.join(tmpdir(), "auto-gallery-locale-budget-"));
  const chunksDir = path.join(buildDir, "static", "chunks");
  const catalogsDir = path.join(buildDir, "catalogs");
  await mkdir(chunksDir, { recursive: true });
  await mkdir(catalogsDir, { recursive: true });
  await writeFile(path.join(catalogsDir, "en.json"), JSON.stringify({ marker: "the unique and deliberately long English catalog sentinel" }));
  await writeFile(path.join(catalogsDir, "zh.json"), JSON.stringify({ marker: "这是一个刻意设置得足够长且唯一的中文目录标记文本" }));
  await writeFile(path.join(chunksDir, "en.js"), 'export default {marker:"the unique and deliberately long English catalog sentinel"};');
  await writeFile(path.join(chunksDir, "zh.js"), 'export default {marker:"这是一个刻意设置得足够长且唯一的中文目录标记文本"};');
  await writeFile(path.join(chunksDir, "route.js"), 'console.log("route");');

  assert.deepEqual(
    (await findLocaleChunkFiles(buildDir, catalogsDir)).sort(),
    ["static/chunks/en.js", "static/chunks/zh.js"],
  );
});

test("production route budgets cover login, shell, dashboard, works, and jobs", () => {
  assert.deepEqual(BUNDLE_BUDGETS, {
    "/admin/login": 250 * 1024,
    "/admin#shell": 350 * 1024,
    "/admin": 550 * 1024,
    "/admin/works": 550 * 1024,
    "/admin/jobs": 550 * 1024,
  });
});

test("budget validation reports every oversized surface", () => {
  const failures = validateBundleBudgets(
    { "/admin/login": { bytes: 251 * 1024 }, "/admin": { bytes: 551 * 1024 } },
    { "/admin/login": 250 * 1024, "/admin": 550 * 1024 },
  );

  assert.equal(failures.length, 2);
  assert.match(failures[0], /\/admin\/login/);
  assert.match(failures[1], /\/admin/);
});

import { fileURLToPath, pathToFileURL } from "node:url";
import { readFile, readdir, stat } from "node:fs/promises";
import path from "node:path";

const KIB = 1024;

export const BUNDLE_BUDGETS = Object.freeze({
  "/admin/login": 250 * KIB,
  "/admin#shell": 350 * KIB,
  "/admin": 550 * KIB,
  "/admin/works": 550 * KIB,
  "/admin/jobs": 550 * KIB,
});

async function resolveAppPath(buildDir, route) {
  try {
    const routes = JSON.parse(
      await readFile(path.join(buildDir, "app-path-routes-manifest.json"), "utf8"),
    );
    const matched = Object.entries(routes).find(([, pathname]) => pathname === route);
    if (matched) return matched[0];
  } catch (error) {
    if (error?.code !== "ENOENT") throw error;
  }
  return `${route}/page`;
}

async function manifestLocation(buildDir, route) {
  const appPath = await resolveAppPath(buildDir, route);
  return {
    appPath,
    file: path.join(
      buildDir,
      "server",
      "app",
      `${appPath.replace(/^\//, "")}_client-reference-manifest.js`,
    ),
  };
}

function parseManifest(source, appPath) {
  const marker = `globalThis.__RSC_MANIFEST[${JSON.stringify(appPath)}] = `;
  const start = source.indexOf(marker);
  if (start === -1) {
    throw new Error(`Client-reference manifest does not contain ${appPath}`);
  }
  const jsonStart = start + marker.length;
  const jsonEnd = source.indexOf(";", jsonStart);
  if (jsonEnd === -1) throw new Error(`Invalid client-reference manifest for ${appPath}`);
  return JSON.parse(source.slice(jsonStart, jsonEnd));
}

async function measureFiles(buildDir, files) {
  const uniqueFiles = [...new Set(files)].sort();
  const sizes = await Promise.all(
    uniqueFiles.map(async (file) => (await stat(path.join(buildDir, file))).size),
  );
  return {
    bytes: sizes.reduce((total, size) => total + size, 0),
    files: uniqueFiles,
  };
}

async function listJavaScriptFiles(directory, root = directory) {
  const entries = await readdir(directory, { withFileTypes: true });
  const nested = await Promise.all(entries.map(async (entry) => {
    const absolute = path.join(directory, entry.name);
    if (entry.isDirectory()) return listJavaScriptFiles(absolute, root);
    return entry.isFile() && entry.name.endsWith(".js")
      ? [path.relative(root, absolute)]
      : [];
  }));
  return nested.flat();
}

function catalogSentinels(catalog) {
  return Object.values(catalog)
    .filter((value) => typeof value === "string" && value.length >= 16 && !/[\r\n]/u.test(value))
    .sort((left, right) => right.length - left.length);
}

export async function findLocaleChunkFiles(
  buildDir,
  catalogsDir = path.resolve(
    path.dirname(fileURLToPath(import.meta.url)),
    "..",
    "src",
    "lib",
    "locales",
  ),
) {
  const chunksRoot = path.join(buildDir, "static", "chunks");
  const chunkFiles = await listJavaScriptFiles(chunksRoot);
  const chunkSources = new Map(await Promise.all(chunkFiles.map(async (file) => [
    file,
    await readFile(path.join(chunksRoot, file), "utf8"),
  ])));
  const localeChunks = new Set();

  for (const lang of ["zh", "en"]) {
    const catalog = JSON.parse(await readFile(path.join(catalogsDir, `${lang}.json`), "utf8"));
    let matched = null;
    for (const sentinel of catalogSentinels(catalog)) {
      const escaped = JSON.stringify(sentinel).slice(1, -1);
      const matches = [...chunkSources.entries()]
        .filter(([, source]) => source.includes(sentinel) || source.includes(escaped))
        .map(([file]) => file);
      if (matches.length === 1) {
        matched = matches[0];
        break;
      }
    }
    if (!matched) throw new Error(`Could not identify the ${lang} locale client chunk`);
    localeChunks.add(path.join("static", "chunks", matched));
  }
  return [...localeChunks];
}

async function includeLargestLocaleChunk(buildDir, measurement, localeFiles) {
  const sizes = await Promise.all(localeFiles.map(async (file) => ({
    file,
    bytes: (await stat(path.join(buildDir, file))).size,
  })));
  const largest = sizes.sort((left, right) => right.bytes - left.bytes)[0];
  return largest
    ? measureFiles(buildDir, [...measurement.files, largest.file])
    : measurement;
}

export async function readRouteBundle(buildDir, route) {
  const location = await manifestLocation(buildDir, route);
  const source = await readFile(location.file, "utf8");
  const manifest = parseManifest(source, location.appPath);
  return measureFiles(buildDir, Object.values(manifest.entryJSFiles ?? {}).flat());
}

export async function readAdminShellBundle(buildDir) {
  const route = "/admin";
  const location = await manifestLocation(buildDir, route);
  const source = await readFile(location.file, "utf8");
  const manifest = parseManifest(source, location.appPath);
  const layoutEntry = Object.entries(manifest.entryJSFiles ?? {}).find(([entry]) =>
    entry.endsWith("/src/app/admin/layout"),
  );
  if (!layoutEntry) throw new Error("Admin client-reference manifest has no admin layout entry");
  return measureFiles(buildDir, layoutEntry[1]);
}

export function validateBundleBudgets(measurements, budgets = BUNDLE_BUDGETS) {
  return Object.entries(budgets).flatMap(([surface, budget]) => {
    const measurement = measurements[surface];
    if (!measurement) return [`${surface}: measurement is missing`];
    if (measurement.bytes <= budget) return [];
    return [
      `${surface}: ${(measurement.bytes / KIB).toFixed(1)} KiB exceeds ${(budget / KIB).toFixed(0)} KiB`,
    ];
  });
}

export async function measureBudgetSurfaces(buildDir) {
  const localeFiles = await findLocaleChunkFiles(buildDir);
  const routeEntries = Object.keys(BUNDLE_BUDGETS).filter((route) => route !== "/admin#shell");
  const routeMeasurements = await Promise.all(
    routeEntries.map(async (route) => {
      const measurement = await readRouteBundle(buildDir, route);
      return [
        route,
        route === "/admin/login"
          ? measurement
          : await includeLargestLocaleChunk(buildDir, measurement, localeFiles),
      ];
    }),
  );
  return {
    ...Object.fromEntries(routeMeasurements),
    "/admin#shell": await includeLargestLocaleChunk(
      buildDir,
      await readAdminShellBundle(buildDir),
      localeFiles,
    ),
  };
}

async function main() {
  const buildDir = path.resolve(process.argv[2] ?? ".next");
  const measurements = await measureBudgetSurfaces(buildDir);
  for (const [surface, measurement] of Object.entries(measurements)) {
    console.log(`${surface.padEnd(16)} ${(measurement.bytes / KIB).toFixed(1)} KiB`);
  }
  const failures = validateBundleBudgets(measurements);
  if (failures.length) {
    console.error(`Bundle budget failed:\n${failures.map((failure) => `- ${failure}`).join("\n")}`);
    process.exitCode = 1;
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  await main();
}

import { readdir, readFile } from "node:fs/promises";
import path from "node:path";

const sourceRoot = path.resolve("src");
const registryPath = path.join(sourceRoot, "lib", "adminRoutes.ts");
const forbiddenLiteral =
  /(["'`])\/admin\/(?:reference\/danbooru|repositories(?:\/|["'`?])|curation(?:\/|["'`?])|dedup(?:\/|["'`?])|users(?:\/|["'`?]))/g;

async function sourceFiles(directory) {
  const entries = await readdir(directory, { withFileTypes: true });
  const nested = await Promise.all(
    entries.map(async (entry) => {
      const fullPath = path.join(directory, entry.name);
      if (entry.isDirectory()) return sourceFiles(fullPath);
      return /\.(?:ts|tsx)$/.test(entry.name) ? [fullPath] : [];
    }),
  );
  return nested.flat();
}

const violations = [];
for (const file of await sourceFiles(sourceRoot)) {
  if (file === registryPath) continue;
  const content = await readFile(file, "utf8");
  for (const match of content.matchAll(forbiddenLiteral)) {
    const line = content.slice(0, match.index).split("\n").length;
    violations.push(`${path.relative(process.cwd(), file)}:${line}: ${match[0].slice(1)}`);
  }
}

if (violations.length) {
  console.error("Legacy admin URL literals must only be declared in src/lib/adminRoutes.ts:");
  for (const violation of violations) console.error(`  ${violation}`);
  process.exit(1);
}

console.log("Admin route contract check passed.");

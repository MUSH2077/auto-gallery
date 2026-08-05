import { mkdtemp, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { spawnSync } from "node:child_process";

const temporaryRoot = await mkdtemp(path.join(tmpdir(), "auto-gallery-openapi-"));
const temporaryTypes = path.join(temporaryRoot, "types.generated.ts");
const committedTypes = path.resolve("src/lib/api/types.generated.ts");
const command = path.resolve("node_modules/.bin/openapi-typescript");

try {
  const result = spawnSync(command, ["../docs/api/openapi.json", "-o", temporaryTypes], {
    cwd: process.cwd(),
    encoding: "utf8",
  });
  if (result.status !== 0) {
    process.stderr.write(result.stderr || result.stdout);
    process.exit(result.status ?? 1);
  }

  const [expected, actual] = await Promise.all([
    readFile(temporaryTypes, "utf8"),
    readFile(committedTypes, "utf8"),
  ]);
  if (expected !== actual) {
    console.error("Generated OpenAPI types are stale. Run: npm run generate:api-types");
    process.exit(1);
  }
  console.log("Generated OpenAPI types match docs/api/openapi.json.");
} finally {
  await rm(temporaryRoot, { recursive: true, force: true });
}

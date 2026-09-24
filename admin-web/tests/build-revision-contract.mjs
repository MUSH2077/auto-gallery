import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const read = (path) => readFileSync(new URL(`../${path}`, import.meta.url), "utf8");

test("system health displays the immutable build revision", () => {
  const types = read("src/lib/api/types.ts");
  const page = read("src/app/admin/system/page.tsx");

  assert.match(types, /build_revision:\s*string/);
  assert.match(page, /health\.data\.build_revision/);
  assert.match(page, /system_health\.build_revision/);
});

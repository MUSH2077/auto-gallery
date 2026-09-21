import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import { extractCatalogs } from "../scripts/generate-i18n-catalog.mjs";

test("runtime locale catalogs preserve every source translation", async () => {
  const source = await readFile(new URL("../src/lib/i18n.tsx", import.meta.url), "utf8");
  const { zh, en } = extractCatalogs(source);

  assert.equal(zh["nav.dashboard"], "仪表盘");
  assert.equal(en["nav.dashboard"], "Dashboard");
  assert.deepEqual(Object.keys(en).sort(), Object.keys(zh).sort());
});

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const dialog = readFileSync(new URL("../src/components/DownloadConflictDialog.tsx", import.meta.url), "utf8");
const drawer = readFileSync(new URL("../src/components/JobDrawers.tsx", import.meta.url), "utf8");
const api = readFileSync(new URL("../src/lib/api/index.ts", import.meta.url), "utf8");

for (const token of ["canonical", "staged", "zoom", "overflow-auto", "rollbackDownloadConflictResolution"]) {
  assert.ok(dialog.includes(token), `conflict dialog must include ${token}`);
}
assert.ok(drawer.includes("<DownloadConflictDialog"));
assert.ok(api.includes("resolveDownloadConflicts"));
assert.ok(api.includes("downloadConflictMediaUrl"));

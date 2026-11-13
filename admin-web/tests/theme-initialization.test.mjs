import assert from "node:assert/strict";
import test from "node:test";
import vm from "node:vm";
import { THEME_BOOTSTRAP_SCRIPT } from "../src/lib/themePreference.ts";
function firstPaint(stored, systemDark, storageFails = false) {
  const classes = new Set(), style = {};
  vm.runInNewContext(THEME_BOOTSTRAP_SCRIPT, {
    localStorage: { getItem() { if (storageFails) throw new Error("storage unavailable"); return stored; } },
    window: { matchMedia: () => ({ matches: systemDark }) },
    document: { documentElement: { style, classList: { toggle(name, enabled) { if (enabled) classes.add(name); else classes.delete(name); } } } },
  });
  return { dark: classes.has("dark"), scheme: style.colorScheme };
}
test("first paint defaults to the approved light theme regardless of system theme", () => {
  assert.deepEqual(firstPaint(null, true), { dark: false, scheme: "light" });
  assert.deepEqual(firstPaint("obsolete", true), { dark: false, scheme: "light" });
  assert.deepEqual(firstPaint(null, true, true), { dark: false, scheme: "light" });
});
test("first paint respects explicit light, dark, and system selections", () => {
  assert.deepEqual(firstPaint("light", true), { dark: false, scheme: "light" });
  assert.deepEqual(firstPaint("dark", false), { dark: true, scheme: "dark" });
  assert.deepEqual(firstPaint("system", true), { dark: true, scheme: "dark" });
  assert.deepEqual(firstPaint("system", false), { dark: false, scheme: "light" });
});

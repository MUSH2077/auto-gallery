import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";

const source = fs.readFileSync(path.join(process.cwd(), "src/components/AppSidebar.tsx"), "utf8");

assert.match(source, /groups\.map\(\(group, groupIndex\)/,
  "permission-filtered navigation groups must remain intact");
assert.match(source, /border-t border-border\/70/,
  "group separators must remain visible");
assert.match(source, /first:border-t-0/,
  "the first-group separator exception must remain intact");
assert.match(source, /border-t border-border\/70 py-1 first:border-t-0 first:pt-3/,
  "expanded groups must use equal spacing above and below every separator");
assert.doesNotMatch(source, /pb-1 pt-3/,
  "expanded groups must not retain asymmetric divider spacing");
assert.doesNotMatch(source, /<h2[^>]*sidebar-group/,
  "sidebar group headings must not be rendered");
assert.match(source, /aria-label=\{t\(group\.labelKey\)\}/,
  "each untitled group must retain an accessible name");

console.log("Sidebar untitled-group contract passed.");

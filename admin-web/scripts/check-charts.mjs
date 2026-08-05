#!/usr/bin/env node

import fs from "node:fs";
import path from "node:path";
import process from "node:process";

const root = process.cwd();
const chartRoot = path.join(root, "src", "components", "charts");
const targetPages = [
  path.join(root, "src", "app", "admin", "creators", "[id]", "page.tsx"),
  path.join(root, "src", "app", "admin", "data-mgmt", "page.tsx"),
];
const issues = [];

function sourceFiles(directory) {
  return fs.readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const fullPath = path.join(directory, entry.name);
    if (entry.isDirectory()) return sourceFiles(fullPath);
    return /\.(ts|tsx)$/.test(entry.name) ? [fullPath] : [];
  });
}

function report(file, source, index, message) {
  const line = source.slice(0, index).split("\n").length;
  issues.push(`${path.relative(root, file)}:${line} ${message}`);
}

const sharedChecks = [
  [/(?:#[0-9a-f]{3,8})\b/gi, "raw hexadecimal chart color; use the shared chart theme"],
  [/\b(?:CHART_COLORS|SOURCE_COLORS)\b/g, "unmediated color map; use useChartTheme"],
  [/\bMath\.random\s*\(/g, "random chart geometry or color is forbidden"],
  [/\b(?:chart\.js|recharts|echarts|victory|nivo|vega)\b/gi, "unapproved chart dependency"],
  [/\boverflow-x-auto\b/g, "horizontal chart scrolling is forbidden; use a responsive encoding"],
  [/\bmin-w-\[[^\]]+\]/g, "data-driven chart minimum widths are forbidden"],
  [/\bmax-width\s*:\s*none\b/g, "unbounded chart width is forbidden"],
  [/\btable\s*=/g, "retired chart data-table prop; keep exact values in the chart interaction"],
];

for (const file of sourceFiles(chartRoot)) {
  const source = fs.readFileSync(file, "utf8");
  for (const [pattern, message] of sharedChecks) {
    pattern.lastIndex = 0;
    for (const match of source.matchAll(pattern)) report(file, source, match.index, message);
  }
}

const retiredContractChecks = [
  [/\bChartTableModel\b/g, "retired ChartTableModel contract"],
  [/["']charts\.view_data["']/g, "retired chart data-table translation"],
];

for (const file of sourceFiles(path.join(root, "src"))) {
  const source = fs.readFileSync(file, "utf8");
  for (const [pattern, message] of retiredContractChecks) {
    pattern.lastIndex = 0;
    for (const match of source.matchAll(pattern)) report(file, source, match.index, message);
  }
}

const localChartChecks = [
  [/\b(?:CHART_COLORS|SOURCE_COLORS|getSourceColor|HorizontalBarChart|MonthStrip|WorkGrid)\b/g, "local or legacy chart implementation"],
  [/<svg\b/g, "page-local SVG; move visual encoding into a shared chart primitive"],
  [/\bMath\.random\s*\(/g, "random chart geometry or color is forbidden"],
  [/\btable\s*=/g, "retired chart data-table prop"],
];

for (const file of targetPages) {
  const source = fs.readFileSync(file, "utf8");
  if (!source.includes("@/components/charts/ChartFrame")) {
    issues.push(`${path.relative(root, file)} must use the shared ChartFrame contract`);
  }
  for (const [pattern, message] of localChartChecks) {
    pattern.lastIndex = 0;
    for (const match of source.matchAll(pattern)) report(file, source, match.index, message);
  }
}

if (issues.length) {
  console.error(`chart contract validation failed with ${issues.length} issue(s):`);
  for (const issue of issues) console.error(`- ${issue}`);
  process.exit(1);
}

console.log("chart contract validation passed.");

#!/usr/bin/env node

import fs from "node:fs";
import path from "node:path";
import process from "node:process";

const root = process.cwd();
const sourceRoot = path.join(root, "src");
const backendRoot = path.resolve(root, "..", "backend", "app");
const issues = [];

function files(directory) {
  return fs.readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const full = path.join(directory, entry.name);
    if (entry.isDirectory()) return files(full);
    return /\.(ts|tsx)$/.test(entry.name) ? [full] : [];
  });
}

function report(file, source, index, message) {
  const prefix = source.slice(0, index);
  const line = prefix.split("\n").length;
  issues.push(`${path.relative(root, file)}:${line} ${message}`);
}

const forbiddenWorkParams = /\b(?:searchParams|sp)\.get\(\s*["'](?:source|creator|creator_id|tag|nsfw|fav|favorite|ai|sort|order|filter|search)["']\s*\)/g;
const forbiddenWorkLinks = /\/admin\/works\?[^"'`\s]*(?:source|creator|creator_id|tag|nsfw|fav|favorite|ai|sort|order)=/g;
const forbiddenJobLinks = /\/admin\/jobs\?[^"'`\s]*(?:status|kind|source|repo|sort|order|search)=/g;
const legacyListWorkFilter = /api\.listWorks\([\s\S]{0,300}\{[\s\S]{0,250}\b(?:search|creator_id|tag|source|is_nsfw|is_favorite|is_ai_generated|sort_by|sort_order|curation_visibility)\s*:/g;
const localSearchIncludes = /\.(?:filter|find)\([\s\S]{0,220}\.includes\(\s*(?:q|query|search|needle)(?:\.|\\b)/g;
const clientParser = /(?:split|match)\(\s*["'`]?:["'`]\s*\)|(?:QUALIFIER|TOKEN)_(?:RE|REGEX)/g;

for (const file of files(sourceRoot)) {
  const source = fs.readFileSync(file, "utf8");
  const relative = path.relative(sourceRoot, file);
  const checks = [
    [forbiddenWorkLinks, "legacy work-filter URL; encode the condition in q"],
    [forbiddenJobLinks, "legacy job-filter URL; encode the condition in q"],
    [legacyListWorkFilter, "legacy listWorks search filter; use api.search with scope=works"],
  ];
  if (relative === path.join("app", "admin", "works", "page.tsx")) {
    checks.push([forbiddenWorkParams, "legacy work URL parameter; only q/page/view are allowed"]);
  }
  if (
    relative.includes(path.join("app", "admin", "works"))
    || relative.includes(path.join("app", "admin", "creators"))
    || relative.includes(path.join("app", "admin", "subscriptions"))
    || relative.includes(path.join("app", "admin", "jobs"))
    || relative.includes(path.join("app", "admin", "scheduler"))
  ) {
    checks.push([localSearchIncludes, "local string filtering; execute the canonical server search instead"]);
    checks.push([clientParser, "client-side search grammar detected; parsing belongs to SearchLanguage"]);
  }
  for (const [pattern, message] of checks) {
    pattern.lastIndex = 0;
    for (const match of source.matchAll(pattern)) report(file, source, match.index, message);
  }
}

const backendSearchFiles = [
  path.join(backendRoot, "repositories", "creator.py"),
  path.join(backendRoot, "repositories", "subscription.py"),
  path.join(backendRoot, "repositories", "work.py"),
  path.join(backendRoot, "services", "creator.py"),
  path.join(backendRoot, "services", "subscription.py"),
  path.join(backendRoot, "api", "works.py"),
  path.join(backendRoot, "api", "creators.py"),
  path.join(backendRoot, "api", "subscriptions.py"),
];
const legacyBackendSearch = [
  [/Creator\.(?:name|display_name)\.ilike|Work\.title\.ilike/g, "repository-local text search; use SearchService"],
  [/async def list_(?:works|creators|subscriptions)\([\s\S]{0,500}\b(?:search|source|creator_id|tag|is_nsfw|is_favorite|is_ai_generated|sort_by|sort_order)\s*:/g, "legacy list API search parameters; accept q and delegate to SearchService"],
  [/async def list_(?:creators|subscriptions)\([\s\S]{0,500}\bsearch\s*:/g, "service-local search path; use SearchService"],
];
for (const file of backendSearchFiles) {
  if (!fs.existsSync(file)) continue;
  const source = fs.readFileSync(file, "utf8");
  for (const [pattern, message] of legacyBackendSearch) {
    pattern.lastIndex = 0;
    for (const match of source.matchAll(pattern)) report(file, source, match.index, message);
  }
}

if (issues.length) {
  console.error(`search contract validation failed with ${issues.length} issue(s):`);
  for (const issue of issues) console.error(`- ${issue}`);
  process.exit(1);
}

console.log("search contract validation passed.");

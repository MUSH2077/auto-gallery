import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";

const root = process.cwd();
const endpointPath = path.join(root, "src/lib/api/endpoints/remoteDiscovery.ts");
const typesPath = path.join(root, "src/lib/api/types.ts");
const apiIndexPath = path.join(root, "src/lib/api/index.ts");
const pagePath = path.join(root, "src/app/admin/discovery/RemoteDiscoveryPage.tsx");
const accountsPath = path.join(root, "src/app/admin/discovery/RemoteAccountPanel.tsx");
const candidatesPath = path.join(root, "src/app/admin/discovery/CandidateWorkbench.tsx");
const privateCachePath = path.join(root, "src/lib/remoteDiscoveryPrivateCache.ts");

assert.ok(fs.existsSync(endpointPath), "remote discovery must have a focused typed endpoint module");

const endpoint = fs.readFileSync(endpointPath, "utf8");
const types = fs.readFileSync(typesPath, "utf8");
const apiIndex = fs.readFileSync(apiIndexPath, "utf8");
const page = fs.readFileSync(pagePath, "utf8");
const accounts = fs.readFileSync(accountsPath, "utf8");
const candidates = fs.readFileSync(candidatesPath, "utf8");

for (const route of [
  "/api/v1/remote-accounts",
  "/test",
  "/collections",
  "/x/oauth/authorize",
  "/x/oauth/callback",
  "/api/v1/discovery/scans",
  "/api/v1/discovery/candidates",
  "/candidates/batch-actions",
  "/resolve",
]) {
  assert.ok(endpoint.includes(route), `typed endpoint module must include ${route}`);
}

assert.match(types, /interface RemoteAccountRead/);
assert.match(types, /interface DiscoveryCandidate/);
assert.match(types, /interface RemoteDiscoveryRollout/,
  "frontend types must consume backend-effective rollout capabilities");
assert.match(types, /type RemoteDiscoverySource\s*=\s*"pixiv"\s*\|\s*"x"\s*\|\s*"bilibili"/);
assert.match(endpoint, /immediate_sync:\s*input\.syncNow\s*\?\?\s*false/);
assert.doesNotMatch(apiIndex, /queryKeys[\s\S]{0,1200}(?:credentials|refresh_token|SESSDATA|cookie)/i,
  "query keys must not contain credential material");
assert.match(apiIndex, /all:\s*\(userId:\s*number\)\s*=>\s*\["remote-discovery-private",\s*userId/,
  "remote account keys must include the authenticated user ID");
assert.match(apiIndex, /candidates:\s*\(userId:\s*number,\s*filters\?/,
  "candidate keys must include the authenticated user ID");
assert.ok(fs.existsSync(privateCachePath), "private discovery cache must have an explicit cleanup boundary");
assert.doesNotMatch(page, /useSearchParams/, "OAuth callback secrets must not enter reactive search-param state");
assert.doesNotMatch(page, /oauthCallback\s*=\s*useMutation/, "OAuth callback secrets must not enter mutation variables");
assert.match(accounts, /remote_discovery_rollout\?\.manual_preview/,
  "account controls must fail closed on the backend-effective preview gate");
assert.match(accounts, /remote_discovery_rollout\?\.auto_import/,
  "account settings must disable automatic import independently");
assert.match(accounts, /auto_import_configured_paused/,
  "a stored auto-import preference behind a closed gate must be labeled as configured but paused");
assert.match(accounts, /auto_import_summary_paused/,
  "the account card must distinguish a configured policy from effective automatic import");
assert.match(candidates, /previewEnabledAccountIds/,
  "candidate import and conflict actions must honor provider preview rollout");

console.log("Remote discovery frontend API contract passed.");

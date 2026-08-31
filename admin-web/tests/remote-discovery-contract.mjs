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
const detailRoutePath = path.join(root, "src/app/admin/discovery/candidates/[id]/page.tsx");
const detailPagePath = path.join(root, "src/app/admin/discovery/candidates/[id]/RemoteCreatorDetailPage.tsx");
const remoteWorksPath = path.join(root, "src/app/admin/discovery/RemoteCreatorWorks.tsx");
const privateCachePath = path.join(root, "src/lib/remoteDiscoveryPrivateCache.ts");
const callbackBootstrapPath = path.join(root, "src/lib/xOAuthCallbackBootstrap.ts");
const layoutPath = path.join(root, "src/app/layout.tsx");
const nextConfigPath = path.join(root, "next.config.js");

assert.ok(fs.existsSync(endpointPath), "remote discovery must have a focused typed endpoint module");

const endpoint = fs.readFileSync(endpointPath, "utf8");
const types = fs.readFileSync(typesPath, "utf8");
const apiIndex = fs.readFileSync(apiIndexPath, "utf8");
const page = fs.readFileSync(pagePath, "utf8");
const accounts = fs.readFileSync(accountsPath, "utf8");
const candidates = fs.readFileSync(candidatesPath, "utf8");
const callbackBootstrap = fs.readFileSync(callbackBootstrapPath, "utf8");
const layout = fs.readFileSync(layoutPath, "utf8");
const nextConfig = fs.readFileSync(nextConfigPath, "utf8");

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
  "/remote-detail",
  "/remote-works",
  "/remote-work-imports",
]) {
  assert.ok(endpoint.includes(route), `typed endpoint module must include ${route}`);
}

assert.match(types, /interface RemoteAccountRead/);
assert.match(types, /interface DiscoveryCandidate/);
assert.match(types, /interface RemoteCreatorDetail/);
assert.match(types, /header_image_url\?:\s*string/);
assert.match(types, /social_counts:\s*Record<string, number>/);
assert.match(types, /public_profile:\s*RemoteCreatorPublicProfile/);
assert.match(types, /candidate:\s*DiscoveryCandidate/);
assert.match(types, /interface RemoteWorkPreview/);
assert.match(types, /work_token:\s*string/);
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
assert.match(apiIndex, /filters\?\.localMatch === undefined \? "all" : filters\.localMatch/,
  "matched and unmatched pages must have distinct private query-cache keys");
assert.ok(fs.existsSync(privateCachePath), "private discovery cache must have an explicit cleanup boundary");
assert.doesNotMatch(page, /useSearchParams/, "OAuth callback secrets must not enter reactive search-param state");
assert.doesNotMatch(page, /oauthCallback\s*=\s*useMutation/, "OAuth callback secrets must not enter mutation variables");
assert.match(endpoint, /completeXOAuth:[\s\S]{0,400}method:\s*"POST"/,
  "OAuth completion must send secrets in a POST body");
assert.match(endpoint, /completeXOAuth:[\s\S]{0,500}body:\s*JSON\.stringify\(\{\s*state,\s*code\s*\}\)/,
  "OAuth completion must serialize state and code only in the request body");
assert.doesNotMatch(endpoint, /oauth\/callback\?/, "OAuth completion secrets must never enter an API URL");
assert.doesNotMatch(page, /searchParams\.get\(["'](?:state|code)["']\)/,
  "OAuth callback secrets must not be read after the head bootstrap");
assert.match(callbackBootstrap, /window\.history\.replaceState\(null,\s*"",\s*"\/admin\/discovery"\)/,
  "the callback URL must be scrubbed synchronously before hydration");
assert.match(callbackBootstrap, /delete window\.__consumeAutoGalleryXOAuthCallback/,
  "the callback closure must be a one-shot consumer");
assert.doesNotMatch(callbackBootstrap, /MutationObserver/,
  "the head bootstrap must not remove Next RSC scripts before hydration consumes them");
assert.match(callbackBootstrap, /document\.querySelectorAll\("script"\)/,
  "the one-shot consumer must scrub any serialized callback markers after hydration");
assert.doesNotMatch(callbackBootstrap, /localStorage|sessionStorage/,
  "the callback bootstrap must never persist OAuth secrets in browser storage");
assert.match(layout, /<head>[\s\S]*x-oauth-callback-bootstrap[\s\S]*<\/head>/,
  "the callback scrubber must execute from the document head");
assert.match(nextConfig, /incomingRequests:[\s\S]{0,160}ignore:[\s\S]{0,160}admin\\\/discovery/,
  "the frontend application must suppress access logging for the external callback target");
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
assert.ok(fs.existsSync(detailRoutePath), "candidate details must have a dedicated route");
assert.ok(fs.existsSync(detailPagePath), "candidate details must have a focused page component");
assert.ok(fs.existsSync(remoteWorksPath), "remote work preview and import must be reusable");
const detailPage = fs.readFileSync(detailPagePath, "utf8");
const remoteWorks = fs.readFileSync(remoteWorksPath, "utf8");
assert.match(detailPage, /header_image_url/,
  "the detail page must render a signed Pixiv header image with a theme fallback");
assert.match(detailPage, /public_profile/,
  "the detail page must expose the provider-approved public profile fields");
assert.match(detailPage, /useState<RemoteWorkFeedType>\("illust"\)/);
assert.match(detailPage, /\["illust",\s*"manga"\]\s+as const/,
  "illustration and manga feeds must be selectable independently");
assert.match(remoteWorks, /x_restrict/,
  "the creator page must gate sensitive works before preview and import");
assert.match(remoteWorks, /preview_urls/,
  "the lightbox must support multi-page Pixiv works");
assert.match(detailPage, /fetchNextPage/,
  "work pagination must follow opaque server cursors");
assert.match(endpoint, /work_type/,
  "typed detail and work requests must send the selected Pixiv feed type");
assert.match(endpoint, /filters\.localMatch !== undefined[\s\S]{0,120}local_match/,
  "the typed client must send local-match filtering to the paginated backend");
assert.match(candidates, /localMatch:\s*local === "matched" \? true : local === "unmatched" \? false : undefined/,
  "the workbench must map only matched/unmatched to the server predicate");
assert.doesNotMatch(candidates, /candidates\.data\?\.items \|\| \[\]\)\.filter/,
  "the workbench must not filter only the current server page");

console.log("Remote discovery frontend API contract passed.");

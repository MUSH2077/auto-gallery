import fs from "node:fs";
import path from "node:path";

const root = path.resolve(import.meta.dirname, "..");
const sourceRoot = path.join(root, "src");
const renderer = path.join(sourceRoot, "components", "MediaAssetRenderer.tsx");
const targetFiles = [
  "app/admin/works/page.tsx",
  "app/admin/works/[id]/page.tsx",
  "app/admin/creators/[id]/page.tsx",
  "app/admin/tags/[id]/page.tsx",
  "app/admin/search/page.tsx",
  "app/admin/subscriptions/repositories/[id]/page.tsx",
  "app/admin/data-mgmt/curation/page.tsx",
  "components/dashboard/DashboardWorkbench.tsx",
  "components/SlideshowPlayer.tsx",
].map((relative) => path.join(sourceRoot, relative));

const failures = [];
const allSourceFiles = [];

function collect(directory) {
  for (const entry of fs.readdirSync(directory, { withFileTypes: true })) {
    const full = path.join(directory, entry.name);
    if (entry.isDirectory()) collect(full);
    else if (/\.(tsx|ts)$/.test(entry.name)) allSourceFiles.push(full);
  }
}

collect(sourceRoot);
for (const file of allSourceFiles) {
  const source = fs.readFileSync(file, "utf8");
  if (file !== renderer && /<video\b/.test(source)) {
    failures.push(`${path.relative(root, file)} renders <video> outside the shared media module`);
  }
}

const rendererSource = fs.readFileSync(renderer, "utf8");
if (/\bautoPlay\b|\bautoplay\b/i.test(rendererSource)) {
  failures.push("MediaAssetRenderer must not enable automatic playback");
}
if (!/preload="metadata"/.test(rendererSource) || !/playsInline/.test(rendererSource)) {
  failures.push("MediaAssetRenderer must retain metadata-only preload and inline playback");
}

for (const file of targetFiles) {
  const source = fs.readFileSync(file, "utf8");
  if (/<img[\s\S]{0,180}api\.mediaUrl\(/.test(source)) {
    failures.push(`${path.relative(root, file)} bypasses the shared media thumbnail renderer`);
  }
}

if (failures.length) {
  console.error(`check:media failed:\n${failures.map((failure) => `- ${failure}`).join("\n")}`);
  process.exit(1);
}
console.log(`check:media passed (${targetFiles.length} core surfaces)`);

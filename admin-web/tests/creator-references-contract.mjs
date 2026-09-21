import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";

const root = process.cwd();
const apiPath = path.join(root, "src/lib/api/endpoints/creatorReferences.ts");
const typesPath = path.join(root, "src/lib/api/types.ts");
const pagePath = path.join(root, "src/app/admin/creators/[id]/page.tsx");
const referencesPath = path.join(root, "src/app/admin/creators/[id]/CreatorReferences.tsx");

const api = fs.readFileSync(apiPath, "utf8");
const types = fs.readFileSync(typesPath, "utf8");
const page = fs.readFileSync(pagePath, "utf8");

assert.match(api, /\/api\/v1\/creators\/\$\{id\}\/references/);
assert.match(types, /interface CreatorReferences/);
assert.match(types, /pixiv:\s*PixivCreatorReference\[\]/);
assert.ok(fs.existsSync(referencesPath), "creator references must use a focused component");
const references = fs.readFileSync(referencesPath, "utf8");
assert.match(page, /<CreatorReferences/);
assert.match(references, /references\.pixiv/);
assert.match(references, /references\.danbooru/);
assert.match(references, /other_names/);
assert.match(references, /@\{identity\.username\}/);
assert.doesNotMatch(references, /createSourceCreator|importDanbooru|createCreatorLink/,
  "reference-name adoption must never write source mappings");

console.log("Creator reference frontend contract passed.");

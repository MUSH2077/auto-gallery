import { pathToFileURL } from "node:url";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import path from "node:path";
import ts from "typescript";

function propertyName(node) {
  if (ts.isIdentifier(node) || ts.isStringLiteralLike(node)) return node.text;
  throw new Error(`Unsupported translation key at ${node.pos}`);
}

function stringRecord(node) {
  if (!ts.isObjectLiteralExpression(node)) {
    throw new Error(`Expected translation object at ${node.pos}`);
  }
  const result = {};
  for (const property of node.properties) {
    if (!ts.isPropertyAssignment(property) || !ts.isStringLiteralLike(property.initializer)) {
      throw new Error(`Translations must be string property assignments at ${property.pos}`);
    }
    result[propertyName(property.name)] = property.initializer.text;
  }
  return result;
}

export function extractCatalogs(source) {
  const sourceFile = ts.createSourceFile(
    "i18n.tsx",
    source,
    ts.ScriptTarget.Latest,
    true,
    ts.ScriptKind.TSX,
  );
  let zhNode;
  let enNode;

  function visit(node) {
    if (ts.isVariableDeclaration(node) && ts.isIdentifier(node.name) && node.name.text === "zh") {
      zhNode = node.initializer;
    }
    if (
      ts.isCallExpression(node)
      && ts.isPropertyAccessExpression(node.expression)
      && ts.isIdentifier(node.expression.expression)
      && node.expression.expression.text === "Object"
      && node.expression.name.text === "assign"
      && node.arguments.length >= 2
      && ts.isIdentifier(node.arguments[0])
      && node.arguments[0].text === "result"
    ) {
      enNode = node.arguments[1];
    }
    ts.forEachChild(node, visit);
  }
  visit(sourceFile);

  if (!zhNode || !enNode) throw new Error("Could not locate both i18n catalogs");
  return { zh: stringRecord(zhNode), en: stringRecord(enNode) };
}

function serialized(catalog) {
  return `${JSON.stringify(catalog, null, 2)}\n`;
}

async function main() {
  const projectDir = path.resolve(path.dirname(new URL(import.meta.url).pathname), "..");
  const source = await readFile(path.join(projectDir, "src", "lib", "i18n.tsx"), "utf8");
  const catalogs = extractCatalogs(source);
  const outputDir = path.join(projectDir, "src", "lib", "locales");
  const checking = process.argv.includes("--check");
  await mkdir(outputDir, { recursive: true });

  for (const [lang, catalog] of Object.entries(catalogs)) {
    const output = path.join(outputDir, `${lang}.json`);
    const expected = serialized(catalog);
    if (checking) {
      const current = await readFile(output, "utf8").catch(() => "");
      if (current !== expected) throw new Error(`${lang}.json is stale; run npm run generate:i18n-catalog`);
    } else {
      await writeFile(output, expected);
    }
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  await main();
}

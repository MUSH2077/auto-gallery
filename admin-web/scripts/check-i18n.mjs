#!/usr/bin/env node

import fs from "node:fs";
import path from "node:path";
import process from "node:process";
import ts from "typescript";
import { extractCatalogs } from "./generate-i18n-catalog.mjs";

const projectRoot = process.cwd();
const sourceRoot = path.join(projectRoot, "src");
const dictionaryPath = path.join(sourceRoot, "lib", "i18n-catalogs.ts");
const dictionarySource = fs.readFileSync(dictionaryPath, "utf8");

function collectDictionaryKeys() {
  const catalogs = extractCatalogs(dictionarySource);
  return {
    zh: new Set(Object.keys(catalogs.zh)),
    en: new Set(Object.keys(catalogs.en)),
  };
}

function sourceFiles(directory) {
  return fs.readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const fullPath = path.join(directory, entry.name);
    if (entry.isDirectory()) return sourceFiles(fullPath);
    return /\.(ts|tsx)$/.test(entry.name) ? [fullPath] : [];
  });
}

const allowedVisibleLiteral = /^(?:auto-gallery|Danbooru|Pixiv|Twitter|Weibo|Iwara|GitHub|gallery-dl|Meilisearch|NSFW|AI|URL|JSON|YAML|HTTP|HTTPS|Redis|PostgreSQL|RQ|API|ID|SHA-256|ZIP|GB|MB|KB|ms|sec|DEBUG|INFO|WARNING|ERROR|CRITICAL|EN|中|中文|English|X|AG|ag|p|Token|ESC|Ctrl\/⌘ K|\.\.\.|—|\/)$/i;
const visibleAttributes = new Set(["placeholder", "title", "aria-label"]);
const cjk = /[\u3400-\u9fff]/u;
const englishWords = /[A-Za-z]{2,}/;
const issues = [];
const { zh, en } = collectDictionaryKeys();
const galleryCopyPath = path.join(sourceRoot, "features", "gallery-ui", "copy.tsx");
const galleryCopyModule = "@/features/gallery-ui/copy";
function moduleResolvesToGalleryCopy(filePath, moduleSpecifier) {
  if (moduleSpecifier === galleryCopyModule) return true;
  if (!moduleSpecifier.startsWith(".")) return false;
  const resolved = path.resolve(path.dirname(filePath), moduleSpecifier);
  return resolved === galleryCopyPath || `${resolved}.tsx` === galleryCopyPath;
}
function objectLiteralKeys(object) {
  const keys = new Set();
  for (const property of object.properties) {
    if (!ts.isPropertyAssignment(property)) { issues.push(`copy.tsx:${property.getStart()}: unsupported non-assignment entry in locale table`); continue; }
    const name = property.name;
    if (ts.isIdentifier(name) || ts.isStringLiteralLike(name)) keys.add(name.text);
    else issues.push(`copy.tsx:${property.getStart()}: locale table key must be a literal`);
  }
  return keys;
}
function collectGalleryCopyKeys() {
  const source = fs.readFileSync(galleryCopyPath, "utf8");
  const ast = ts.createSourceFile(galleryCopyPath, source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  const tables = new Map();
  ast.forEachChild((node) => {
    if (!ts.isVariableStatement(node)) return;
    for (const declaration of node.declarationList.declarations) {
      if (!ts.isIdentifier(declaration.name) || !ts.isObjectLiteralExpression(declaration.initializer)) continue;
      if (declaration.name.text === "zh" || declaration.name.text === "en") tables.set(declaration.name.text, objectLiteralKeys(declaration.initializer));
    }
  });
  if (!tables.has("zh") || !tables.has("en")) { issues.push("copy.tsx: locale tables must define both zh and en object literals"); return new Set(); }
  const zhKeys = tables.get("zh"), enKeys = tables.get("en");
  for (const key of [...zhKeys].sort()) if (!enKeys.has(key)) issues.push(`copy.tsx: missing English copy key "${key}"`);
  for (const key of [...enKeys].sort()) if (!zhKeys.has(key)) issues.push(`copy.tsx: missing Chinese copy key "${key}"`);
  return new Set([...zhKeys].filter((key) => enKeys.has(key)));
}
const galleryCopyKeys = collectGalleryCopyKeys();

for (const key of [...zh].sort()) {
  if (!en.has(key)) issues.push(`dictionary: missing English key "${key}"`);
}
for (const key of [...en].sort()) {
  if (!zh.has(key)) issues.push(`dictionary: missing Chinese key "${key}"`);
}

for (const filePath of sourceFiles(sourceRoot)) {
  if (filePath === dictionaryPath) continue;
  const source = fs.readFileSync(filePath, "utf8");
  const ast = ts.createSourceFile(
    filePath,
    source,
    ts.ScriptTarget.Latest,
    true,
    filePath.endsWith(".tsx") ? ts.ScriptKind.TSX : ts.ScriptKind.TS,
  );
  const relative = path.relative(projectRoot, filePath);

  const galleryCopyBindings = new Set();
  const useGalleryCopyBindings = new Set();
  const galleryCopyContextBindings = new Set();
  ast.statements.forEach((statement) => {
    if (!ts.isImportDeclaration(statement) || !ts.isStringLiteral(statement.moduleSpecifier)) return;
    if (!moduleResolvesToGalleryCopy(filePath, statement.moduleSpecifier.text)) return;
    const bindings = statement.importClause?.namedBindings;
    if (!bindings || !ts.isNamedImports(bindings)) return;
    for (const element of bindings.elements) {
      const imported = element.propertyName?.text || element.name.text;
      if (imported === "galleryCopy") galleryCopyBindings.add(element.name.text);
      if (imported === "useGalleryCopy") useGalleryCopyBindings.add(element.name.text);
    }
  });

  const report = (node, message) => {
    const { line, character } = ast.getLineAndCharacterOfPosition(node.getStart(ast));
    issues.push(`${relative}:${line + 1}:${character + 1} ${message}`);
  };

  const checkVisible = (node, raw) => {
    const value = raw.replace(/\s+/g, " ").trim();
    const withoutEntities = value.replace(/&[a-z]+;/gi, "").trim();
    if (
      !value
      || !withoutEntities
      || allowedVisibleLiteral.test(withoutEntities)
      || /^\{[a-z_]+\}$/i.test(value)
      || /^https?:\/\//i.test(value)
      || /^\/[A-Za-z0-9._/?=&-]+$/.test(value)
      || /^danbooru\s*#$/i.test(value)
    ) return;
    if (cjk.test(value)) report(node, `visible Chinese literal "${value}"`);
    else if (englishWords.test(value)) report(node, `visible English literal "${value}"`);
  };

  const scanVisibleExpression = (node) => {
    if (
      ts.isCallExpression(node)
      && ts.isIdentifier(node.expression)
      && node.expression.text === "t"
    ) return;
    if (ts.isStringLiteralLike(node)) {
      checkVisible(node, node.text);
      return;
    }
    if (ts.isObjectLiteralExpression(node)) {
      for (const property of node.properties) {
        if (
          ts.isPropertyAssignment(property)
          && /^(?:message|msg|title|description)$/.test(property.name.getText().replaceAll(/['"]/g, ""))
        ) {
          scanVisibleExpression(property.initializer);
        }
      }
      return;
    }
    if (ts.isTemplateHead(node) || ts.isTemplateMiddle(node) || ts.isTemplateTail(node)) {
      checkVisible(node, node.text);
      return;
    }
    ts.forEachChild(node, scanVisibleExpression);
  };

  const literalKeys = (node) => {
    if (ts.isStringLiteralLike(node)) return new Set([node.text]);
    if (ts.isParenthesizedExpression(node)) return literalKeys(node.expression);
    if (ts.isConditionalExpression(node)) {
      const whenTrue = literalKeys(node.whenTrue), whenFalse = literalKeys(node.whenFalse);
      if (!whenTrue || !whenFalse) return null;
      return new Set([...whenTrue, ...whenFalse]);
    }
    return null;
  };
  const checkGalleryCopyCall = (node, localName, directImport) => {
    const keyArgument = node.arguments[directImport ? 1 : 0];
    const keys = keyArgument && literalKeys(keyArgument);
    if (!keys) { report(node, `gallery copy call "${localName}" requires a literal key`); return; }
    for (const key of keys) if (!galleryCopyKeys.has(key)) report(keyArgument, `gallery copy key "${key}" is not present in both copy tables`);
  };
  const visit = (node) => {
    if (ts.isVariableDeclaration(node) && node.initializer && ts.isCallExpression(node.initializer) && ts.isIdentifier(node.initializer.expression) && useGalleryCopyBindings.has(node.initializer.expression.text)) {
      if (ts.isObjectBindingPattern(node.name)) {
        for (const element of node.name.elements) {
          if (!ts.isBindingElement(element)) continue;
          const property = element.propertyName || element.name;
          if (ts.isIdentifier(property) && property.text === "copy" && ts.isIdentifier(element.name)) galleryCopyContextBindings.add(element.name.text);
        }
      }
    }
    if (ts.isCallExpression(node) && ts.isIdentifier(node.expression)) {
      if (galleryCopyBindings.has(node.expression.text)) checkGalleryCopyCall(node, node.expression.text, true);
      if (galleryCopyContextBindings.has(node.expression.text)) checkGalleryCopyCall(node, node.expression.text, false);
    }
    if (
      ts.isCallExpression(node)
      && ts.isIdentifier(node.expression)
      && node.expression.text === "t"
      && node.arguments[0]
      && ts.isStringLiteralLike(node.arguments[0])
    ) {
      const key = node.arguments[0].text;
      if (!zh.has(key) || !en.has(key)) report(node, `translation key "${key}" is not present in both dictionaries`);
      if (node.arguments[1] && !ts.isObjectLiteralExpression(node.arguments[1])) {
        report(node, `translation fallback is forbidden for "${key}"`);
      }
    }
    if (ts.isCallExpression(node)) {
      const isVisibleSetter = ts.isIdentifier(node.expression)
        && /^(?:setResult|setMessage|setNotice|setFeedback)$/.test(node.expression.text);
      const isVisibleMethod = ts.isPropertyAccessExpression(node.expression)
        && (
          (ts.isIdentifier(node.expression.expression) && node.expression.expression.text === "toast")
          || (
            ts.isIdentifier(node.expression.expression)
            && node.expression.expression.text === "window"
            && /^(?:alert|confirm)$/.test(node.expression.name.text)
          )
        );
      if (isVisibleSetter || isVisibleMethod) {
        for (const argument of node.arguments) scanVisibleExpression(argument);
      }
    }
    if (ts.isJsxText(node)) checkVisible(node, node.text);
    if (
      ts.isJsxAttribute(node)
      && visibleAttributes.has(node.name.getText())
      && node.initializer
      && ts.isStringLiteral(node.initializer)
    ) {
      checkVisible(node.initializer, node.initializer.text);
    }
    if (
      ts.isJsxExpression(node)
      && node.expression
      && ts.isStringLiteralLike(node.expression)
    ) {
      checkVisible(node.expression, node.expression.text);
    }
    ts.forEachChild(node, visit);
  };
  visit(ast);
}

if (issues.length) {
  console.error(`i18n validation failed with ${issues.length} issue(s):`);
  for (const issue of issues) console.error(`- ${issue}`);
  process.exit(1);
}

console.log(`i18n validation passed (${zh.size} bilingual keys checked).`);

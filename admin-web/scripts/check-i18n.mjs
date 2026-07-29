#!/usr/bin/env node

import fs from "node:fs";
import path from "node:path";
import process from "node:process";
import ts from "typescript";

const projectRoot = process.cwd();
const sourceRoot = path.join(projectRoot, "src");
const dictionaryPath = path.join(sourceRoot, "lib", "i18n.tsx");
const dictionarySource = fs.readFileSync(dictionaryPath, "utf8");
const dictionaryAst = ts.createSourceFile(dictionaryPath, dictionarySource, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);

function propertyKey(node) {
  const name = node.name;
  if (!name) return null;
  if (ts.isStringLiteral(name) || ts.isIdentifier(name)) return name.text;
  return null;
}

function collectDictionaryKeys() {
  let zhObject;
  let enObject;
  const visit = (node) => {
    if (ts.isVariableDeclaration(node) && node.name.getText() === "zh" && node.initializer && ts.isObjectLiteralExpression(node.initializer)) {
      zhObject = node.initializer;
    }
    if (
      ts.isCallExpression(node)
      && ts.isPropertyAccessExpression(node.expression)
      && node.expression.expression.getText() === "Object"
      && node.expression.name.text === "assign"
      && node.arguments[1]
      && ts.isObjectLiteralExpression(node.arguments[1])
    ) {
      enObject = node.arguments[1];
    }
    ts.forEachChild(node, visit);
  };
  visit(dictionaryAst);
  if (!zhObject || !enObject) {
    throw new Error("Unable to locate the zh/en dictionaries in src/lib/i18n.tsx");
  }
  return {
    zh: new Set(zhObject.properties.map(propertyKey).filter(Boolean)),
    en: new Set(enObject.properties.map(propertyKey).filter(Boolean)),
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

  const visit = (node) => {
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

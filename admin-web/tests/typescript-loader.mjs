import { readFile } from "node:fs/promises";
import ts from "typescript";
import { access } from "node:fs/promises";

export async function resolve(specifier, context, nextResolve) {
  const candidate = specifier.startsWith("@/")
    ? new URL(`../src/${specifier.slice(2)}`, import.meta.url)
    : specifier.startsWith(".") && context.parentURL ? new URL(specifier, context.parentURL) : null;
  if (candidate) {
    for (const suffix of ["", ".ts", "/index.ts"]) {
      const url = new URL(candidate.href + suffix);
      if (!url.pathname.endsWith(".ts")) continue;
      try { await access(url); return { url: url.href, shortCircuit: true }; } catch {}
    }
  }
  return nextResolve(specifier, context);
}

// Node 20 cannot run the pure TypeScript rule modules directly. Keep the
// regression tests on the repository's pinned TypeScript compiler.
export async function load(url, context, nextLoad) {
  if (!url.endsWith(".ts")) return nextLoad(url, context);
  const source = await readFile(new URL(url), "utf8");
  const output = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
  }).outputText;
  return { format: "module", shortCircuit: true, source: output };
}

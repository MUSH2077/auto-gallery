import { readFile } from "node:fs/promises";
import ts from "typescript";

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

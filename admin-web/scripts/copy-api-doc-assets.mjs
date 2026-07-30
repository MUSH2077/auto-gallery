import { copyFile, mkdir } from "node:fs/promises";
import { createRequire } from "node:module";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const require = createRequire(import.meta.url);
const root = dirname(dirname(fileURLToPath(import.meta.url)));
const destination = join(root, "public", "api-docs");

await mkdir(destination, { recursive: true });
await Promise.all([
  copyFile(require.resolve("swagger-ui-dist/swagger-ui-bundle.js"), join(destination, "swagger-ui-bundle.js")),
  copyFile(require.resolve("swagger-ui-dist/swagger-ui.css"), join(destination, "swagger-ui.css")),
  copyFile(require.resolve("redoc/bundles/redoc.standalone.js"), join(destination, "redoc.standalone.js")),
]);

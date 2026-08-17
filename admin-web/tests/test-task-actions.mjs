import assert from "node:assert/strict";

import { canPauseDownload } from "../src/lib/task-actions.ts";

for (const status of ["enqueued", "downloading", "downloaded", "importing"]) {
  assert.equal(canPauseDownload(status), true, `${status} should expose Pause`);
}

for (const status of ["failed", "stale", "paused", "complete", "cancelled"]) {
  assert.equal(canPauseDownload(status), false, `${status} must not expose Pause`);
}

console.log("task action state assertions passed");

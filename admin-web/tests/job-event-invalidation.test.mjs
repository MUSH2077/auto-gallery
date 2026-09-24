import assert from "node:assert/strict";
import test from "node:test";

import { invalidateJobEventQueries } from "../src/lib/jobEventInvalidation.ts";

test("task status events invalidate every shared operational surface", async () => {
  const invalidated = [];
  const queryClient = {
    invalidateQueries: async ({ queryKey }) => {
      invalidated.push(queryKey);
    },
  };

  await invalidateJobEventQueries(queryClient);

  assert.deepEqual(invalidated, [
    ["system", "workbench"],
    ["download-jobs"],
    ["import-jobs"],
    ["tasks"],
    ["tasks", "operations", "attention"],
    ["admin-operation-task"],
    ["admin-operation-snapshot"],
    ["notifications"],
  ]);
});

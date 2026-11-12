import assert from "node:assert/strict";
import test from "node:test";
import { QueryClient, QueryObserver } from "@tanstack/react-query";
import { invalidateJobEventQueries } from "../src/lib/jobEventInvalidation.ts";

test("one status event refreshes all operational surfaces including task children once", async () => {
  const client = new QueryClient();
  const keys = [["system", "workbench"], ["download-jobs"], ["import-jobs"], ["tasks", "gallery"],
    ["tasks", "operations", "attention"], ["admin-operation-task"], ["admin-operation-snapshot"], ["notifications"]];
  const counts = new Map();
  const unsubscribe = keys.map(queryKey => new QueryObserver(client, { queryKey, initialData: 0, staleTime: Infinity,
    queryFn: async ({ signal }) => { const key=JSON.stringify(queryKey);counts.set(key,(counts.get(key)||0)+1);
      await new Promise(resolve=>setTimeout(resolve,10));return 1; },
  }).subscribe(() => {}));
  try {
    await invalidateJobEventQueries(client);
    for(const key of keys) assert.equal(counts.get(JSON.stringify(key)),1,JSON.stringify(key));
  } finally { unsubscribe.forEach(stop=>stop());client.clear(); }
});

import assert from "node:assert/strict";
import test from "node:test";

import { parseJobsRoute, updatedJobsHref } from "../src/lib/jobsRoute.ts";

test("Jobs deep links prefer task drawer and clamp invalid pages", () => {
  const params = new URLSearchParams("tab=imports&page=-3&task=task-1&job=download-1&import_job=import-1&q=status%3Afailed");
  const state = parseJobsRoute(params, 100);
  assert.equal(state.activeTab, "imports");
  assert.equal(state.page, 1);
  assert.equal(state.taskOffset, 0);
  assert.equal(state.selectedTaskId, "task-1");
  assert.equal(state.selectedJobId, null);
  assert.equal(state.search, "status:failed");
});

test("Jobs page offset and drawer precedence retain existing URL semantics", () => {
  const state = parseJobsRoute(new URLSearchParams("tab=downloads&page=4&import_job=import-1&job=download-1"), 100);
  assert.equal(state.taskOffset, 300);
  assert.equal(state.selectedJobId, "import-1");
  assert.equal(parseJobsRoute(new URLSearchParams("tab=unknown&page=NaN"), 100).activeTab, "all");
});

test("Jobs URL updates preserve unrelated filters and remove empty values", () => {
  const current = new URLSearchParams("q=kind%3Aimport&page=3&job=old");
  const href = updatedJobsHref("/admin/jobs", current, { task: "new", job: null, page: null });
  assert.equal(href, "/admin/jobs?q=kind%3Aimport&task=new");
  assert.equal(updatedJobsHref("/admin/jobs", new URLSearchParams(), { job: null }), "/admin/jobs");
});

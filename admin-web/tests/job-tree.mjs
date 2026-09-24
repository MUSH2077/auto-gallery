import assert from "node:assert/strict";
import test from "node:test";

import {
  aggregateTaskGroup, buildTaskTree, flattenTaskNode, taskRunProgress,
} from "../src/lib/jobTree.ts";

const task = (id, status, parent_task_id = null, extra = {}) => ({
  id, status, parent_task_id, progress_data: null, progress_stage: null,
  progress_current: null, progress_total: null, ...extra,
});

test("Jobs tree preserves input order and keeps missing parents as roots", () => {
  const rows = [
    task("parent", "running"),
    task("first", "complete", "parent"),
    task("orphan", "failed", "unknown"),
    task("second", "enqueued", "parent"),
  ];
  const roots = buildTaskTree(rows);
  assert.deepEqual(roots.map((node) => node.task.id), ["parent", "orphan"]);
  assert.deepEqual(roots[0].children.map((node) => node.task.id), ["first", "second"]);
  assert.deepEqual(flattenTaskNode(roots[0]).map((item) => item.id), ["parent", "first", "second"]);
});

test("Jobs aggregate keeps failed precedence and child progress", () => {
  const [root] = buildTaskTree([
    task("parent", "running"),
    task("done", "complete", "parent"),
    task("bad", "failed", "parent"),
    task("waiting", "enqueued", "parent"),
  ]);
  assert.deepEqual(aggregateTaskGroup(root), {
    status: "failed",
    progress: { stage: "failed", current: 1, total: 3, percent: (1 / 3) * 100, message: "1/3" },
    active: 1, failed: 1, complete: 1, total: 3,
  });
});

test("Single task uses its own progress and completion status", () => {
  const [root] = buildTaskTree([
    task("single", "complete", null, { progress_stage: "importing", progress_current: 4, progress_total: 5 }),
  ]);
  assert.deepEqual(taskRunProgress(root.task), { stage: "importing", current: 4, total: 5 });
  assert.deepEqual(aggregateTaskGroup(root), {
    status: "complete",
    progress: { stage: "importing", current: 4, total: 5 },
    active: 0, failed: 0, complete: 1, total: 1,
  });
});

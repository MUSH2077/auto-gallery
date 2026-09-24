/** Pure Jobs tree and aggregate rules shared by rendering and tests. */

import type { JobProgress, TaskRun } from "./api";

export function isAttentionStatus(status: string) {
  return ["enqueued", "running", "recovering", "downloading", "downloaded", "importing", "failed", "stale"].includes(status);
}

export type TaskNode = {
  task: TaskRun;
  children: TaskNode[];
};

export function taskRunProgress(task: TaskRun): JobProgress | null {
  const progress = task.progress_data as JobProgress | null;
  if (progress) return progress;
  if (!task.progress_stage) return null;
  return {
    stage: task.progress_stage,
    current: task.progress_current || undefined,
    total: task.progress_total || undefined,
  };
}

export function flattenTaskNode(node: TaskNode): TaskRun[] {
  return [node.task, ...node.children.flatMap(flattenTaskNode)];
}

export function buildTaskTree(tasks: TaskRun[]): TaskNode[] {
  const nodes = new Map<string, TaskNode>();
  for (const task of tasks) nodes.set(task.id, { task, children: [] });
  const roots: TaskNode[] = [];
  for (const task of tasks) {
    const node = nodes.get(task.id)!;
    const parent = task.parent_task_id ? nodes.get(task.parent_task_id) : null;
    if (parent && parent.task.id !== task.id) parent.children.push(node);
    else roots.push(node);
  }
  return roots;
}

export function aggregateTaskGroup(node: TaskNode): { status: string; progress: JobProgress | null; active: number; failed: number; complete: number; total: number } {
  const tasks = flattenTaskNode(node).filter((task) => task.id !== node.task.id);
  if (!tasks.length) {
    return {
      status: node.task.status,
      progress: taskRunProgress(node.task),
      active: 0,
      failed: 0,
      complete: node.task.status === "complete" ? 1 : 0,
      total: 1,
    };
  }
  const failed = tasks.filter((task) => ["failed", "stale", "cancelled"].includes(task.status)).length;
  const active = tasks.filter((task) => isAttentionStatus(task.status) && !["failed", "stale", "cancelled"].includes(task.status)).length;
  const complete = tasks.filter((task) => task.status === "complete").length;
  const total = tasks.length;
  const status = failed > 0 ? "failed" : active > 0 ? "running" : complete === total ? "complete" : node.task.status;
  return {
    status,
    progress: {
      stage: status,
      current: complete,
      total,
      percent: total ? (complete / total) * 100 : undefined,
      message: `${complete}/${total}`,
    },
    active,
    failed,
    complete,
    total,
  };
}


import type { TaskRun } from "./api";
import type { TFunction } from "./i18n";

export function resourceReasonLabel(t: TFunction, reason?: string | null) {
  if (!reason) return null;
  const [code, detail] = reason.split(":", 2);
  const key = `jobs.resource.reason.${code}`;
  const translated = t(key);
  if (translated === key) return reason;
  return detail ? `${translated} (${detail})` : translated;
}

export const terminalTaskStatuses = new Set(["complete", "failed", "cancelled", "stale"]);

export function taskDisplayStatus(task: TaskRun) {
  return ["enqueued", "running", "paused", "recovering"].includes(task.status)
    && task.resource_state === "waiting"
    && task.resource_reason
    ? "waiting"
    : task.status;
}

export const taskCreator = (task: TaskRun) => String(task.meta?.creator_name || task.source || "—");

export function taskReason(t: TFunction, task: TaskRun) {
  return (task.error_log || resourceReasonLabel(t, task.resource_reason) || "").split("\n")[0];
}

export function taskProgress(task: TaskRun) {
  const current = Number(task.progress_current || 0);
  const total = Number(task.progress_total || 0);
  const specified = task.progress_data?.percent;
  return {
    current,
    total,
    percent: typeof specified === "number" ? specified : total > 0 ? Math.min(100, current / total * 100) : 0,
  };
}

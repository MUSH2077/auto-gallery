import type { AdminOperationPollResponse, OperationProgress } from "./api/types";

export interface OperationJobState {
  jobId: string;
  taskId?: string;
  kind: string;
  title: string;
  startedAt: number;
  status: "running" | "completed" | "error";
  progress: OperationProgress | null;
  result: Record<string, unknown> | null;
  error?: string;
  meta?: Record<string, unknown>;
}

export type OperationStatus =
  | "enqueued" | "queued" | "running" | "recovering" | "paused"
  | "complete" | "completed" | "failed" | "stale" | "cancelled" | "unknown";

export const OPERATION_ACTIVE_STATUSES = new Set<OperationStatus>(["queued", "enqueued", "running", "recovering", "paused"]);
export const OPERATION_TERMINAL_STATUSES = new Set<OperationStatus>(["complete", "completed", "failed", "stale", "cancelled", "unknown"]);

export function normalizeOperationStatus(status: unknown): OperationStatus {
  if (typeof status !== "string") return "unknown";
  if (OPERATION_ACTIVE_STATUSES.has(status as OperationStatus)) return status as OperationStatus;
  if (OPERATION_TERMINAL_STATUSES.has(status as OperationStatus)) return status as OperationStatus;
  return "unknown";
}

export function isOperationActive(status: unknown): boolean {
  return OPERATION_ACTIVE_STATUSES.has(normalizeOperationStatus(status));
}

export function isOperationTerminal(status: unknown): boolean {
  return OPERATION_TERMINAL_STATUSES.has(normalizeOperationStatus(status));
}

export function isMissingOperation(error: unknown): boolean {
  return !!error && typeof error === "object" && "status" in error && (error.status === 404 || error.status === 410);
}

export const operationTaskKey = (id: string | null | undefined) => ["admin-operation-task", id] as const;

export function transitionOperation(current: OperationJobState, response: AdminOperationPollResponse): {
  state: OperationJobState; terminal: boolean; shouldRefresh: boolean;
} {
  if (current.status !== "running") return { state: current, terminal: false, shouldRefresh: false };
  const status = normalizeOperationStatus(response?.status);
  const taskId = typeof response?.task_id === "string" ? response.task_id : current.taskId;
  if (isOperationActive(status)) return { state: { ...current, taskId, progress: response.progress || current.progress }, terminal: false, shouldRefresh: false };
  if (status === "complete" || status === "completed") return { state: { ...current, taskId, progress: response.progress || current.progress, result: response.result || null, status: "completed" }, terminal: true, shouldRefresh: true };
  return { state: { ...current, taskId, progress: response.progress || current.progress, error: response.error || `Operation ${status}`, status: "error" }, terminal: true, shouldRefresh: false };
}

import { useCallback } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { api } from "./api";
import { useOperationMonitor, type OperationJobState } from "./operationMonitor";
import { isMissingOperation } from "./operationStatus";
import { refreshOperationResources } from "./operationRefresh";
import type { CommandTask } from "./taskCommands";
import type { AdminOperationPollResponse } from "./api/types";

// Keep the previous page's two-hour recovery window; the server retains
// completed batch results for two hours independently of this browser record.
export const BATCH_RECOVERY_MS = 2 * 60 * 60 * 1000;
export const BATCH_STORAGE_KEY = "danbooru_batch_job";
type BatchResponse = Awaited<ReturnType<typeof api.getBatchImportStatus>>;
export interface BatchJobState {
  jobId: string;
  importType: "pixiv" | "url";
  total: number;
  startedAt: number;
  progress: BatchResponse["progress"];
  result: BatchResponse["result"];
  status: "pending" | "running" | "completed" | "error";
}

export function restoreBatchRecord(record: Partial<OperationJobState> & { importType?: unknown; total?: unknown }): Partial<OperationJobState> {
  if (record.importType !== "pixiv" && record.importType !== "url") return {};
  return { ...record, kind: record.importType === "url" ? "admin-danbooru-url-batch-import" : "admin-danbooru-batch-import",
    title: "Batch import", meta: { importType: record.importType, total: typeof record.total === "number" ? record.total : 0 } };
}
function persistBatchRecord(state: OperationJobState) {
  return { jobId: state.jobId, taskId: state.taskId, importType: state.meta?.importType, total: state.meta?.total, startedAt: state.startedAt };
}
class MissingBatchResult extends Error {}
function terminalBatchError(error: unknown) { return isMissingOperation(error) || error instanceof MissingBatchResult; }

async function fetchBatchStatus(operation: OperationJobState) {
  const response = operation.taskId ? await api.getAdminOperationStatus(operation.taskId) : await api.getBatchImportStatus(operation.jobId);
  return adaptBatchStatus(response);
}

export function adaptBatchStatus(response: AdminOperationPollResponse | BatchResponse) {
  const completed = response.status === "completed" || response.status === "complete";
  const result = response.result;
  if (completed && (!result || ![result.imported, result.low_confidence, result.not_found, result.errors].every(Array.isArray))) {
    // Let the existing Query retry policy perform bounded confirmation; no
    // page timer or second result writer is needed for the legacy response.
    throw new MissingBatchResult("Batch result is no longer available");
  }
  // TaskRun starts with result_data={}; it is not a completed batch result.
  return { ...response, status: completed ? "complete" : response.status, result: completed ? result : null };
}

export function useBatchImportMonitor(enabled: boolean) {
  const client = useQueryClient();
  const refresh = useCallback((_operation: OperationJobState, result: OperationJobState["result"]) => {
    refreshOperationResources(client, "danbooru-import-all", undefined, result);
  }, [client]);
  const monitor = useOperationMonitor({ storageKey: BATCH_STORAGE_KEY, fetchStatus: fetchBatchStatus, refreshResources: refresh,
    maxAgeMs: BATCH_RECOVERY_MS, enabled, restoreRecord: restoreBatchRecord, persistRecord: persistBatchRecord, terminalError: terminalBatchError });
  const start = useCallback((jobId: string, importType: "pixiv" | "url", total: number, taskId?: string) => {
    monitor.start(jobId, importType === "url" ? "admin-danbooru-url-batch-import" : "admin-danbooru-batch-import", "Batch import", { importType, total }, taskId);
  }, [monitor.start]);
  const accept = useCallback((task: CommandTask, accepted: { task_id: string; status: string }) => {
    const type = task.operation_type === "admin-danbooru-url-batch-import" ? "url" : "pixiv";
    const inputs = type === "url" ? task.meta?.urls : task.meta?.pixiv_ids;
    start(accepted.task_id, type, Array.isArray(inputs) ? inputs.length : 0, accepted.task_id);
  }, [start]);
  const state = monitor.operation;
  const raw = state?.result;
  const result = raw && [raw.imported, raw.low_confidence, raw.not_found, raw.errors].every(Array.isArray) ? raw as BatchResponse["result"] : null;
  const rawProgress = state?.progress as (NonNullable<OperationJobState["progress"]> & { imported?: number; errors?: number }) | null;
  const progress = rawProgress && typeof rawProgress.current === "number" && typeof rawProgress.total === "number" && rawProgress.total > 0
    ? { current: rawProgress.current, total: rawProgress.total, imported: rawProgress.imported ?? 0, errors: rawProgress.errors ?? 0 }
    : null;
  const batchJob: BatchJobState | null = state ? {
    jobId: state.jobId, importType: state.meta?.importType === "url" ? "url" : "pixiv", total: Number(state.meta?.total || 0),
    startedAt: state.startedAt, progress: progress || null, result, status: state.status,
  } : null;
  return { batchJob, start, accept, clear: monitor.clear };
}

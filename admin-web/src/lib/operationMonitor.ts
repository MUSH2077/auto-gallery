import { pollInterval } from "./polling";
import { createContext, useCallback, useEffect, useRef, useState } from "react";
import { useQuery, useQueryClient, type QueryClient } from "@tanstack/react-query";
import type { AdminOperationPollResponse, AdminOperationSnapshotResponse } from "@/lib/api/types";
import type { CommandTask } from "./taskCommands";
import { isMissingOperation, isOperationActive, operationTaskKey, transitionOperation, type OperationJobState } from "./operationStatus";
export { isOperationActive, isOperationTerminal, normalizeOperationStatus, transitionOperation } from "./operationStatus";
export type { OperationStatus, OperationJobState } from "./operationStatus";

interface OperationMonitorOptions {
  storageKey: string;
  fetchStatus: (operation: OperationJobState) => Promise<AdminOperationPollResponse>;
  refreshResources: (operation: OperationJobState, result: OperationJobState["result"]) => void;
  maxAgeMs?: number;
  enabled?: boolean;
  restoreRecord?: (record: Partial<OperationJobState>) => Partial<OperationJobState>;
  persistRecord?: (state: OperationJobState) => unknown;
  terminalError?: (error: unknown) => boolean;
}

export const OperationMonitorContext = createContext<ReturnType<typeof useOperationMonitor> | null>(null);

export function acceptOperationQueries(client: QueryClient, jobId: string, kind: string, taskId?: string) {
  const queryKey = operationTaskKey(taskId ?? jobId);
  const wasActive = client.getQueryCache().find({ queryKey, exact: true })?.isActive();
  void client.cancelQueries({ queryKey, exact: true });
  client.setQueryData(queryKey, { task_id: taskId, job_id: jobId, operation_type: kind, status: "enqueued", result: null });
  if (taskId) {
    for (const query of client.getQueryCache().findAll({ queryKey: ["admin-operation-snapshot"] })) {
      const cached = query.state.data as AdminOperationSnapshotResponse | undefined;
      if (cached?.current?.task_id !== taskId && cached?.snapshot?.task_id !== taskId) continue;
      // An older latest-response can otherwise reinstate the failed attempt
      // when the user navigates back, even after the task query was cancelled.
      void client.cancelQueries({ queryKey: query.queryKey, exact: true });
      client.setQueryData(query.queryKey, { ...cached, current: { task_id: taskId, operation_type: kind, status: "enqueued" } });
    }
  }
  if (wasActive) void client.invalidateQueries({ queryKey, exact: true });
}

export function useOperationMonitor({ storageKey, fetchStatus, refreshResources, maxAgeMs = 2 * 60 * 60 * 1000, enabled = true, restoreRecord, persistRecord, terminalError = isMissingOperation }: OperationMonitorOptions) {
  const queryClient = useQueryClient();
  const [operation, setOperation] = useState<OperationJobState | null>(null);
  const [restored, setRestored] = useState(false);
  const revision = useRef(0);
  const [epoch, setEpoch] = useState(0);
  const handledTerminal = useRef<number | null>(null);
  const lastApplied = useRef<{ epoch: number; data: unknown; error: unknown } | null>(null);

  useEffect(() => {
    if (!enabled) return;
    try {
      const raw = sessionStorage.getItem(storageKey);
      if (!raw) return;
      const parsed = JSON.parse(raw) as Partial<OperationJobState>;
      const saved = restoreRecord ? restoreRecord(parsed) : parsed;
      if (typeof saved.jobId !== "string" || typeof saved.kind !== "string" || typeof saved.startedAt !== "number" || Date.now() - saved.startedAt >= maxAgeMs) {
        sessionStorage.removeItem(storageKey);
        return;
      }
      setEpoch(++revision.current);
      setOperation({
        jobId: saved.jobId, taskId: saved.taskId, kind: saved.kind, title: saved.title || saved.kind,
        startedAt: saved.startedAt, status: "running", progress: null, result: null, meta: saved.meta,
      });
    } catch {
      try { sessionStorage.removeItem(storageKey); } catch {}
    } finally { setRestored(true); }
  }, [enabled, maxAgeMs, storageKey, restoreRecord]);

  const start = useCallback((jobId: string, kind: string, title: string, meta?: Record<string, unknown>, taskId?: string) => {
    const next: OperationJobState = { jobId, taskId, kind, title, meta, startedAt: Date.now(), status: "running", progress: null, result: null };
    setEpoch(++revision.current);
    setOperation(next);
    handledTerminal.current = null;
    // Retries retain the TaskRun id: discard an in-flight old attempt before
    // seeding that same query with the newly accepted attempt.
    acceptOperationQueries(queryClient, jobId, kind, taskId);
    try { sessionStorage.setItem(storageKey, JSON.stringify(persistRecord ? persistRecord(next) : { jobId, taskId, kind, title, meta, startedAt: next.startedAt })); } catch {}
  }, [storageKey, queryClient, persistRecord]);

  const clear = useCallback(() => {
    setEpoch(++revision.current);
    setOperation(null);
    try { sessionStorage.removeItem(storageKey); } catch {}
  }, [storageKey]);

  const accept = useCallback((task: CommandTask, accepted: { task_id: string; status: string }) => {
    const previous = operation?.taskId === accepted.task_id || operation?.jobId === accepted.task_id ? operation : null;
    if (task.kind !== "admin" && !previous) return;
    const kind = task.operation_type || previous?.kind;
    if (!kind) return;
    // Generic retry promises a stable TaskRun identity, not a new RQ id.
    const meta = previous?.meta ?? (typeof task.meta?.entity === "string" ? { entity: task.meta.entity } : undefined);
    start(accepted.task_id, kind, task.title || previous?.title || kind, meta, accepted.task_id);
  }, [operation, start, queryClient]);

  const queryKey = operationTaskKey(operation?.taskId ?? operation?.jobId);
  const query = useQuery({
    queryKey,
    queryFn: () => fetchStatus(operation!),
    enabled: enabled && restored && !!operation && operation.status === "running",
    staleTime: 0,
    refetchInterval: (q) => operation ? operationRefetchInterval(q.state.data?.status, !!q.state.data) : false,
    refetchIntervalInBackground: false,
    refetchOnWindowFocus: false,
    retry: (count, error) => !isMissingOperation(error) && count < 2,
  });

  useEffect(() => {
    if (!enabled || !operation || operation.status !== "running" || revision.current !== epoch) return;
    const identity = operation.jobId;
    if (handledTerminal.current === epoch) return;
    if (lastApplied.current?.epoch === epoch && lastApplied.current.data === query.data && lastApplied.current.error === query.error) return;
    lastApplied.current = { epoch, data: query.data, error: query.error };
    if (query.error) {
      if (terminalError(query.error)) {
        handledTerminal.current = epoch;
        const error = isMissingOperation(query.error) ? "Operation no longer exists" : query.error instanceof Error ? query.error.message : "Operation unavailable";
        setOperation((current) => current?.jobId === identity ? { ...current, status: "error", error } : current);
        queryClient.setQueryData(queryKey, { ...query.data, task_id: operation.taskId, status: "unknown", error });
        try { if (JSON.parse(sessionStorage.getItem(storageKey) || "null")?.jobId === identity) sessionStorage.removeItem(storageKey); } catch {}
      }
      return;
    }
    if (!query.data) return;
    const data = query.data;
    const transition = transitionOperation(operation, data);
    if (transition.terminal) handledTerminal.current = epoch;
    setOperation((current) => current?.jobId === identity ? transition.state : current);
    if (transition.shouldRefresh) {
      refreshResources(transition.state, transition.state.result);
      try { if (revision.current === epoch && JSON.parse(sessionStorage.getItem(storageKey) || "null")?.jobId === identity) sessionStorage.removeItem(storageKey); } catch {}
      return;
    }
    if (transition.terminal) try { if (JSON.parse(sessionStorage.getItem(storageKey) || "null")?.jobId === identity) sessionStorage.removeItem(storageKey); } catch {}
  }, [enabled, epoch, operation?.jobId, operation?.status, query.data, query.error, refreshResources, storageKey]);

  return { operation, start, accept, clear, queryKey, isRestored: restored, error: query.error };
}

/** React Query interval policy shared by layout and page-level monitors. */
export function operationRefetchInterval(status: unknown, hasData = true): number | false {
  if (!hasData || isOperationActive(status)) return pollInterval(true);
  return false;
}

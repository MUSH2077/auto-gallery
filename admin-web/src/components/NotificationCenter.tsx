"use client";
import dynamic from "next/dynamic";
import { createContext, useContext, useCallback, useEffect, type ReactNode } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { usePermissions } from "@/lib/usePermissions";
import { useAuth } from "@/lib/auth";
import { OperationMonitorContext, useOperationMonitor, type OperationJobState } from "@/lib/operationMonitor";
import { refreshOperationResources } from "@/lib/operationRefresh";

import { useBatchImportMonitor, type BatchJobState } from "@/lib/batchImportMonitor";
export type { BatchJobState } from "@/lib/batchImportMonitor";

const OPERATION_STORAGE_KEY = "admin_operation_job";

interface NotificationCtx {
  clearRecent: () => void;
  // Batch job managed globally (polling runs at layout level)
  batchJob: BatchJobState | null;
  startBatchJob: (jobId: string, importType: "pixiv" | "url", total: number) => void;
  clearBatchJob: () => void;
  operationJob: OperationJobState | null;
  startOperationJob: (
    jobId: string,
    kind: OperationJobState["kind"],
    title: string,
    meta?: Record<string, any>,
    taskId?: string,
  ) => void;
  clearOperationJob: () => void;
}

const NotificationContext = createContext<NotificationCtx>({
  clearRecent: () => {},
  batchJob: null,
  startBatchJob: () => {},
  clearBatchJob: () => {},
  operationJob: null,
  startOperationJob: () => {},
  clearOperationJob: () => {},
});

export function NotificationProvider({ children }: { children: ReactNode }) {
  const qc = useQueryClient();
  const { isAuthenticated, isLoading: authLoading, user: authUser } = useAuth();
  const {
    has,
    isLoading: permissionsLoading,
    user: permissionsUser,
  } = usePermissions({ enabled: isAuthenticated });
  const canAccessBatchImports = isAuthenticated
    && !permissionsLoading
    && permissionsUser?.id === authUser?.id
    && has("subscriptions");
  const batchMonitor = useBatchImportMonitor(canAccessBatchImports);
  const { batchJob } = batchMonitor;
  const fetchOperationStatus = useCallback((operation: OperationJobState) => operation.kind === "danbooru-mapping-refresh"
    ? api.getDanbooruMappingRefreshStatus(operation.jobId)
    : api.getAdminOperationStatus(operation.taskId ?? operation.jobId), []);
  const refreshOperation = useCallback((operation: OperationJobState) => {
    refreshOperationResources(qc, operation.kind, operation.meta);
  }, [qc]);
  const monitor = useOperationMonitor({
    storageKey: OPERATION_STORAGE_KEY,
    fetchStatus: fetchOperationStatus,
    refreshResources: refreshOperation,
  });
  const operationJob = monitor.operation;
  useEffect(() => {
    if (!authLoading && !isAuthenticated) batchMonitor.clear();
  }, [authLoading, isAuthenticated, batchMonitor.clear]);

  const startBatchJob = batchMonitor.start;
  const clearBatchJob = batchMonitor.clear;

  const startOperationJob = useCallback((
    jobId: string,
    kind: OperationJobState["kind"],
    title: string,
    meta?: Record<string, any>,
    taskId?: string,
  ) => {
    monitor.start(jobId, kind, title, meta, taskId);
  }, [monitor.start]);

  const clearOperationJob = useCallback(() => {
    monitor.clear();
  }, [monitor.clear]);

  const clearRecent = useCallback(() => {
    // Also clear completed/error batch jobs
    if (batchJob && batchJob.status !== "running") {
      clearBatchJob();
    }
    if (operationJob && operationJob.status !== "running") {
      clearOperationJob();
    }
  }, [batchJob, clearBatchJob, operationJob, clearOperationJob]);

  const acceptOperation = (task: Parameters<typeof monitor.accept>[0], accepted: Parameters<typeof monitor.accept>[1]) => {
    if (task.operation_type === "admin-danbooru-batch-import" || task.operation_type === "admin-danbooru-url-batch-import") batchMonitor.accept(task, accepted);
    else monitor.accept(task, accepted);
  };

  return (
    <NotificationContext.Provider value={{
      clearRecent,
      batchJob, startBatchJob, clearBatchJob,
      operationJob, startOperationJob, clearOperationJob,
    }}>
      <OperationMonitorContext.Provider value={{ ...monitor, accept: acceptOperation }}>{children}</OperationMonitorContext.Provider>
    </NotificationContext.Provider>
  );
}

export function useNotifications(): NotificationCtx {
  return useContext(NotificationContext);
}

// ─── Notification Bell ───

// Keep popup positioning and presentation out of the shared session/provider chunk.
export const NotificationBell = dynamic(() => import("./NotificationBell"), {
  ssr: false,
  loading: () => <span className="inline-block h-11 w-11" aria-hidden="true" />,
});

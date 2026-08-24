"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type {
  AdminOperationAccepted,
  AdminOperationSnapshot,
  AdminOperationSnapshotResponse,
  AdminOperationStatus,
} from "@/lib/api/types";

const ACTIVE_STATUSES = new Set(["enqueued", "running", "recovering", "paused"]);
const ADMIN_OPERATION_POLL_MS = 1_000;

export interface AdminOperationController<TResult, TVariables = void> {
  operationType: string;
  taskId: string | null;
  task: AdminOperationStatus<TResult> | null;
  snapshot: AdminOperationSnapshot<TResult> | null;
  result: TResult | null;
  isStarting: boolean;
  isRetrying: boolean;
  isActive: boolean;
  isLatestLoading: boolean;
  canStart: boolean;
  startError: Error | null;
  taskError: Error | null;
  latestError: Error | null;
  start: (variables: TVariables) => void;
  retry: () => void;
  retryLatest: () => void;
}

export function useAdminOperation<TResult, TVariables = void>({
  operationType,
  scope,
  startOperation,
  loadLatest,
  onCompleted,
}: {
  operationType: string;
  scope: string;
  startOperation: (variables: TVariables) => Promise<AdminOperationAccepted>;
  loadLatest: () => Promise<AdminOperationSnapshotResponse<TResult>>;
  onCompleted?: (result: TResult) => void;
}): AdminOperationController<TResult, TVariables> {
  const queryClient = useQueryClient();
  const identity = `${operationType}:${scope}`;
  const [startedTask, setStartedTask] = useState<{ identity: string; taskId: string } | null>(null);
  const notifiedCompletion = useRef<string | null>(null);
  const reconciledTerminal = useRef<string | null>(null);
  const snapshotKey = useMemo(
    () => ["admin-operation-snapshot", operationType, scope] as const,
    [operationType, scope],
  );

  const latestQuery = useQuery({
    queryKey: snapshotKey,
    queryFn: loadLatest,
    staleTime: 30_000,
    refetchOnWindowFocus: false,
  });
  const current = latestQuery.data?.current ?? null;
  const startedTaskId = startedTask?.identity === identity ? startedTask.taskId : null;
  const taskId = startedTaskId ?? current?.task_id ?? null;

  const taskQuery = useQuery({
    queryKey: ["admin-operation-task", taskId],
    queryFn: () => apiTask<TResult>(taskId!),
    enabled: taskId !== null,
    placeholderData: current?.task_id === taskId
      ? {
          ...current,
          job_id: current.job_id ?? current.task_id,
          result: null,
          error: null,
        }
      : undefined,
    refetchInterval: (query) => {
      const task = query.state.data;
      return !task || ACTIVE_STATUSES.has(task.status)
        ? ADMIN_OPERATION_POLL_MS
        : false;
    },
    refetchOnWindowFocus: false,
  });

  const startMutation = useMutation({
    mutationFn: startOperation,
    onSuccess: (accepted) => {
      notifiedCompletion.current = null;
      reconciledTerminal.current = null;
      setStartedTask({ identity, taskId: accepted.task_id });
      queryClient.setQueryData<AdminOperationStatus<TResult>>(
        ["admin-operation-task", accepted.task_id],
        {
          ...accepted,
          progress: { phase: "enqueued", label: "" },
          result: null,
          error: null,
        },
      );
    },
  });

  const retryMutation = useMutation({
    mutationFn: () => {
      if (!taskId) throw new Error("Missing TaskRun id");
      return retryTask(taskId);
    },
    onSuccess: (accepted) => {
      notifiedCompletion.current = null;
      reconciledTerminal.current = null;
      queryClient.setQueryData<AdminOperationStatus<TResult>>(
        ["admin-operation-task", accepted.task_id],
        {
          ...accepted,
          progress: { phase: "enqueued", label: "" },
          result: null,
          error: null,
        },
      );
      void queryClient.invalidateQueries({
        queryKey: ["admin-operation-task", accepted.task_id],
      });
    },
  });

  const task = taskQuery.data ?? null;
  useEffect(() => {
    if (
      !taskId
      || !task
      || ACTIVE_STATUSES.has(task.status)
      || reconciledTerminal.current === `${identity}:${taskId}`
    ) {
      return;
    }
    reconciledTerminal.current = `${identity}:${taskId}`;
    if (task.status === "complete") {
      void queryClient.invalidateQueries({ queryKey: snapshotKey });
    }
    if (task.status === "complete" && task.result) {
      notifiedCompletion.current = `${identity}:${taskId}`;
      onCompleted?.(task.result);
    }
  }, [identity, onCompleted, queryClient, snapshotKey, task, taskId]);

  const completedTaskSnapshot: AdminOperationSnapshot<TResult> | null =
    task?.status === "complete" && task.result && taskId
      ? {
          task_id: taskId,
          job_id: task.rq_job_id ?? task.job_id,
          status: "complete",
          operation_type: task.operation_type,
          progress: task.progress,
          result: task.result,
          completed_at: new Date().toISOString(),
        }
      : null;
  const snapshot = completedTaskSnapshot ?? latestQuery.data?.snapshot ?? null;
  const isActive = !!task && ACTIVE_STATUSES.has(task.status);
  const canStart = !latestQuery.isLoading
    && !latestQuery.isError
    && !startMutation.isPending
    && !isActive
    && current === null;

  return {
    operationType,
    taskId,
    task,
    snapshot,
    result: snapshot?.result ?? null,
    isStarting: startMutation.isPending,
    isRetrying: retryMutation.isPending,
    isActive,
    isLatestLoading: latestQuery.isLoading,
    canStart,
    startError: startMutation.error,
    taskError: taskQuery.error,
    latestError: latestQuery.error,
    start: (variables) => {
      if (canStart) startMutation.mutate(variables);
    },
    retry: retryMutation.mutate,
    retryLatest: () => { void latestQuery.refetch(); },
  };
}

async function apiTask<TResult>(taskId: string): Promise<AdminOperationStatus<TResult>> {
  const { api } = await import("@/lib/api");
  return api.getAdminOperationTask<TResult>(taskId);
}

async function retryTask(taskId: string): Promise<AdminOperationAccepted> {
  const { api } = await import("@/lib/api");
  return api.retryAdminOperation(taskId);
}

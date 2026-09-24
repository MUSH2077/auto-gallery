"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type {
  AdminOperationAccepted,
  AdminOperationSnapshot,
  AdminOperationSnapshotResponse,
  AdminOperationStatus,
} from "@/lib/api/types";
import { ADMIN_OPERATION_CONFIRM_MS, pollInterval } from "@/lib/polling";

const ACTIVE_STATUSES = new Set(["enqueued", "running", "recovering", "paused"]);
const RETRYABLE_STATUSES = new Set(["failed", "stale", "cancelled"]);

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
  canRetry: boolean;
  startError: Error | null;
  taskError: Error | null;
  latestError: Error | null;
  start: (variables: TVariables) => void;
  retry: () => void;
  retryLatest: () => void;
  resetStart: () => void;
}

export function useAdminOperation<TResult, TVariables = void>({
  operationType,
  scope,
  startOperation,
  loadLatest,
  initialAccepted,
  onCompleted,
}: {
  operationType: string;
  scope: string;
  startOperation: (variables: TVariables) => Promise<AdminOperationAccepted>;
  loadLatest: () => Promise<AdminOperationSnapshotResponse<TResult>>;
  initialAccepted?: AdminOperationAccepted | null;
  onCompleted?: (result: TResult) => void;
}): AdminOperationController<TResult, TVariables> {
  const queryClient = useQueryClient();
  const identity = `${operationType}:${scope}`;
  const [startedTasks, setStartedTasks] = useState<Record<string, string>>(
    () => initialAccepted
      ? { [identity]: initialAccepted.task_id }
      : {},
  );
  const [displayedTasks, setDisplayedTasks] = useState<Record<string, string>>(
    () => initialAccepted
      ? { [identity]: initialAccepted.task_id }
      : {},
  );
  const pendingStartTarget = useRef<{
    identity: string;
    snapshotKey: readonly [string, string, string];
  } | null>(null);
  const pendingRetryTarget = useRef<{
    identity: string;
    snapshotKey: readonly [string, string, string];
  } | null>(null);
  const notifiedCompletion = useRef<string | null>(null);
  const reconciledTerminal = useRef<string | null>(null);
  const confirmedRunningTasks = useRef<Set<string>>(new Set());
  const snapshotKey = useMemo(
    () => ["admin-operation-snapshot", operationType, scope] as const,
    [operationType, scope],
  );

  const latestQuery = useQuery({
    queryKey: snapshotKey,
    queryFn: loadLatest,
    enabled: initialAccepted == null,
    staleTime: 30_000,
    refetchOnWindowFocus: false,
  });
  const current = latestQuery.data?.current ?? null;
  const startedTaskId = startedTasks[identity] ?? null;
  const displayedTaskId = displayedTasks[identity] ?? null;
  const taskId = startedTaskId ?? current?.task_id ?? displayedTaskId;

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
        ? pollInterval(true)
        : false;
    },
    refetchIntervalInBackground: false,
    refetchOnWindowFocus: false,
  });

  const startMutation = useMutation({
    mutationFn: startOperation,
    onSuccess: (accepted) => {
      const target = pendingStartTarget.current ?? { identity, snapshotKey };
      const acceptedIdentity = target.identity;
      pendingStartTarget.current = null;
      notifiedCompletion.current = null;
      reconciledTerminal.current = null;
      setStartedTasks((tasks) => ({ ...tasks, [acceptedIdentity]: accepted.task_id }));
      setDisplayedTasks((tasks) => ({ ...tasks, [acceptedIdentity]: accepted.task_id }));
      queryClient.setQueryData<AdminOperationSnapshotResponse<TResult>>(
        target.snapshotKey,
        (previous) => ({
          snapshot: previous?.snapshot ?? null,
          current: {
            task_id: accepted.task_id,
            job_id: accepted.job_id,
            status: accepted.status,
            operation_type: accepted.operation_type,
            progress: { phase: "enqueued", label: "" },
          },
        }),
      );
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
    onError: () => {
      const target = pendingStartTarget.current;
      pendingStartTarget.current = null;
      if (target) {
        void queryClient.invalidateQueries({ queryKey: target.snapshotKey });
      } else {
        void latestQuery.refetch();
      }
    },
  });

  const retryMutation = useMutation({
    mutationFn: () => {
      if (!taskId) throw new Error("Missing TaskRun id");
      pendingRetryTarget.current = { identity, snapshotKey };
      return retryTask(taskId);
    },
    onSuccess: (accepted) => {
      const target = pendingRetryTarget.current ?? { identity, snapshotKey };
      const acceptedIdentity = target.identity;
      pendingRetryTarget.current = null;
      notifiedCompletion.current = null;
      reconciledTerminal.current = null;
      setStartedTasks((tasks) => ({ ...tasks, [acceptedIdentity]: accepted.task_id }));
      setDisplayedTasks((tasks) => ({ ...tasks, [acceptedIdentity]: accepted.task_id }));
      queryClient.setQueryData<AdminOperationSnapshotResponse<TResult>>(
        target.snapshotKey,
        (previous) => ({
          snapshot: previous?.snapshot ?? null,
          current: {
            task_id: accepted.task_id,
            job_id: accepted.job_id,
            status: accepted.status,
            operation_type: accepted.operation_type,
            progress: { phase: "enqueued", label: "" },
          },
        }),
      );
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
    onError: () => {
      const target = pendingRetryTarget.current;
      pendingRetryTarget.current = null;
      if (target) {
        void queryClient.invalidateQueries({ queryKey: target.snapshotKey });
      } else {
        void latestQuery.refetch();
      }
    },
  });

  const task = taskQuery.data ?? null;
  const refetchTask = taskQuery.refetch;
  useEffect(() => {
    if (!taskId || task?.status !== "running") return;
    const confirmationKey = `${identity}:${taskId}`;
    if (confirmedRunningTasks.current.has(confirmationKey)) return;
    confirmedRunningTasks.current.add(confirmationKey);
    const timer = window.setTimeout(() => {
      void refetchTask();
    }, ADMIN_OPERATION_CONFIRM_MS);
    return () => window.clearTimeout(timer);
  }, [identity, refetchTask, task?.status, taskId]);

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
    setDisplayedTasks((tasks) => ({ ...tasks, [identity]: taskId }));
    setStartedTasks((tasks) => {
      if (tasks[identity] !== taskId) return tasks;
      const next = { ...tasks };
      delete next[identity];
      return next;
    });
    if (task.status === "complete") {
      const completed: AdminOperationSnapshot<TResult> | null = task.result
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
      queryClient.setQueryData<AdminOperationSnapshotResponse<TResult>>(
        snapshotKey,
        (previous) => ({
          snapshot: completed ?? previous?.snapshot ?? null,
          current: null,
        }),
      );
    } else {
      queryClient.setQueryData<AdminOperationSnapshotResponse<TResult>>(
        snapshotKey,
        (previous) => ({
          snapshot: previous?.snapshot ?? null,
          current: null,
        }),
      );
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
  useEffect(() => {
    if (!snapshot?.result) return;
    const completionIdentity = `${identity}:${snapshot.task_id}`;
    if (notifiedCompletion.current === completionIdentity) return;
    notifiedCompletion.current = completionIdentity;
    onCompleted?.(snapshot.result);
  }, [identity, onCompleted, snapshot]);
  const isActive = !!task && ACTIVE_STATUSES.has(task.status);
  const hasRetryableFailure = !!task && RETRYABLE_STATUSES.has(task.status);
  const canStart = !latestQuery.isLoading
    && !latestQuery.isError
    && !startMutation.isPending
    && !retryMutation.isPending
    && !isActive
    && !hasRetryableFailure
    && startedTaskId === null
    && current === null;
  const canRetry = !!taskId
    && hasRetryableFailure
    && !startMutation.isPending
    && !retryMutation.isPending;

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
    canRetry,
    startError: startMutation.error,
    taskError: taskQuery.error,
    latestError: latestQuery.error,
    start: (variables) => {
      if (canStart) {
        pendingStartTarget.current = { identity, snapshotKey };
        startMutation.mutate(variables);
      }
    },
    retry: () => {
      if (canRetry) retryMutation.mutate();
    },
    retryLatest: () => { void latestQuery.refetch(); },
    resetStart: () => startMutation.reset(),
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

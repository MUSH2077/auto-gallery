"use client";

import Link from "next/link";
import { RealProgressBar } from "@/components/RealProgressBar";
import { useT } from "@/lib/i18n";
import type { AdminOperationController } from "@/lib/useAdminOperation";

export function AdminOperationStatus<TResult, TVariables>({
  controller,
}: {
  controller: AdminOperationController<TResult, TVariables>;
}) {
  const t = useT();
  const { task } = controller;
  const progress = task?.progress
    ? {
        stage: task.progress.phase,
        message: task.progress.label,
        current: task.progress.current,
        total: task.progress.total,
        percent: task.progress.percent,
      }
    : null;
  const requestError = controller.startError || controller.taskError;
  const visibleTaskId = controller.taskId ?? controller.snapshot?.task_id ?? null;

  return (
    <section
      data-admin-operation={controller.operationType}
      className="mt-3 rounded-md border border-border bg-surface p-3 text-sm"
      aria-live="polite"
      aria-busy={controller.isStarting || controller.isRetrying || controller.isActive}
    >
      {controller.isLatestLoading && !task ? (
        <p role="status" className="text-xs text-muted">{t("admin_operation.latest_loading")}</p>
      ) : null}

      {controller.latestError && !task ? (
        <div role="alert" className="text-xs text-danger">
          <p>{t("admin_operation.latest_error")}</p>
          <button type="button" className="btn-ghost mt-2 text-danger" onClick={controller.retryLatest}>
            {t("common.retry")}
          </button>
        </div>
      ) : null}

      {controller.isStarting ? (
        <p role="status" className="text-xs text-muted">{t("admin_operation.starting")}</p>
      ) : null}

      {!controller.isLatestLoading && !controller.latestError && !task ? (
        controller.snapshot ? (
          <div className="flex flex-wrap items-center justify-between gap-2">
            <span className="font-medium text-fg">{t("admin_operation.completed")}</span>
            <Link
              href={`/admin/jobs?tab=admin&task=${encodeURIComponent(controller.snapshot.task_id)}`}
              className="text-xs font-medium text-accent underline-offset-2 hover:underline"
            >
              {t("jobs.task_detail")}
            </Link>
          </div>
        ) : (
          <p className="text-xs text-muted">{t("admin_operation.no_snapshot")}</p>
        )
      ) : null}

      {task ? (
        <div className="space-y-2">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <span className="font-medium text-fg">
              {task.status === "complete"
                ? t("admin_operation.completed")
                : task.status === "failed" || task.status === "stale" || task.status === "cancelled"
                  ? t("admin_operation.failed")
                  : t("admin_operation.running")}
            </span>
            {visibleTaskId ? (
              <Link
                href={`/admin/jobs?tab=admin&task=${encodeURIComponent(visibleTaskId)}`}
                className="text-xs font-medium text-accent underline-offset-2 hover:underline"
              >
                {t("jobs.task_detail")}
              </Link>
            ) : null}
          </div>
          {controller.isActive && progress ? <RealProgressBar progress={progress} /> : null}
          {(task.status === "failed" || task.status === "stale" || task.status === "cancelled") ? (
            <div role="alert" className="rounded-md border border-danger/30 bg-danger-subtle p-3 text-danger">
              <p className="text-xs">{task.error || t("admin_operation.failed")}</p>
              {task.reason_code ? <code className="mt-1 block text-[11px]">{task.reason_code}</code> : null}
              <button
                type="button"
                className="btn-ghost mt-2 text-danger"
                disabled={!controller.canRetry}
                onClick={controller.retry}
              >
                {controller.isRetrying ? t("admin_operation.retrying") : t("common.retry")}
              </button>
            </div>
          ) : null}
        </div>
      ) : null}

      {requestError && !task ? (
        <div role="alert" className="text-xs text-danger">{requestError.message}</div>
      ) : null}
    </section>
  );
}

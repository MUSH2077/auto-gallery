"use client";
import { useState, useRef, useId, type ReactNode } from "react";
import { Bell, Check, X } from "lucide-react";
import { useRouter } from "next/navigation";
import Link from "next/link";
import * as Popover from "@radix-ui/react-popover";
import { useQuery } from "@tanstack/react-query";
import { useT, type TFunction } from "@/lib/i18n";
import { api, queryKeys } from "@/lib/api";
import { usePresence, useStaggeredEntrance } from "@/lib/motion";
import { useI18nFormat } from "@/lib/i18n-format";
import { adminRoutes } from "@/lib/adminRoutes";
import { taskRunDestination } from "@/lib/taskRoutes";
import type { OperationJobState } from "@/lib/operationMonitor";
import { useNotifications } from "./NotificationCenter";
type ActivityStatus = "running" | "completed" | "error" | "pending";

function operationResultMessage(operation: OperationJobState, t: TFunction): string {
  const result = operation.result;
  const message = typeof result?.message === "string" ? result.message : null;
  const count = (key: string) => typeof result?.[key] === "number" ? result[key] : 0;
  if (operation.kind === "danbooru-import-all" && operation.status === "completed") {
    if (result?.found === false) return message || t("notification.no_danbooru_match");
    return t("notification.creators_refreshed");
  }
  if (operation.kind === "danbooru-mapping-refresh" && operation.status === "completed") {
    return t("danbooru.refresh_result", {
      found: count("found"),
      scanned: count("scanned"),
      total: count("total"),
      notFound: count("not_found"),
      skipped: count("skipped"),
      errors: count("errors"),
    });
  }
  if (operation.kind === "hierarchy-delete" && operation.status === "completed") {
    return t("deletion.result", {
      targets: Array.isArray(result?.entity_ids) ? result.entity_ids.length : 0,
      processed: count("trashed_or_purged_works"),
      shared: count("shared_works_preserved"),
    });
  }
  return message || t("status.completed");
}

const TASK_RUN_ID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

function operationDestination(operation: OperationJobState): string | null {
  if (operation.taskId && TASK_RUN_ID.test(operation.taskId)) {
    return `${adminRoutes.jobs}?tab=admin&task=${operation.taskId}`;
  }
  if (operation.kind === "danbooru-import-all") return adminRoutes.danbooru;
  return null;
}

// Map a server task_run status onto the bell's visual status set.
function taskActivityStatus(status?: string | null): ActivityStatus {
  if (status === "running" || status === "downloading" || status === "importing" || status === "enqueued") return "running";
  if (status === "complete" || status === "completed") return "completed";
  if (status === "failed" || status === "cancelled" || status === "stale") return "error";
  return "pending";
}

function NotificationItemContent({href,onNavigate,children}:{href:string|null;onNavigate:()=>void;children:ReactNode}) {
  return href ? <Link href={href} className="flex-1 min-w-0" onClick={onNavigate}>{children}</Link>
    : <div className="flex-1 min-w-0">{children}</div>;
}

export default function NotificationBell() {
  const t = useT();
  const fmt = useI18nFormat();
  const router = useRouter();
  const {
    clearRecent,
    clearBatchJob, batchJob,
    clearOperationJob, operationJob,
  } = useNotifications();
  const [open, setOpen] = useState(false);
  const { mounted, closing } = usePresence(open);
  const ref = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const panelId = useId();

  // Recent tasks come from the same server feed as /admin/notifications.
  const recent = useQuery({
    queryKey: [...queryKeys.tasks.all, "bell-recent"],
    queryFn: () => api.listTasks({ include_account: true, limit: 10 }),
    enabled: open,
    staleTime: 10_000,
  });
  const serverItems = recent.data?.items ?? [];
  const itemEntrance = useStaggeredEntrance(serverItems.map((task) => task.id));

  const activeCount = (batchJob?.status === "running" ? 1 : 0)
    + (operationJob?.status === "running" ? 1 : 0)
    + serverItems.filter((task) => taskActivityStatus(task.status) === "running").length;
  const hasRecent = serverItems.length > 0 || !!batchJob || !!operationJob;
  const operationLink = operationJob ? operationDestination(operationJob) : null;

  const statusIcon = (status: ActivityStatus) => {
    if (status === "running") {
      return <span className="w-2 h-2 rounded-full bg-accent animate-pulse shrink-0" />;
    }
    if (status === "completed") {
      return <Check aria-hidden="true" className="h-4 w-4 shrink-0 text-success" strokeWidth={2.5} />;
    }
    return <X aria-hidden="true" className="h-4 w-4 shrink-0 text-danger" strokeWidth={2.5} />;
  };

  const timeAgo = (ts: number): string => fmt.relative(new Date(ts).toISOString());

  return (
    <Popover.Root open={open} onOpenChange={setOpen}><div ref={ref} className="relative">
      <Popover.Trigger asChild><button
        ref={triggerRef}
        type="button"
        className="relative inline-flex h-11 w-11 items-center justify-center rounded-md text-muted transition-colors hover:bg-subtle hover:text-fg"
        title={t("notification.bell_label")}
        aria-label={t("notification.bell_label")}
        aria-expanded={open}
        aria-controls={panelId}
        onKeyDown={(event) => { if (open && event.key === "Escape") event.stopPropagation(); }}
      >
        <Bell aria-hidden="true" className="h-4 w-4" />
        {activeCount > 0 && (
          <span className="absolute -top-0.5 -right-0.5 bg-danger text-white text-sm font-bold rounded-full min-w-5 h-5 flex items-center justify-center px-1 leading-5">
            {activeCount}
          </span>
        )}
      </button></Popover.Trigger>

      {mounted && (
        <Popover.Portal container={ref.current?.closest(".gallery-app") || undefined} forceMount>
        <Popover.Content id={panelId} forceMount side="bottom" align="end" sideOffset={8} collisionPadding={16}
          aria-label={t("notification.recent")}
          onEscapeKeyDown={(event) => event.stopPropagation()}
          className={`popover ${closing ? "popover-exit" : ""} z-50 flex flex-col overflow-hidden rounded-md border border-border bg-white text-fg shadow-overlay dark:shadow-overlay-dark dark:border-border dark:bg-surface dark:text-fg`}
          style={{width:"min(320px, calc(100vw - 32px))",maxHeight:"min(480px, var(--radix-popover-content-available-height))"}}>
          <div className="flex shrink-0 flex-wrap items-center justify-between gap-x-3 gap-y-1 border-b border-border px-4 py-2.5 dark:border-border">
            <span className="text-sm font-semibold">{t("notification.recent")}</span>
            <div className="flex items-center gap-3">
              <button onClick={() => { setOpen(false); router.push("/admin/notifications"); }}
                className="text-xs text-accent hover:text-accent dark:hover:text-accent transition-colors">
                {t("notifications.title")} →
              </button>
              {(batchJob || operationJob) && (
                <button onClick={() => { clearRecent(); }}
                  className="text-xs text-muted transition-colors hover:text-fg dark:text-muted dark:hover:text-fg">
                  {t("notification.clear_all")}
                </button>
              )}
            </div>
          </div>
          <div className="min-h-0 flex-auto overflow-y-auto">
            {!hasRecent ? (
              <div className="px-4 py-8 text-center text-sm text-muted">
                {t("notification.empty")}
              </div>
            ) : (
              <>
                {batchJob && (
                  <div className="cursor-pointer border-b border-border px-4 py-2.5 transition-colors hover:bg-subtle dark:border-border dark:hover:bg-subtle"
                    >
                    <div className="flex items-start gap-2.5">
                      <div className="mt-0.5">{statusIcon(batchJob.status)}</div>
                      <Link href={adminRoutes.danbooru} className="flex-1 min-w-0" onClick={() => setOpen(false)}>
                        <p className="text-sm font-medium truncate">
                          {batchJob.importType === "pixiv" ? t("notification.batch_import") : t("notification.url_batch_import")}
                        </p>
                        {batchJob.status === "error" && (
                          <p className="text-xs text-danger mt-0.5">{t("notification.job_failed")}</p>
                        )}
                        {batchJob.progress && (
                          <>
                            <p className="mt-0.5 text-xs text-muted">
                              {t("notification.import_progress", {
                                current: batchJob.progress.current,
                                total: batchJob.progress.total,
                                imported: batchJob.progress.imported,
                              })}
                            </p>
                            <div className="mt-1.5 h-1 w-full overflow-hidden rounded-full bg-subtle dark:bg-border">
                              <div className="h-full w-full bg-accent rounded-full transition-transform duration-slow ease-out"
                                style={{ transform: `scaleX(${batchJob.progress.current / batchJob.progress.total})`, transformOrigin: "left" }} />
                            </div>
                          </>
                        )}
                        {batchJob.result && (
                          <p className="mt-0.5 text-xs text-muted">
                            {t("notification.import_done", { count: batchJob.result.imported_count || batchJob.result.imported?.length || 0 })}
                          </p>
                        )}
                        <span className="gallery-notification-time text-sm text-muted">{timeAgo(batchJob.startedAt)}</span>
                      </Link>
                      {batchJob.status !== "running" && (
                        <button
                          onClick={(e) => { e.stopPropagation(); clearBatchJob(); }}
                          className="mt-0.5 inline-flex h-11 w-11 shrink-0 items-center justify-center rounded-md text-muted hover:bg-subtle hover:text-fg"
                          aria-label={t("common.dismiss")}
                        >
                          <X aria-hidden="true" className="h-4 w-4" />
                        </button>
                      )}
                    </div>
                  </div>
                )}
                {operationJob && (
                  <div className={`${operationLink ? "cursor-pointer" : "cursor-default"} border-b border-border px-4 py-2.5 transition-colors hover:bg-subtle dark:border-border dark:hover:bg-subtle`}
                    >
                    <div className="flex items-start gap-2.5">
                      <div className="mt-0.5">{statusIcon(operationJob.status)}</div>
                      <NotificationItemContent href={operationLink} onNavigate={() => setOpen(false)}>
                        <p className="text-sm font-medium truncate">{operationJob.title}</p>
                        {operationJob.status === "error" && (
                          <p className="text-xs text-danger mt-0.5 truncate">{operationJob.error || t("notification.operation_failed")}</p>
                        )}
                        {operationJob.progress?.label && operationJob.status === "running" && (
                          <p className="mt-0.5 text-xs text-muted truncate">
                            {operationJob.progress.label}
                          </p>
                        )}
                        {operationJob.progress?.total && operationJob.progress.current !== undefined && (
                          <div className="mt-1.5 h-1 w-full overflow-hidden rounded-full bg-subtle dark:bg-border">
                            <div className="h-full w-full bg-accent rounded-full transition-transform duration-slow ease-out"
                              style={{ transform: `scaleX(${operationJob.progress.current / operationJob.progress.total})`, transformOrigin: "left" }} />
                          </div>
                        )}
                        {operationJob.result && (
                          <p className="mt-0.5 text-xs text-muted truncate">
                            {operationResultMessage(operationJob, t)}
                          </p>
                        )}
                        <span className="gallery-notification-time text-sm text-muted">{timeAgo(operationJob.startedAt)}</span>
                      </NotificationItemContent>
                      {operationJob.status !== "running" && (
                        <button
                          onClick={(e) => { e.stopPropagation(); clearOperationJob(); }}
                          className="mt-0.5 inline-flex h-11 w-11 shrink-0 items-center justify-center rounded-md text-muted hover:bg-subtle hover:text-fg"
                          aria-label={t("common.dismiss")}
                        >
                          <X aria-hidden="true" className="h-4 w-4" />
                        </button>
                      )}
                    </div>
                  </div>
                )}
                {serverItems.map((task, index) => {
                  const st = taskActivityStatus(task.status);
                  const link = taskRunDestination(task);
                  const entrance = itemEntrance(task.id, index);
                  const content = (
                      <div className="flex items-start gap-2.5">
                        <div className="mt-0.5">{statusIcon(st)}</div>
                        <div className="flex-1 min-w-0">
                          <div className="flex items-center justify-between gap-2">
                            <p className={`text-sm truncate ${st === "running" ? "font-medium" : ""}`}>{task.title || task.operation_type || task.kind}</p>
                            <span className="gallery-notification-time shrink-0 text-sm text-muted">{task.created_at ? timeAgo(new Date(task.created_at).getTime()) : ""}</span>
                          </div>
                        </div>
                      </div>
                  );
                  const className = `${entrance.className} block border-b border-border px-4 py-2.5 transition-colors hover:bg-subtle dark:border-border dark:hover:bg-subtle`;
                  return link ? <Link key={task.id} href={link} className={className} style={entrance.style} onClick={() => setOpen(false)}>{content}</Link>
                    : <div key={task.id} className={className} style={entrance.style}>{content}</div>;
                })}
              </>
            )}
          </div>
        </Popover.Content></Popover.Portal>
      )}
    </div></Popover.Root>
  );
}

"use client";
import { createContext, useContext, useState, useCallback, useRef, useEffect, useId, type ReactNode } from "react";
import { Bell, Check, X } from "lucide-react";
import { useRouter } from "next/navigation";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useT, type TFunction } from "@/lib/i18n";
import { api, queryKeys } from "@/lib/api";
import { usePresence, useStaggeredEntrance } from "@/lib/motion";
import { useI18nFormat } from "@/lib/i18n-format";
import { adminRoutes } from "@/lib/adminRoutes";
import { usePermissions } from "@/lib/usePermissions";
import { useAuth } from "@/lib/auth";
import { taskRunDestination } from "@/lib/taskRoutes";
import { OperationMonitorContext, useOperationMonitor, type OperationJobState } from "@/lib/operationMonitor";
import { normalizeOperationStatus } from "@/lib/operationStatus";
import { refreshOperationResources } from "@/lib/operationRefresh";
import { pollInterval } from "@/lib/polling";

type ActivityStatus = "running" | "completed" | "error" | "pending";

export interface ActivityItem {
  id: string;
  type: "job" | "mutation" | "system";
  title: string;
  message?: string;
  timestamp: number;
  status: ActivityStatus;
  progress?: number;
  link?: string;
}

import { useBatchImportMonitor, type BatchJobState } from "@/lib/batchImportMonitor";
export type { BatchJobState } from "@/lib/batchImportMonitor";

const OPERATION_STORAGE_KEY = "admin_operation_job";

interface NotificationCtx {
  items: ActivityItem[];
  addActivity: (item: Omit<ActivityItem, "id" | "timestamp">) => string;
  updateActivity: (id: string, patch: Partial<Pick<ActivityItem, "status" | "message" | "progress">>) => void;
  removeActivity: (id: string) => void;
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
  items: [],
  addActivity: () => "",
  updateActivity: () => {},
  removeActivity: () => {},
  clearRecent: () => {},
  batchJob: null,
  startBatchJob: () => {},
  clearBatchJob: () => {},
  operationJob: null,
  startOperationJob: () => {},
  clearOperationJob: () => {},
});

let _activityId = 0;
const MAX_ITEMS = 50;
const COMPLETED_TTL = 30_000;

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

export function NotificationProvider({ children }: { children: ReactNode }) {
  const t = useT();
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
  const [items, setItems] = useState<ActivityItem[]>([]);
  const batchMonitor = useBatchImportMonitor(canAccessBatchImports);
  const { batchJob } = batchMonitor;
  const fetchOperationStatus = useCallback((operation: OperationJobState) => operation.kind === "danbooru-mapping-refresh"
    ? api.getDanbooruMappingRefreshStatus(operation.jobId)
    : api.getAdminOperationStatus(operation.taskId ?? operation.jobId), []);
  const refreshOperation = useCallback((operation: OperationJobState, result: OperationJobState["result"]) => {
    refreshOperationResources(qc, operation.kind, operation.meta, result);
  }, [qc]);
  const monitor = useOperationMonitor({
    storageKey: OPERATION_STORAGE_KEY,
    fetchStatus: fetchOperationStatus,
    refreshResources: refreshOperation,
  });
  const operationJob = monitor.operation;
  const timersRef = useRef<Map<string, ReturnType<typeof setTimeout>>>(new Map());

  // ─── Activity log ───
  useEffect(() => {
    const timers = timersRef.current;
    return () => { timers.forEach((t) => clearTimeout(t)); timers.clear(); };
  }, []);

  useEffect(() => {
    if (!authLoading && !isAuthenticated) batchMonitor.clear();
  }, [authLoading, isAuthenticated, batchMonitor.clear]);

  const scheduleRemoval = useCallback((id: string) => {
    const existing = timersRef.current.get(id);
    if (existing) clearTimeout(existing);
    const timer = setTimeout(() => {
      setItems((prev) => prev.filter((a) => a.id !== id));
      timersRef.current.delete(id);
    }, COMPLETED_TTL);
    timersRef.current.set(id, timer);
  }, []);

  const addActivity = useCallback((item: Omit<ActivityItem, "id" | "timestamp">) => {
    const id = `act_${++_activityId}_${Date.now()}`;
    const now = Date.now();
    setItems((prev) => {
      const next = [{ ...item, id, timestamp: now }, ...prev];
      if (next.length > MAX_ITEMS) {
        const running = next.filter((a) => a.status === "running");
        const rest = next.filter((a) => a.status !== "running");
        return [...running, ...rest.slice(0, MAX_ITEMS - running.length)];
      }
      return next;
    });
    return id;
  }, []);

  const updateActivity = useCallback((id: string, patch: Partial<Pick<ActivityItem, "status" | "message" | "progress">>) => {
    setItems((prev) => prev.map((a) => {
      if (a.id !== id) return a;
      const updated = { ...a, ...patch };
      if ((patch.status === "completed" || patch.status === "error") && a.status === "running") {
        scheduleRemoval(id);
      }
      return updated;
    }));
  }, [scheduleRemoval]);

  const removeActivity = useCallback((id: string) => {
    const timer = timersRef.current.get(id);
    if (timer) { clearTimeout(timer); timersRef.current.delete(id); }
    setItems((prev) => prev.filter((a) => a.id !== id));
  }, []);

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
    timersRef.current.forEach((t) => clearTimeout(t));
    timersRef.current.clear();
    setItems((prev) => prev.filter((a) => a.status === "running"));
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
      items, addActivity, updateActivity, removeActivity, clearRecent,
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

// Map a server task_run status onto the bell's visual status set.
function taskActivityStatus(status?: string | null): ActivityStatus {
  if (status === "running" || status === "downloading" || status === "importing" || status === "enqueued") return "running";
  if (status === "complete" || status === "completed") return "completed";
  if (status === "failed" || status === "cancelled" || status === "stale") return "error";
  return "pending";
}

export function NotificationBell() {
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

  useEffect(() => {
    function handleClick(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    }
    document.addEventListener("mousedown", handleClick);
    return () => document.removeEventListener("mousedown", handleClick);
  }, []);

  useEffect(() => {
    if (!open) return;
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      event.preventDefault();
      setOpen(false);
      triggerRef.current?.focus();
    };
    document.addEventListener("keydown", handleKeyDown);
    return () => document.removeEventListener("keydown", handleKeyDown);
  }, [open]);

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
    <div ref={ref} className="relative">
      <button
        ref={triggerRef}
        type="button"
        onClick={() => setOpen(!open)}
        className="relative inline-flex h-11 w-11 items-center justify-center rounded-md text-muted transition-colors hover:bg-subtle hover:text-fg"
        title={t("notification.bell_label")}
        aria-label={t("notification.bell_label")}
        aria-expanded={open}
        aria-controls={panelId}
      >
        <Bell aria-hidden="true" className="h-4 w-4" />
        {activeCount > 0 && (
          <span className="absolute -top-0.5 -right-0.5 bg-danger text-white text-[10px] font-bold rounded-full min-w-[16px] h-4 flex items-center justify-center px-0.5 leading-none">
            {activeCount}
          </span>
        )}
      </button>

      {mounted && (
        <div id={panelId} className={`popover ${closing ? "popover-exit" : ""} absolute right-0 z-50 mt-2 max-h-[480px] w-80 overflow-hidden rounded-md border border-border bg-white text-fg shadow-overlay dark:shadow-overlay-dark dark:border-border dark:bg-surface dark:text-fg`}>
          <div className="flex items-center justify-between border-b border-border px-4 py-2.5 dark:border-border">
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
          <div className="overflow-y-auto max-h-[420px]">
            {!hasRecent ? (
              <div className="px-4 py-8 text-center text-sm text-muted">
                {t("notification.empty")}
              </div>
            ) : (
              <>
                {batchJob && (
                  <div className="cursor-pointer border-b border-border px-4 py-2.5 transition-colors hover:bg-subtle dark:border-border dark:hover:bg-subtle"
                    onClick={() => { setOpen(false); router.push(adminRoutes.danbooru); }}>
                    <div className="flex items-start gap-2.5">
                      <div className="mt-0.5">{statusIcon(batchJob.status)}</div>
                      <div className="flex-1 min-w-0">
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
                        <span className="text-[10px] text-muted">{timeAgo(batchJob.startedAt)}</span>
                      </div>
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
                    onClick={() => {
                      if (!operationLink) return;
                      setOpen(false);
                      router.push(operationLink);
                    }}>
                    <div className="flex items-start gap-2.5">
                      <div className="mt-0.5">{statusIcon(operationJob.status)}</div>
                      <div className="flex-1 min-w-0">
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
                        <span className="text-[10px] text-muted">{timeAgo(operationJob.startedAt)}</span>
                      </div>
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
                  return (
                    <div key={task.id}
                      className={`${entrance.className} border-b border-border px-4 py-2.5 transition-colors hover:bg-subtle dark:border-border dark:hover:bg-subtle ${link ? "cursor-pointer" : "cursor-default"}`}
                      style={entrance.style}
                      onClick={() => { if (link) { setOpen(false); router.push(link); } }}>
                      <div className="flex items-start gap-2.5">
                        <div className="mt-0.5">{statusIcon(st)}</div>
                        <div className="flex-1 min-w-0">
                          <div className="flex items-center justify-between gap-2">
                            <p className={`text-sm truncate ${st === "running" ? "font-medium" : ""}`}>{task.title || task.operation_type || task.kind}</p>
                            <span className="shrink-0 text-[10px] text-muted">{task.created_at ? timeAgo(new Date(task.created_at).getTime()) : ""}</span>
                          </div>
                        </div>
                      </div>
                    </div>
                  );
                })}
              </>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

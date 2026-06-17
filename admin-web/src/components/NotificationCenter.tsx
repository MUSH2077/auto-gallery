"use client";
import { createContext, useContext, useState, useCallback, useRef, useEffect, type ReactNode } from "react";
import { useRouter } from "next/navigation";
import { useQuery, useQueryClient, type QueryClient } from "@tanstack/react-query";
import { useT } from "@/lib/i18n";
import { api, queryKeys } from "@/lib/api";

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

// Batch import job state managed at layout level (survives navigation)
export interface BatchJobState {
  jobId: string;
  importType: "pixiv" | "url";
  total: number;
  startedAt: number;
  progress: { current: number; total: number; imported: number; errors: number } | null;
  result: any | null;
  status: "pending" | "running" | "completed" | "error";
}

export interface OperationJobState {
  jobId: string;
  kind: "admin-clear" | "danbooru-import-all";
  title: string;
  startedAt: number;
  status: "running" | "completed" | "error";
  progress: { phase?: string; label?: string; current?: number; total?: number } | null;
  result: any | null;
  error?: string;
  meta?: Record<string, any>;
}

const STORAGE_KEY = "danbooru_batch_job";
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

function operationResultMessage(operation: OperationJobState): string {
  if (operation.kind === "danbooru-import-all" && operation.status === "completed") {
    if (operation.result?.found === false) return operation.result.message || "No matching Danbooru artist found";
    return "Creators and subscriptions refreshed";
  }
  return operation.result?.message || "Complete";
}

function refetchCreatorSubscriptionQueries(qc: QueryClient) {
  qc.invalidateQueries({ queryKey: queryKeys.creators.all });
  qc.invalidateQueries({ queryKey: queryKeys.subscriptions.all });
  qc.invalidateQueries({ queryKey: queryKeys.creators.count });
  qc.invalidateQueries({ queryKey: queryKeys.subscriptions.count });
  void Promise.all([
    qc.refetchQueries({ queryKey: queryKeys.creators.all, type: "all" }),
    qc.refetchQueries({ queryKey: queryKeys.subscriptions.all, type: "all" }),
  ]);
}

function refreshOperationQueries(
  qc: QueryClient,
  kind: OperationJobState["kind"],
  meta?: Record<string, any>,
  result?: any,
) {
  if (kind === "danbooru-import-all") {
    refetchCreatorSubscriptionQueries(qc);
    qc.invalidateQueries({ queryKey: queryKeys.workbench });
    qc.invalidateQueries({ queryKey: ["creator-subscription-overview"] });
    const refetches = [
      qc.refetchQueries({ queryKey: queryKeys.workbench, type: "all" }),
      qc.refetchQueries({ queryKey: ["creator-subscription-overview"], type: "all" }),
    ];
    const creatorId = result?.creator_id;
    if (creatorId) {
      refetches.push(qc.refetchQueries({ queryKey: queryKeys.creators.detail(creatorId), type: "all" }));
      refetches.push(qc.refetchQueries({ queryKey: queryKeys.creators.links(creatorId), type: "all" }));
      refetches.push(qc.refetchQueries({ queryKey: ["creator-subscription-overview", creatorId], type: "all" }));
    }
    const subscriptionId = result?.subscription_id;
    if (subscriptionId) {
      refetches.push(qc.refetchQueries({ queryKey: queryKeys.subscriptions.detail(subscriptionId), type: "all" }));
      refetches.push(qc.refetchQueries({ queryKey: queryKeys.subscriptions.sources(subscriptionId), type: "all" }));
    }
    void Promise.all(refetches);
    return;
  }

  const entity = meta?.entity as string | undefined;
  if (entity === "creators" || entity === "all") {
    qc.setQueryData(queryKeys.creators.count, { count: 0 });
    qc.setQueryData(queryKeys.subscriptions.count, { count: 0 });
    qc.setQueriesData({ queryKey: ["creators", "list"] }, { items: [], total: 0 });
    qc.setQueriesData({ queryKey: ["subscriptions", "list"] }, []);
    qc.invalidateQueries({ queryKey: queryKeys.creators.all });
    qc.invalidateQueries({ queryKey: queryKeys.subscriptions.all });
    qc.invalidateQueries({ queryKey: queryKeys.workbench });
    qc.invalidateQueries({ queryKey: ["repositories"] });
    qc.invalidateQueries({ queryKey: ["creator-subscription-overview"] });
  }
  if (entity === "works" || entity === "all" || entity === "downloads") {
    qc.invalidateQueries({ queryKey: queryKeys.works.all });
    qc.invalidateQueries({ queryKey: queryKeys.workbench });
    qc.invalidateQueries({ queryKey: ["storage-breakdown"] });
    qc.invalidateQueries({ queryKey: ["system-info"] });
    qc.invalidateQueries({ queryKey: queryKeys.curation.all });
    qc.invalidateQueries({ queryKey: queryKeys.downloadJobs.all });
    qc.invalidateQueries({ queryKey: queryKeys.importJobs.all });
  }
  if (entity === "tags" || entity === "all") {
    qc.invalidateQueries({ queryKey: queryKeys.tags.all });
    qc.invalidateQueries({ queryKey: queryKeys.works.all });
    qc.invalidateQueries({ queryKey: queryKeys.workbench });
  }
  if (entity === "jobs" || entity === "downloads" || entity === "all") {
    qc.invalidateQueries({ queryKey: queryKeys.subscriptions.all });
    qc.invalidateQueries({ queryKey: queryKeys.downloadJobs.all });
    qc.invalidateQueries({ queryKey: queryKeys.importJobs.all });
    qc.invalidateQueries({ queryKey: ["queue-stats"] });
    qc.invalidateQueries({ queryKey: queryKeys.workbench });
  }
  if (entity === "settings") {
    qc.invalidateQueries({ queryKey: queryKeys.admin.settings });
    qc.invalidateQueries({ queryKey: queryKeys.schedulerDecisions });
    qc.invalidateQueries({ queryKey: ["gallerydl-config"] });
  }
}

export function NotificationProvider({ children }: { children: ReactNode }) {
  const t = useT();
  const qc = useQueryClient();
  const [items, setItems] = useState<ActivityItem[]>([]);
  const [batchJob, setBatchJob] = useState<BatchJobState | null>(null);
  const [operationJob, setOperationJob] = useState<OperationJobState | null>(null);
  const timersRef = useRef<Map<string, ReturnType<typeof setTimeout>>>(new Map());

  // ─── Activity log ───
  useEffect(() => {
    const timers = timersRef.current;
    return () => { timers.forEach((t) => clearTimeout(t)); timers.clear(); };
  }, []);

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

  // ─── Batch job (global, survives navigation) ───
  const startBatchJob = useCallback((jobId: string, importType: "pixiv" | "url", total: number) => {
    const state: BatchJobState = {
      jobId, importType, total,
      startedAt: Date.now(),
      progress: null, result: null,
      status: "running",
    };
    setBatchJob(state);
    try { sessionStorage.setItem(STORAGE_KEY, JSON.stringify({ jobId, importType, total, startedAt: Date.now() })); } catch {}
  }, [t]);

  const clearBatchJob = useCallback(() => {
    setBatchJob(null);
    try { sessionStorage.removeItem(STORAGE_KEY); } catch {}
  }, []);

  const startOperationJob = useCallback((
    jobId: string,
    kind: OperationJobState["kind"],
    title: string,
    meta?: Record<string, any>,
  ) => {
    const state: OperationJobState = {
      jobId, kind, title,
      startedAt: Date.now(),
      progress: null, result: null,
      status: "running",
      meta,
    };
    setOperationJob(state);
    try {
      sessionStorage.setItem(OPERATION_STORAGE_KEY, JSON.stringify({
        jobId, kind, title, meta, startedAt: state.startedAt,
      }));
    } catch {}
  }, []);

  const clearOperationJob = useCallback(() => {
    setOperationJob(null);
    try { sessionStorage.removeItem(OPERATION_STORAGE_KEY); } catch {}
  }, []);

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

  // Mount recovery: restore batch job from sessionStorage (only if recent)
  useEffect(() => {
    try {
      const stored = sessionStorage.getItem(STORAGE_KEY);
      if (stored) {
        const parsed = JSON.parse(stored);
        const age = Date.now() - parsed.startedAt;
        if (age < 5 * 60 * 1000) {
          setBatchJob({
            jobId: parsed.jobId, importType: parsed.importType, total: parsed.total,
            startedAt: parsed.startedAt, progress: null, result: null, status: "running",
          });
          // Fetch current status immediately so completed jobs show results,
          // and expired/stale jobs are cleaned up instead of leaving UI stuck.
          api.getBatchImportStatus(parsed.jobId).then((d: any) => {
            if (d?.result) {
              setBatchJob((prev) => {
                if (!prev || prev.jobId !== parsed.jobId) return prev;
                return { jobId: prev.jobId, importType: prev.importType, total: prev.total, startedAt: prev.startedAt, result: d.result, status: "completed" as const, progress: null };
              });
            } else if (!d?.progress) {
              setBatchJob(null);
              try { sessionStorage.removeItem(STORAGE_KEY); } catch {}
            }
          }).catch(() => {
            setBatchJob(null);
            try { sessionStorage.removeItem(STORAGE_KEY); } catch {}
          });
        } else {
          try { sessionStorage.removeItem(STORAGE_KEY); } catch {}
        }
      }
    } catch {}
  }, []); // eslint-disable-line

  useEffect(() => {
    try {
      const stored = sessionStorage.getItem(OPERATION_STORAGE_KEY);
      if (!stored) return;
      const parsed = JSON.parse(stored);
      const age = Date.now() - parsed.startedAt;
      if (age >= 2 * 60 * 60 * 1000) {
        sessionStorage.removeItem(OPERATION_STORAGE_KEY);
        return;
      }
      setOperationJob({
        jobId: parsed.jobId,
        kind: parsed.kind,
        title: parsed.title,
        meta: parsed.meta,
        startedAt: parsed.startedAt,
        progress: null,
        result: null,
        status: "running",
      });
    } catch {}
  }, []);

  // ─── Global polling (layout level — never unmounts on page navigation) ───
  const batchStatusQuery = useQuery({
    queryKey: ["batch-import-status-global", batchJob?.jobId],
    queryFn: () => api.getBatchImportStatus(batchJob?.jobId || undefined),
    enabled: !!batchJob?.jobId,
    staleTime: 0, // Always refetch on mount to restore result after navigation
    refetchInterval: (query) => {
      if (!batchJob) return false;
      return query.state.data?.status === "completed" ? false : 2000;
    },
  });

  const operationStatusQuery = useQuery({
    queryKey: ["admin-operation-status-global", operationJob?.jobId],
    queryFn: () => api.getAdminOperationStatus(operationJob?.jobId || ""),
    enabled: !!operationJob?.jobId && operationJob.status === "running",
    staleTime: 0,
    refetchInterval: (query) => {
      if (!operationJob) return false;
      const status = query.state.data?.status;
      return status === "complete" || status === "failed" ? false : 2000;
    },
    retry: 2,
  });

  // Sync polling results to batch job state
  useEffect(() => {
    if (!batchJob || !batchStatusQuery.data) return;
    const data = batchStatusQuery.data;

    if (data.progress) {
      setBatchJob((prev) => prev ? { jobId: prev.jobId, importType: prev.importType, total: prev.total, startedAt: prev.startedAt, progress: data.progress, result: null, status: "running" as const } : prev);
    }

    if (data.result) {
      setBatchJob((prev) => prev ? { jobId: prev.jobId, importType: prev.importType, total: prev.total, startedAt: prev.startedAt, result: data.result, status: "completed" as const, progress: null } : prev);
      refetchCreatorSubscriptionQueries(qc);
      qc.invalidateQueries({ queryKey: queryKeys.workbench });
      qc.invalidateQueries({ queryKey: ["creator-subscription-overview"] });
    }

    // Handle expired/stuck jobs: no progress AND no result means Redis keys are gone
    if (!data.progress && !data.result) {
      const elapsed = Date.now() - batchJob.startedAt;
      if (elapsed > 10 * 60 * 1000) {
        setBatchJob((prev) => prev ? { jobId: prev.jobId, importType: prev.importType, total: prev.total, startedAt: prev.startedAt, result: null, status: "error" as const, progress: null } : prev);
        try { sessionStorage.removeItem(STORAGE_KEY); } catch {}
      }
    }
  }, [batchStatusQuery.data, batchJob?.jobId]); // eslint-disable-line

  useEffect(() => {
    if (!operationJob || !operationStatusQuery.data) return;
    const data = operationStatusQuery.data;

    if (data.status === "queued" || data.status === "running") {
      setOperationJob((prev) => prev && prev.jobId === operationJob.jobId
        ? { ...prev, progress: data.progress || prev.progress, status: "running" }
        : prev);
      return;
    }

    if (data.status === "complete") {
      setOperationJob((prev) => prev && prev.jobId === operationJob.jobId
        ? { ...prev, progress: data.progress || prev.progress, result: data.result || null, status: "completed" }
        : prev);
      refreshOperationQueries(qc, operationJob.kind, operationJob.meta, data.result);
      try { sessionStorage.removeItem(OPERATION_STORAGE_KEY); } catch {}
      return;
    }

    if (data.status === "failed") {
      setOperationJob((prev) => prev && prev.jobId === operationJob.jobId
        ? { ...prev, progress: data.progress || prev.progress, error: data.error || "Operation failed", status: "error" }
        : prev);
      try { sessionStorage.removeItem(OPERATION_STORAGE_KEY); } catch {}
    }
  }, [operationStatusQuery.data, operationJob?.jobId]); // eslint-disable-line

  return (
    <NotificationContext.Provider value={{
      items, addActivity, updateActivity, removeActivity, clearRecent,
      batchJob, startBatchJob, clearBatchJob,
      operationJob, startOperationJob, clearOperationJob,
    }}>
      {children}
    </NotificationContext.Provider>
  );
}

export function useNotifications(): NotificationCtx {
  return useContext(NotificationContext);
}

// ─── Notification Bell ───
export function NotificationBell() {
  const t = useT();
  const router = useRouter();
  const {
    items, removeActivity, clearRecent,
    clearBatchJob, batchJob,
    clearOperationJob, operationJob,
  } = useNotifications();
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    function handleClick(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    }
    document.addEventListener("mousedown", handleClick);
    return () => document.removeEventListener("mousedown", handleClick);
  }, []);

  const activeCount = (batchJob?.status === "running" ? 1 : 0)
    + (operationJob?.status === "running" ? 1 : 0)
    + items.filter((a) => a.status === "running").length;
  const hasRecent = items.length > 0 || !!batchJob || !!operationJob;

  const statusIcon = (status: ActivityStatus) => {
    if (status === "running") {
      return <span className="w-2 h-2 rounded-full bg-blue-400 animate-pulse shrink-0" />;
    }
    if (status === "completed") {
      return (
        <svg className="w-4 h-4 text-green-400 shrink-0" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
          <path d="M20 6L9 17l-5-5" />
        </svg>
      );
    }
    return (
      <svg className="w-4 h-4 text-red-400 shrink-0" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
        <path d="M18 6L6 18M6 6l12 12" />
      </svg>
    );
  };

  const timeAgo = (ts: number): string => {
    const diff = Date.now() - ts;
    const sec = Math.floor(diff / 1000);
    if (sec < 60) return `${sec}s ago`;
    const min = Math.floor(sec / 60);
    if (min < 60) return `${min}m ago`;
    const hr = Math.floor(min / 60);
    return `${hr}h ago`;
  };

  return (
    <div ref={ref} className="relative">
      <button
        onClick={() => setOpen(!open)}
        className="p-1.5 rounded hover:bg-white/10 transition-colors relative"
        title={t("notification.bell_label")}
        aria-label={t("notification.bell_label")}
      >
        <svg className="w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <path d="M6 8a6 6 0 0112 0c0 7 3 9 3 9H3s3-2 3-9" />
          <path d="M10.3 21a1.94 1.94 0 003.4 0" />
        </svg>
        {activeCount > 0 && (
          <span className="absolute -top-0.5 -right-0.5 bg-red-500 text-white text-[10px] font-bold rounded-full min-w-[16px] h-4 flex items-center justify-center px-0.5 leading-none">
            {activeCount}
          </span>
        )}
      </button>

      {open && (
        <div className="absolute right-0 z-50 mt-2 max-h-[480px] w-80 overflow-hidden rounded-md border border-ag-border bg-white text-[#24292f] shadow-xl dark:border-ag-border dark:bg-ag-surface dark:text-ag-text">
          <div className="flex items-center justify-between border-b border-ag-border px-4 py-2.5 dark:border-ag-border">
            <span className="text-sm font-semibold">{t("notification.recent")}</span>
            <div className="flex items-center gap-3">
              <button onClick={() => { setOpen(false); router.push("/admin/notifications"); }}
                className="text-xs text-blue-500 hover:text-blue-700 dark:text-blue-400 dark:hover:text-blue-300 transition-colors">
                {t("notifications.title")} →
              </button>
              {(items.length > 0 || batchJob || operationJob) && (
                <button onClick={() => { clearRecent(); }}
                  className="text-xs text-[#57606a] transition-colors hover:text-[#24292f] dark:text-[#8b949e] dark:hover:text-[#e6edf3]">
                  {t("notification.clear_all")}
                </button>
              )}
            </div>
          </div>
          <div className="overflow-y-auto max-h-[420px]">
            {!hasRecent ? (
              <div className="px-4 py-8 text-center text-sm text-[#57606a] dark:text-[#8b949e]">
                {t("notification.empty")}
              </div>
            ) : (
              <>
                {batchJob && (
                  <div className="cursor-pointer border-b border-ag-border px-4 py-2.5 transition-colors hover:bg-[#f6f8fa] dark:border-ag-border dark:hover:bg-[#21262d]"
                    onClick={() => { setOpen(false); router.push("/admin/reference/danbooru"); }}>
                    <div className="flex items-start gap-2.5">
                      <div className="mt-0.5">{statusIcon(batchJob.status)}</div>
                      <div className="flex-1 min-w-0">
                        <p className="text-sm font-medium truncate">
                          {batchJob.importType === "pixiv" ? t("notification.batch_import") : "URL Batch Import"}
                        </p>
                        {batchJob.status === "error" && (
                          <p className="text-xs text-red-400 mt-0.5">Job expired or failed</p>
                        )}
                        {batchJob.progress && (
                          <>
                            <p className="mt-0.5 text-xs text-[#57606a] dark:text-[#8b949e]">
                              {batchJob.progress.current}/{batchJob.progress.total} · {batchJob.progress.imported} imported
                            </p>
                            <div className="mt-1.5 h-1 w-full overflow-hidden rounded-full bg-[#eaeef2] dark:bg-[#30363d]">
                              <div className="h-full bg-blue-500 rounded-full transition-all duration-500 ease-out"
                                style={{ width: `${(batchJob.progress.current / batchJob.progress.total) * 100}%` }} />
                            </div>
                          </>
                        )}
                        {batchJob.result && (
                          <p className="mt-0.5 text-xs text-[#57606a] dark:text-[#8b949e]">
                            Done: {batchJob.result.imported_count || batchJob.result.imported?.length || 0} imported
                          </p>
                        )}
                        <span className="text-[10px] text-[#57606a] dark:text-[#8b949e]">{timeAgo(batchJob.startedAt)}</span>
                      </div>
                      {batchJob.status !== "running" && (
                        <button
                          onClick={(e) => { e.stopPropagation(); clearBatchJob(); }}
                          className="mt-0.5 shrink-0 text-[#8b949e] hover:text-[#57606a] dark:hover:text-[#e6edf3]"
                          aria-label="Dismiss"
                        >
                          <svg className="w-3 h-3" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round">
                            <path d="M18 6L6 18M6 6l12 12" />
                          </svg>
                        </button>
                      )}
                    </div>
                  </div>
                )}
                {operationJob && (
                  <div className="cursor-pointer border-b border-ag-border px-4 py-2.5 transition-colors hover:bg-[#f6f8fa] dark:border-ag-border dark:hover:bg-[#21262d]"
                    onClick={() => {
                      setOpen(false);
                      router.push(operationJob.kind === "danbooru-import-all" ? "/admin/reference/danbooru" : "/admin/data-mgmt");
                    }}>
                    <div className="flex items-start gap-2.5">
                      <div className="mt-0.5">{statusIcon(operationJob.status)}</div>
                      <div className="flex-1 min-w-0">
                        <p className="text-sm font-medium truncate">{operationJob.title}</p>
                        {operationJob.status === "error" && (
                          <p className="text-xs text-red-400 mt-0.5 truncate">{operationJob.error || "Operation failed"}</p>
                        )}
                        {operationJob.progress?.label && operationJob.status === "running" && (
                          <p className="mt-0.5 text-xs text-[#57606a] dark:text-[#8b949e] truncate">
                            {operationJob.progress.label}
                          </p>
                        )}
                        {operationJob.progress?.total && operationJob.progress.current !== undefined && (
                          <div className="mt-1.5 h-1 w-full overflow-hidden rounded-full bg-[#eaeef2] dark:bg-[#30363d]">
                            <div className="h-full bg-blue-500 rounded-full transition-all duration-500 ease-out"
                              style={{ width: `${(operationJob.progress.current / operationJob.progress.total) * 100}%` }} />
                          </div>
                        )}
                        {operationJob.result && (
                          <p className="mt-0.5 text-xs text-[#57606a] dark:text-[#8b949e] truncate">
                            {operationResultMessage(operationJob)}
                          </p>
                        )}
                        <span className="text-[10px] text-[#57606a] dark:text-[#8b949e]">{timeAgo(operationJob.startedAt)}</span>
                      </div>
                      {operationJob.status !== "running" && (
                        <button
                          onClick={(e) => { e.stopPropagation(); clearOperationJob(); }}
                          className="mt-0.5 shrink-0 text-[#8b949e] hover:text-[#57606a] dark:hover:text-[#e6edf3]"
                          aria-label="Dismiss"
                        >
                          <svg className="w-3 h-3" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round">
                            <path d="M18 6L6 18M6 6l12 12" />
                          </svg>
                        </button>
                      )}
                    </div>
                  </div>
                )}
                {items.map((a) => (
                  <div key={a.id}
                    className={`border-b border-ag-border px-4 py-2.5 transition-colors hover:bg-[#f6f8fa] dark:border-ag-border dark:hover:bg-[#21262d] ${a.link ? "cursor-pointer" : "cursor-default"}`}
                    onClick={() => { if (a.link) { setOpen(false); router.push(a.link); } }}>
                    <div className="flex items-start gap-2.5">
                      <div className="mt-0.5">{statusIcon(a.status)}</div>
                      <div className="flex-1 min-w-0">
                        <div className="flex items-center justify-between gap-2">
                          <p className={`text-sm truncate ${a.status === "running" ? "font-medium" : ""}`}>{a.title}</p>
                          <span className="shrink-0 text-[10px] text-[#57606a] dark:text-[#8b949e]">{timeAgo(a.timestamp)}</span>
                        </div>
                        {a.message && <p className="mt-0.5 truncate text-xs text-[#57606a] dark:text-[#8b949e]">{a.message}</p>}
                        {a.status === "running" && a.progress !== undefined && (
                          <div className="mt-1.5 h-1 w-full overflow-hidden rounded-full bg-[#eaeef2] dark:bg-[#30363d]">
                            <div className="h-full bg-blue-500 rounded-full transition-all duration-500 ease-out"
                              style={{ width: `${a.progress}%` }} />
                          </div>
                        )}
                      </div>
                      <button onClick={(e) => { e.stopPropagation(); removeActivity(a.id); }}
                        className="mt-0.5 shrink-0 text-[#8b949e] hover:text-[#57606a] dark:hover:text-[#e6edf3]" aria-label="Dismiss">
                        <svg className="w-3 h-3" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round">
                          <path d="M18 6L6 18M6 6l12 12" />
                        </svg>
                      </button>
                    </div>
                  </div>
                ))}
              </>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

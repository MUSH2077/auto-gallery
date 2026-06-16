"use client";
import { createContext, useContext, useState, useCallback, useRef, useEffect, type ReactNode } from "react";
import { useRouter } from "next/navigation";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useT } from "@/lib/i18n";
import { api } from "@/lib/api";

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

const STORAGE_KEY = "danbooru_batch_job";

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
});

let _activityId = 0;
const MAX_ITEMS = 50;
const COMPLETED_TTL = 30_000;

export function NotificationProvider({ children }: { children: ReactNode }) {
  const t = useT();
  const qc = useQueryClient();
  const [items, setItems] = useState<ActivityItem[]>([]);
  const [batchJob, setBatchJob] = useState<BatchJobState | null>(null);
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

  const clearRecent = useCallback(() => {
    timersRef.current.forEach((t) => clearTimeout(t));
    timersRef.current.clear();
    setItems((prev) => prev.filter((a) => a.status === "running"));
    // Also clear completed/error batch jobs
    if (batchJob && batchJob.status !== "running") {
      clearBatchJob();
    }
  }, [batchJob, clearBatchJob]);

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

  // Sync polling results to batch job state
  useEffect(() => {
    if (!batchJob || !batchStatusQuery.data) return;
    const data = batchStatusQuery.data;

    if (data.progress) {
      setBatchJob((prev) => prev ? { jobId: prev.jobId, importType: prev.importType, total: prev.total, startedAt: prev.startedAt, progress: data.progress, result: null, status: "running" as const } : prev);
    }

    if (data.result) {
      setBatchJob((prev) => prev ? { jobId: prev.jobId, importType: prev.importType, total: prev.total, startedAt: prev.startedAt, result: data.result, status: "completed" as const, progress: null } : prev);
      qc.invalidateQueries({ queryKey: ["creators"] });
      qc.invalidateQueries({ queryKey: ["subscriptions"] });
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

  return (
    <NotificationContext.Provider value={{
      items, addActivity, updateActivity, removeActivity, clearRecent,
      batchJob, startBatchJob, clearBatchJob,
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
  const { items, removeActivity, clearRecent, clearBatchJob, batchJob } = useNotifications();
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
    + items.filter((a) => a.status === "running").length;
  const hasRecent = items.length > 0 || !!batchJob;

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
        <div className="absolute right-0 z-50 mt-2 max-h-[480px] w-80 overflow-hidden rounded-md border border-[#d8dee4] bg-white text-[#24292f] shadow-xl dark:border-[#30363d] dark:bg-[#161b22] dark:text-[#e6edf3]">
          <div className="flex items-center justify-between border-b border-[#d8dee4] px-4 py-2.5 dark:border-[#30363d]">
            <span className="text-sm font-semibold">{t("notification.recent")}</span>
            <div className="flex items-center gap-3">
              <button onClick={() => { setOpen(false); router.push("/admin/notifications"); }}
                className="text-xs text-blue-500 hover:text-blue-700 dark:text-blue-400 dark:hover:text-blue-300 transition-colors">
                {t("notifications.title")} →
              </button>
              {(items.length > 0 || batchJob) && (
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
                  <div className="cursor-pointer border-b border-[#d8dee4] px-4 py-2.5 transition-colors hover:bg-[#f6f8fa] dark:border-[#30363d] dark:hover:bg-[#21262d]"
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
                {items.map((a) => (
                  <div key={a.id}
                    className={`border-b border-[#d8dee4] px-4 py-2.5 transition-colors hover:bg-[#f6f8fa] dark:border-[#30363d] dark:hover:bg-[#21262d] ${a.link ? "cursor-pointer" : "cursor-default"}`}
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

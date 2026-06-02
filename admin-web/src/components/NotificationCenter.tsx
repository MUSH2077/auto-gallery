"use client";
import { createContext, useContext, useState, useCallback, useRef, useEffect, type ReactNode } from "react";
import { useRouter } from "next/navigation";
import { useT } from "@/lib/i18n";

type ActivityStatus = "running" | "completed" | "error";

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

interface NotificationCtx {
  items: ActivityItem[];
  addActivity: (item: Omit<ActivityItem, "id" | "timestamp">) => string;
  updateActivity: (id: string, patch: Partial<Pick<ActivityItem, "status" | "message" | "progress">>) => void;
  removeActivity: (id: string) => void;
  clearRecent: () => void;
}

const NotificationContext = createContext<NotificationCtx>({
  items: [],
  addActivity: () => "",
  updateActivity: () => {},
  removeActivity: () => {},
  clearRecent: () => {},
});

let _activityId = 0;
const MAX_ITEMS = 50;
const COMPLETED_TTL = 30_000;

export function NotificationProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<ActivityItem[]>([]);
  const timersRef = useRef<Map<string, ReturnType<typeof setTimeout>>>(new Map());

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

  const clearRecent = useCallback(() => {
    timersRef.current.forEach((t) => clearTimeout(t));
    timersRef.current.clear();
    setItems((prev) => prev.filter((a) => a.status === "running"));
  }, []);

  return (
    <NotificationContext.Provider value={{ items, addActivity, updateActivity, removeActivity, clearRecent }}>
      {children}
    </NotificationContext.Provider>
  );
}

export function useNotifications(): NotificationCtx {
  return useContext(NotificationContext);
}

/** Bell icon + badge + dropdown shown in the admin nav bar */
export function NotificationBell() {
  const t = useT();
  const router = useRouter();
  const { items, removeActivity, clearRecent } = useNotifications();
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    function handleClick(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    }
    document.addEventListener("mousedown", handleClick);
    return () => document.removeEventListener("mousedown", handleClick);
  }, []);

  const activeCount = items.filter((a) => a.status === "running").length;
  const hasRecent = items.length > 0;

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
        <div className="absolute right-0 mt-2 w-80 max-h-[480px] bg-white dark:bg-slate-800 border border-slate-200 dark:border-slate-700 rounded-lg shadow-xl z-50 text-slate-800 dark:text-slate-100 overflow-hidden">
          <div className="flex items-center justify-between px-4 py-2.5 border-b border-slate-200 dark:border-slate-700">
            <span className="text-sm font-semibold">{t("notification.recent")}</span>
            {items.length > 0 && (
              <button
                onClick={() => clearRecent()}
                className="text-xs text-slate-400 hover:text-slate-600 dark:hover:text-slate-300 transition-colors"
              >
                {t("notification.clear_all")}
              </button>
            )}
          </div>

          <div className="overflow-y-auto max-h-[420px]">
            {!hasRecent ? (
              <div className="px-4 py-8 text-center text-sm text-slate-400 dark:text-slate-500">
                {t("notification.empty")}
              </div>
            ) : (
              items.map((a) => (
                <div
                  key={a.id}
                  className={`px-4 py-2.5 border-b border-slate-100 dark:border-slate-700/50 hover:bg-slate-50 dark:hover:bg-slate-700/30 transition-colors ${
                    a.link ? "cursor-pointer" : "cursor-default"
                  }`}
                  onClick={() => {
                    if (a.link) {
                      setOpen(false);
                      router.push(a.link);
                    }
                  }}
                >
                  <div className="flex items-start gap-2.5">
                    <div className="mt-0.5">{statusIcon(a.status)}</div>
                    <div className="flex-1 min-w-0">
                      <div className="flex items-center justify-between gap-2">
                        <p className={`text-sm truncate ${a.status === "running" ? "font-medium" : ""}`}>{a.title}</p>
                        <span className="text-[10px] text-slate-400 shrink-0">{timeAgo(a.timestamp)}</span>
                      </div>
                      {a.message && (
                        <p className="text-xs text-slate-500 dark:text-slate-400 mt-0.5 truncate">{a.message}</p>
                      )}
                      {a.status === "running" && a.progress !== undefined && (
                        <div className="w-full h-1 bg-slate-200 dark:bg-slate-600 rounded-full overflow-hidden mt-1.5">
                          <div
                            className="h-full bg-blue-500 rounded-full transition-all duration-500 ease-out"
                            style={{ width: `${a.progress}%` }}
                          />
                        </div>
                      )}
                    </div>
                    <button
                      onClick={(e) => { e.stopPropagation(); removeActivity(a.id); }}
                      className="text-slate-300 hover:text-slate-500 dark:hover:text-slate-400 shrink-0 mt-0.5"
                      aria-label="Dismiss"
                    >
                      <svg className="w-3 h-3" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round">
                        <path d="M18 6L6 18M6 6l12 12" />
                      </svg>
                    </button>
                  </div>
                </div>
              ))
            )}
          </div>
        </div>
      )}
    </div>
  );
}

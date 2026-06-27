"use client";
import { createContext, useContext, useState, useCallback, type ReactNode } from "react";

type ToastType = "success" | "error" | "info" | "warning";

export interface ToastAction {
  label: string;
  onClick: () => void;
}

export interface ToastOptions {
  title?: string;
  message: string;
  duration?: number;       // ms; 0 or Infinity = persistent
  persistent?: boolean;
  action?: ToastAction;
}

interface Toast {
  id: number;
  type: ToastType;
  title?: string;
  message: string;
  duration: number;        // resolved ms; Infinity = persistent
  action?: ToastAction;
  progress?: number;       // 0-100, undefined = no progress bar
  exiting: boolean;
  entering: boolean;
}

export interface ToastCtx {
  toast: (type: ToastType, options: ToastOptions | string) => number;
  success: (options: ToastOptions | string) => number;
  error: (options: ToastOptions | string) => number;
  info: (options: ToastOptions | string) => number;
  warning: (options: ToastOptions | string) => number;
  dismiss: (id: number) => void;
  updateProgress: (id: number, progress: number) => void;
}

const ToastContext = createContext<ToastCtx>({
  toast: () => 0,
  success: () => 0, error: () => 0, info: () => 0, warning: () => 0,
  dismiss: () => {}, updateProgress: () => {},
});

let _id = 0;

const DEFAULT_DURATION = 3500;

function resolveOptions(input: ToastOptions | string): ToastOptions {
  if (typeof input === "string") return { message: input };
  return input;
}

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);

  const dismiss = useCallback((id: number) => {
    setToasts((prev) => prev.map((t) => (t.id === id ? { ...t, exiting: true } : t)));
    setTimeout(() => setToasts((prev) => prev.filter((t) => t.id !== id)), 300);
  }, []);

  const add = useCallback((type: ToastType, input: ToastOptions | string) => {
    const opts = resolveOptions(input);
    const id = ++_id;
    let duration = opts.duration ?? DEFAULT_DURATION;
    if (opts.persistent) duration = Infinity;

    const toast: Toast = {
      id, type,
      title: opts.title,
      message: opts.message,
      duration,
      action: opts.action,
      entering: true,
      exiting: false,
    };

    setToasts((prev) => [...prev.slice(-5), toast]);

    // Trigger enter animation on next frame
    requestAnimationFrame(() => {
      setToasts((prev) => prev.map((t) => (t.id === id ? { ...t, entering: false } : t)));
    });

    if (duration !== Infinity) {
      setTimeout(() => dismiss(id), duration);
    }

    return id;
  }, [dismiss]);

  const updateProgress = useCallback((id: number, progress: number) => {
    setToasts((prev) => prev.map((t) => (t.id === id ? { ...t, progress: Math.max(0, Math.min(100, progress)) } : t)));
  }, []);

  const ctx: ToastCtx = {
    toast: (type, opts) => add(type, opts),
    success: (opts) => add("success", opts),
    error: (opts) => add("error", opts),
    info: (opts) => add("info", opts),
    warning: (opts) => add("warning", opts),
    dismiss,
    updateProgress,
  };

  const toneClasses: Record<ToastType, string> = {
    success: "border-success/30 bg-success-subtle text-success dark:border-success/30 dark:bg-success-subtle dark:text-success",
    error: "border-danger/30 bg-danger-subtle text-danger dark:border-danger/30 dark:bg-danger-subtle dark:text-danger",
    info: "border-accent/30 bg-accent-subtle text-accent dark:border-accent/30 dark:bg-accent-subtle dark:text-accent",
    warning: "border-warning/30 bg-warning-subtle text-warning dark:border-warning/30 dark:bg-warning-subtle dark:text-warning",
  };

  const icons: Record<ToastType, string> = {
    success: "✓", error: "✗", info: "ℹ", warning: "⚠",
  };

  const progressColors: Record<ToastType, string> = {
    success: "bg-success dark:bg-success",
    error: "bg-danger dark:bg-danger",
    info: "bg-accent dark:bg-accent",
    warning: "bg-warning dark:bg-warning",
  };

  return (
    <ToastContext.Provider value={ctx}>
      {children}
      <div className="fixed bottom-4 right-4 z-50 flex max-w-sm flex-col gap-2" role="log" aria-live="polite">
        {toasts.map((t) => (
          <div
            key={t.id}
            className={`${toneClasses[t.type]} flex flex-col gap-1 rounded-md border px-4 py-3 text-sm shadow-lg transition-all duration-300 ${
              t.entering ? "opacity-0 translate-x-4" : t.exiting ? "opacity-0 -translate-x-4" : "opacity-100 translate-x-0"
            }`}
            role="status"
          >
            <div className="flex items-center gap-2">
              <span className="font-bold shrink-0">{icons[t.type]}</span>
              <div className="flex-1 min-w-0">
                {t.title && <p className="font-semibold text-xs leading-tight truncate">{t.title}</p>}
                <p className={`${t.title ? "text-xs opacity-90" : "text-sm"} leading-tight`}>{t.message}</p>
              </div>
              <div className="flex items-center gap-1 shrink-0">
                {t.action && (
                  <button
                    onClick={() => { t.action!.onClick(); dismiss(t.id); }}
                    className="rounded-md border border-current/25 px-2 py-0.5 text-xs font-medium transition-colors hover:bg-white/40 dark:hover:bg-white/10"
                  >
                    {t.action.label}
                  </button>
                )}
                <button
                  onClick={() => dismiss(t.id)}
                  className="ml-1 text-lg leading-none opacity-60 hover:opacity-100"
                  aria-label="Dismiss"
                >
                  &times;
                </button>
              </div>
            </div>
            {t.progress !== undefined && (
              <div className="h-1 w-full overflow-hidden rounded-full bg-current/15">
                <div
                  className={`h-full ${progressColors[t.type]} rounded-full transition-all duration-300 ease-out`}
                  style={{ width: `${t.progress}%` }}
                />
              </div>
            )}
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  );
}

export function useToast(): ToastCtx {
  return useContext(ToastContext);
}

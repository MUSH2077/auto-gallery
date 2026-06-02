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

  const bgColors: Record<ToastType, string> = {
    success: "bg-green-600", error: "bg-red-600", info: "bg-blue-600", warning: "bg-yellow-500",
  };

  const icons: Record<ToastType, string> = {
    success: "✓", error: "✗", info: "ℹ", warning: "⚠",
  };

  const textColors: Record<ToastType, string> = {
    success: "text-white", error: "text-white", info: "text-white", warning: "text-black",
  };

  const progressColors: Record<ToastType, string> = {
    success: "bg-green-400", error: "bg-red-400", info: "bg-blue-400", warning: "bg-yellow-600",
  };

  return (
    <ToastContext.Provider value={ctx}>
      {children}
      <div className="fixed bottom-4 right-4 z-50 flex flex-col gap-2 max-w-sm" role="log" aria-live="polite">
        {toasts.map((t) => (
          <div
            key={t.id}
            className={`${bgColors[t.type]} ${textColors[t.type]} px-4 py-3 rounded-lg shadow-lg text-sm flex flex-col gap-1 transition-all duration-300 ${
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
                    className="px-2 py-0.5 rounded bg-white/20 hover:bg-white/30 text-xs font-medium transition-colors"
                  >
                    {t.action.label}
                  </button>
                )}
                <button
                  onClick={() => dismiss(t.id)}
                  className="opacity-60 hover:opacity-100 ml-1 text-lg leading-none"
                  aria-label="Dismiss"
                >
                  &times;
                </button>
              </div>
            </div>
            {t.progress !== undefined && (
              <div className="w-full h-1 bg-white/20 rounded-full overflow-hidden">
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

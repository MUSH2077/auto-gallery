"use client";
import { createContext, useContext, useState, useCallback, type ReactNode } from "react";

type ToastType = "success" | "error" | "info" | "warning";

interface Toast {
  id: number;
  type: ToastType;
  message: string;
  exiting: boolean;
}

interface ToastCtx {
  success: (msg: string) => void;
  error: (msg: string) => void;
  info: (msg: string) => void;
  warning: (msg: string) => void;
}

const ToastContext = createContext<ToastCtx>({
  success: () => {}, error: () => {}, info: () => {}, warning: () => {},
});

let _id = 0;

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);

  const add = useCallback((type: ToastType, message: string) => {
    const id = ++_id;
    setToasts((prev) => [...prev.slice(-5), { id, type, message, exiting: false }]);
    setTimeout(() => {
      setToasts((prev) => prev.map((t) => (t.id === id ? { ...t, exiting: true } : t)));
      setTimeout(() => setToasts((prev) => prev.filter((t) => t.id !== id)), 300);
    }, 3500);
  }, []);

  const ctx: ToastCtx = {
    success: (msg) => add("success", msg),
    error: (msg) => add("error", msg),
    info: (msg) => add("info", msg),
    warning: (msg) => add("warning", msg),
  };

  const colors: Record<ToastType, string> = {
    success: "bg-green-600", error: "bg-red-600", info: "bg-blue-600", warning: "bg-yellow-500 text-black",
  };
  const icons: Record<ToastType, string> = {
    success: "✓", error: "✗", info: "ℹ", warning: "⚠",
  };

  return (
    <ToastContext.Provider value={ctx}>
      {children}
      <div className="fixed bottom-4 right-4 z-50 flex flex-col gap-2 max-w-sm">
        {toasts.map((t) => (
          <div
            key={t.id}
            className={`${colors[t.type]} text-white px-4 py-3 rounded-lg shadow-lg text-sm flex items-center gap-2 transition-all duration-300 ${t.exiting ? "opacity-0 translate-x-4" : "opacity-100"}`}
          >
            <span className="font-bold shrink-0">{icons[t.type]}</span>
            <span className="flex-1">{t.message}</span>
            <button onClick={() => setToasts((prev) => prev.filter((x) => x.id !== t.id))} className="shrink-0 opacity-60 hover:opacity-100 ml-2">×</button>
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  );
}

export function useToast() {
  return useContext(ToastContext);
}

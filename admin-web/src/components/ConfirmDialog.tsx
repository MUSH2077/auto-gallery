"use client";
import { useT } from "@/lib/i18n";

export default function ConfirmDialog({ open, title, message, onConfirm, onCancel, isPending, error }: {
  open: boolean;
  title: string;
  message: string;
  onConfirm: () => void;
  onCancel: () => void;
  isPending?: boolean;
  error?: string | null;
}) {
  const t = useT();
  if (!open) return null;
  return (
    <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
      <div className="bg-white dark:bg-slate-800 rounded-lg shadow-xl max-w-md w-full mx-4 p-6">
        <h3 className="text-lg font-semibold mb-2 dark:text-white">{title}</h3>
        <p className="text-sm text-gray-600 dark:text-gray-300 mb-4">{message}</p>
        {error && <p className="text-red-600 dark:text-red-400 text-sm mb-3 bg-red-50 dark:bg-red-900/30 border border-red-200 dark:border-red-800 rounded p-2">{error}</p>}
        <div className="flex justify-end gap-3">
          <button onClick={onCancel} disabled={isPending} className="px-4 py-2 text-sm rounded border dark:border-slate-600 hover:bg-gray-50 dark:hover:bg-slate-700 dark:text-gray-300 disabled:opacity-50">{t("common.cancel")}</button>
          <button onClick={onConfirm} disabled={isPending}
            className="px-4 py-2 text-sm rounded bg-red-600 text-white hover:bg-red-700 disabled:opacity-50">
            {isPending ? t("common.processing") : t("common.confirm")}
          </button>
        </div>
      </div>
    </div>
  );
}

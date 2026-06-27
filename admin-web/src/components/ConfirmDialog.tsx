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
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 px-4">
      <div className="w-full max-w-md rounded-md border border-border bg-white shadow-xl dark:border-border dark:bg-surface">
        <div className="border-b border-border px-4 py-3 dark:border-border">
          <h3 className="text-base font-semibold text-fg">{title}</h3>
        </div>
        <div className="px-4 py-4">
        <p className="mb-4 text-sm leading-6 text-muted">{message}</p>
        {error && <p className="mb-3 rounded-md border border-danger/30 bg-danger-subtle p-2 text-sm text-danger dark:border-danger/30 dark:bg-danger-subtle dark:text-danger">{error}</p>}
        <div className="flex justify-end gap-3">
          <button onClick={onCancel} disabled={isPending} className="btn-ghost">{t("common.cancel")}</button>
          <button onClick={onConfirm} disabled={isPending}
            className="btn-danger">
            {isPending ? t("common.processing") : t("common.confirm")}
          </button>
        </div>
        </div>
      </div>
    </div>
  );
}

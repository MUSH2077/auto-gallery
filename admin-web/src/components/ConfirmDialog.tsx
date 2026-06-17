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
      <div className="w-full max-w-md rounded-md border border-ag-border bg-white shadow-xl dark:border-ag-border dark:bg-ag-surface">
        <div className="border-b border-ag-border px-4 py-3 dark:border-ag-border">
          <h3 className="text-base font-semibold text-[#24292f] dark:text-ag-text">{title}</h3>
        </div>
        <div className="px-4 py-4">
        <p className="mb-4 text-sm leading-6 text-[#57606a] dark:text-[#8b949e]">{message}</p>
        {error && <p className="mb-3 rounded-md border border-[#cf222e]/30 bg-[#ffebe9] p-2 text-sm text-[#cf222e] dark:border-[#f85149]/30 dark:bg-[#f8514926] dark:text-[#f85149]">{error}</p>}
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

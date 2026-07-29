"use client";
import { useT } from "@/lib/i18n";
import Modal from "@/components/Modal";

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
  return (
    <Modal open={open} onClose={onCancel} title={title}>
      <p className="mb-4 text-sm leading-6 text-muted">{message}</p>
      {error && <p role="alert" className="mb-3 rounded-md border border-danger/30 bg-danger-subtle p-2 text-sm text-danger dark:border-danger/30 dark:bg-danger-subtle dark:text-danger">{error}</p>}
      <div className="flex flex-wrap justify-end gap-3">
          <button type="button" onClick={onCancel} disabled={isPending} className="btn-ghost">{t("common.cancel")}</button>
          <button type="button" onClick={onConfirm} disabled={isPending}
            className="btn-danger">
            {isPending ? t("common.processing") : t("common.confirm")}
          </button>
      </div>
    </Modal>
  );
}

"use client";
import { type ReactNode, useEffect, useId, useState } from "react";
import { useT } from "@/lib/i18n";
import Modal from "@/components/Modal";

export default function ConfirmDialog({ open, title, message, onConfirm, onCancel, isPending, error, confirmationPhrase, confirmDisabled, children }: {
  open: boolean;
  title: string;
  message: string;
  onConfirm: () => void;
  onCancel: () => void;
  isPending?: boolean;
  error?: string | null;
  confirmationPhrase?: string;
  confirmDisabled?: boolean;
  children?: ReactNode;
}) {
  const t = useT();
  const inputId = useId();
  const [confirmation, setConfirmation] = useState("");
  useEffect(() => setConfirmation(""), [confirmationPhrase, open]);
  const confirmationMatches = !confirmationPhrase || confirmation === confirmationPhrase;
  return (
    <Modal open={open} onClose={onCancel} title={title}>
      <p className="mb-4 text-sm leading-6 text-muted">{message}</p>
      {confirmationPhrase && (
        <div className="mb-4">
          <label htmlFor={inputId} className="mb-1.5 block text-sm text-fg">
            {t("common.type_to_confirm", { phrase: confirmationPhrase })}
          </label>
          <input
            id={inputId}
            autoComplete="off"
            spellCheck={false}
            value={confirmation}
            onChange={(event) => setConfirmation(event.target.value)}
            className="min-h-11 w-full rounded-md border border-danger bg-surface px-3 font-mono text-sm"
          />
        </div>
      )}
      {children}
      {error && <p role="alert" className="mb-3 rounded-md border border-danger/30 bg-danger-subtle p-2 text-sm text-danger dark:border-danger/30 dark:bg-danger-subtle dark:text-danger">{error}</p>}
      <div className="flex flex-wrap justify-end gap-3">
          <button type="button" onClick={onCancel} disabled={isPending} className="btn-ghost">{t("common.cancel")}</button>
          <button type="button" onClick={onConfirm} disabled={isPending || confirmDisabled || !confirmationMatches}
            className="btn-danger">
            {isPending ? t("common.processing") : t("common.confirm")}
          </button>
      </div>
    </Modal>
  );
}

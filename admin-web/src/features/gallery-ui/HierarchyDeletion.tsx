"use client";

import { useId } from "react";
import type { DeletionPreview } from "@/lib/api";
import { useT } from "@/lib/i18n";
import { BusinessConfirm } from "@/features/gallery-ui/BusinessPage";

type Props = {
  open: boolean;
  title: string;
  message?: string;
  confirmationPhrase: string;
  preview?: DeletionPreview;
  previewLoading?: boolean;
  deleteFiles: boolean;
  onDeleteFilesChange: (value: boolean) => void;
  onConfirm: () => void;
  onCancel: () => void;
  isPending?: boolean;
  error?: string | null;
};

export default function BusinessHierarchyDeletion({ open, title, message, confirmationPhrase, preview, previewLoading, deleteFiles, onDeleteFilesChange, onConfirm, onCancel, isPending, error }: Props) {
  const t = useT();
  const checkboxId = useId();
  const isPermanent = preview?.mode === "permanent";
  const activeTaskCount = preview?.active_task_count || 0;
  const hasActiveTasks = activeTaskCount > 0;
  return (
    <BusinessConfirm
      open={open}
      title={title}
      message={message || (isPermanent ? t("deletion.permanent_message") : t("deletion.soft_message"))}
      confirmationPhrase={isPermanent ? confirmationPhrase : undefined}
      onConfirm={onConfirm}
      onCancel={onCancel}
      isPending={isPending || previewLoading}
      confirmDisabled={hasActiveTasks || !preview}
      error={error}
    >
      {previewLoading ? (
        <div className="mb-4 grid grid-cols-2 gap-2" aria-label={t("common.loading")}>
          {Array.from({ length: 4 }).map((_, index) => <div key={index} className="h-14 animate-pulse rounded-md bg-subtle" />)}
        </div>
      ) : preview ? (
        <div className="mb-4 space-y-3">
          <dl className="grid grid-cols-2 gap-2 text-sm">
            {[
              [t("deletion.affected_works"), preview.affected_work_count],
              [t("deletion.exclusive_works"), preview.exclusive_work_count],
              [t("deletion.shared_works"), preview.shared_work_count],
              [t("deletion.exclusive_assets"), preview.exclusive_asset_count],
            ].map(([label, value]) => <div key={String(label)} className="rounded-md border border-border p-2"><dt className="text-xs text-muted">{label}</dt><dd className="font-mono font-semibold">{value}</dd></div>)}
          </dl>
          <p className="text-xs leading-5 text-muted">{t("deletion.shared_preserved")}</p>
          {hasActiveTasks && <p role="alert" className="rounded-md border border-warning/30 bg-warning-subtle p-2 text-sm text-warning">{t("deletion.active_tasks", { count: activeTaskCount })}</p>}
          {preview.can_delete_files && <label htmlFor={checkboxId} className="flex items-start gap-3 rounded-md border border-danger/30 bg-danger-subtle p-3"><input id={checkboxId} type="checkbox" checked={deleteFiles} onChange={(event) => onDeleteFilesChange(event.target.checked)} className="mt-0.5 rounded border-danger" /><span><span className="block text-sm font-medium text-danger">{t("deletion.delete_files")}</span><span className="mt-1 block text-xs leading-5 text-danger/80">{t("deletion.delete_files_desc")}</span></span></label>}
        </div>
      ) : null}
    </BusinessConfirm>
  );
}
"use client";

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import ConfirmDialog from "@/components/ConfirmDialog";
import { useToast } from "@/components/Toast";
import { api, queryKeys, type ClearEntity } from "@/lib/api";
import { useT } from "@/lib/i18n";
import { usePermissions } from "@/lib/usePermissions";

export default function DomainDangerZone({
  entity,
  title,
  description,
}: {
  entity: ClearEntity;
  title: string;
  description: string;
}) {
  const t = useT();
  const { has } = usePermissions();
  const toast = useToast();
  const qc = useQueryClient();
  const [open, setOpen] = useState(false);
  const preview = useQuery({
    queryKey: ["clear-impact-preview", entity],
    queryFn: () => api.previewClearEntity(entity),
    enabled: open && has("system"),
    staleTime: 30_000,
  });
  const clear = useMutation({
    mutationFn: () => {
      if (!preview.data) throw new Error(t("common.loading"));
      return api.startClearOperation(entity, preview.data.confirmation_phrase);
    },
    onSuccess: async () => {
      setOpen(false);
      toast.success(t("datamgmt.action_queued", { action: title }));
      await qc.invalidateQueries({ queryKey: queryKeys.tasks.all });
    },
    onError: (error: Error) => toast.error(error.message),
  });

  if (!has("system")) return null;
  const impact = preview.data
    ? Object.entries(preview.data.counts).map(([name, count]) => `${name}: ${count}`).join(" · ")
    : t("common.loading");

  return (
    <section className="mt-10 border-t border-danger/30 pt-5" aria-labelledby={`danger-${entity}`}>
      <div className="flex flex-col gap-4 rounded-md border border-danger/30 bg-danger-subtle/30 p-4 sm:flex-row sm:items-center sm:justify-between">
        <div className="min-w-0">
          <h2 id={`danger-${entity}`} className="font-semibold text-danger">{title}</h2>
          <p className="mt-1 text-sm text-muted">{description}</p>
        </div>
        <button type="button" className="btn-danger min-h-11 shrink-0 px-4" onClick={() => setOpen(true)}>
          {title}
        </button>
      </div>
      {open && (
        <ConfirmDialog
          open
          title={title}
          message={`${description} ${impact}`}
          confirmationPhrase={preview.data?.confirmation_phrase}
          onConfirm={() => clear.mutate()}
          onCancel={() => setOpen(false)}
          isPending={clear.isPending || preview.isLoading}
          error={(preview.error as Error | null)?.message || (clear.error as Error | null)?.message}
        />
      )}
    </section>
  );
}

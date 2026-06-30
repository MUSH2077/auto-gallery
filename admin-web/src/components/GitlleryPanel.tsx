"use client";

import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api, queryKeys } from "@/lib/api";
import { useT } from "@/lib/i18n";
import { useToast } from "@/components/Toast";

// Static lookup map — Tailwind's content scanner can't see template-constructed
// class names (e.g. `bg-${tone}-subtle`), so the literal classes must appear in source.
const TONE: Record<"clean" | "behind", string> = {
  clean: "bg-success-subtle text-success",
  behind: "bg-warning-subtle text-warning",
};

export default function GitlleryPanel() {
  const t = useT();
  const toast = useToast();
  const qc = useQueryClient();

  const status = useQuery({
    queryKey: queryKeys.gitllery.status,
    queryFn: () => api.gitlleryStatus(),
  });

  const reconcile = useMutation({
    mutationFn: () => api.gitlleryReconcile(),
    onSuccess: () => {
      toast.success(t("gitllery.reconciled"));
      qc.invalidateQueries({ queryKey: ["gitllery"] });
    },
    onError: (e: Error) => toast.error(e.message),
  });

  if (status.isLoading) {
    return <div className="card h-20 animate-pulse p-4" />;
  }
  if (status.error) {
    return <div className="card p-4 text-sm text-danger">{(status.error as Error).message}</div>;
  }

  const s = status.data!;
  const behind = s.behind_total;
  const tone: "clean" | "behind" = behind === 0 ? "clean" : "behind";

  return (
    <section className="card p-4">
      <div className="flex items-center justify-between">
        <h2 className="text-sm font-semibold">{t("gitllery.title")}</h2>
        <span className={`badge ${TONE[tone]}`}>
          {behind === 0 ? t("gitllery.clean") : t("gitllery.behind", { count: behind })}
        </span>
      </div>
      <div className="mt-2 text-xs text-muted">
        {t("gitllery.repos", { count: s.repositories.length })} · {t("gitllery.missing", { count: s.missing_repos })}
      </div>
      <button
        onClick={() => reconcile.mutate()}
        disabled={reconcile.isPending}
        className="btn-ghost mt-3 px-3 py-1.5 text-xs"
      >
        {reconcile.isPending ? t("common.saving") : t("gitllery.reconcile")}
      </button>
    </section>
  );
}

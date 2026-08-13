"use client";

import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { api, queryKeys } from "@/lib/api";
import { adminRoutes } from "@/lib/adminRoutes";
import { useT } from "@/lib/i18n";

// Static lookup map — Tailwind's content scanner can't see template-constructed
// class names (e.g. `bg-${tone}-subtle`), so the literal classes must appear in source.
const TONE: Record<"clean" | "behind" | "sync", string> = {
  clean: "bg-success-subtle text-success",
  behind: "bg-warning-subtle text-warning",
  sync: "bg-accent-subtle text-accent",
};

export default function GitlleryPanel({ creatorId }: { creatorId?: string }) {
  const t = useT();

  const status = useQuery({
    queryKey: queryKeys.gitllery.status,
    queryFn: () => api.gitlleryStatus(),
    // status is server-cached (30s) and invalidated by reconcile/rebuild —
    // don't refire it on every mount/focus.
    staleTime: 60_000,
    refetchOnWindowFocus: false,
  });

  if (status.isLoading) {
    return <div className="card h-20 animate-pulse p-4" />;
  }
  if (status.error) {
    return <div className="card p-4 text-sm text-danger">{(status.error as Error).message}</div>;
  }

  const s = status.data!;
  const behind = s.behind_total;
  const needsSync = !!s.needs_reconcile;
  const tone: "clean" | "behind" | "sync" = needsSync ? "sync" : behind === 0 ? "clean" : "behind";

  return (
    <section className="card p-4">
      <div className="flex items-center justify-between">
        <h2 className="text-sm font-semibold">{t("gitllery.title")}</h2>
        <span className={`badge transition-colors duration-slow ${TONE[tone]}`}>
          {needsSync
            ? t("gitllery.needs_reconcile")
            : behind === 0 ? t("gitllery.clean") : t("gitllery.behind", { count: behind })}
        </span>
      </div>
      <div className="mt-2 text-xs text-muted">
        {t("gitllery.repos", { count: s.repositories.length })} · {t("gitllery.missing", { count: s.missing_repos })}
      </div>
      <div className="mt-1 text-xs text-muted">
        {t("gitllery.product_name")} {s.product_version} · {s.format_id} r{s.format_revision} · {s.projection_mode}
      </div>
      <div className="mt-3 flex gap-2">
        {creatorId && (
          <Link
            href={`${adminRoutes.curation}?subject_type=creator&subject_id=${encodeURIComponent(creatorId)}`}
            className="btn-ghost px-3 py-1.5 text-xs"
          >
            {t("gitllery.open_log")}
          </Link>
        )}
        <Link href="/admin/settings/gitllery" className="btn-ghost px-3 py-1.5 text-xs">
          {t("gitllery.open_settings")}
        </Link>
      </div>
    </section>
  );
}

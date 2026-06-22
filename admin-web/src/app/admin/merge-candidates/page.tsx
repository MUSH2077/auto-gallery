"use client";
import { useQuery } from "@tanstack/react-query";
import { api, queryKeys } from "@/lib/api";
import { PageHeader, EmptyState, ErrorState, SourceBadge } from "@/components";
import { useRouter } from "next/navigation";
import { useT } from "@/lib/i18n";

export default function MergeCandidatesPage() {
  const t = useT();
  const router = useRouter();
  const mc = useQuery({ queryKey: queryKeys.dedup.mergeCandidates, queryFn: api.listMergeCandidates });

  return (
    <main className="max-w-4xl mx-auto p-6">
      <PageHeader title={t("merge.title")} description={t("merge.desc")} />

      <div className="mb-6 rounded-md border border-[#fff8c5] bg-[#fff8c5] p-4 text-sm text-[#9a6700] dark:border-[#d29922]/30 dark:bg-[#d29922]/15 dark:text-[#f2cc60]">
        <strong>{t("merge.warning")}</strong> {t("merge.warning_detail")}
      </div>

      {mc.isLoading && <div className="animate-pulse space-y-2">{Array.from({ length: 3 }).map((_, i) => <div key={i} className="h-16 rounded-md bg-[#eaeef2] dark:bg-[#21262d]" />)}</div>}
      {mc.error && <ErrorState message={(mc.error as Error).message} />}

      {mc.data && mc.data.candidates.length === 0 && (
        <EmptyState title={t("merge.no_candidates")} description={t("merge.no_candidates_desc")} />
      )}

      {mc.data?.candidates.map((c, i) => (
        <div key={i} className="card mb-2 p-4 text-sm">
          <div className="font-medium mb-2">{c.title}</div>
          <div className="flex items-center gap-2 mb-2">
            {c.sources.map((s) => <SourceBadge key={s} source={s} />)}
            <span className="text-xs text-[#57606a] dark:text-[#8b949e]">{c.source_count} {t("merge.sources_count")}</span>
          </div>
          <div className="flex gap-2 flex-wrap">
            {c.work_ids.map((wid) => (
              <button key={wid} onClick={() => router.push(`/admin/works/${wid}`)}
                className="text-xs text-blue-600 hover:underline font-mono">{wid.slice(0, 8)}</button>
            ))}
          </div>
        </div>
      ))}
    </main>
  );
}

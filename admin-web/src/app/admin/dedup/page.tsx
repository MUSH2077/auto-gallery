"use client";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { PageHeader, EmptyState, ErrorState } from "@/components";
import { useRouter } from "next/navigation";
import { useT } from "@/lib/i18n";

export default function DedupPage() {
  const t = useT();
  const router = useRouter();
  const qc = useQueryClient();
  const dups = useQuery({ queryKey: ["dedup-duplicates"], queryFn: api.listDuplicates });
  const scan = useMutation({ mutationFn: api.scanDuplicates, onSuccess: () => qc.invalidateQueries({ queryKey: ["dedup-duplicates"] }) });

  return (
    <main className="max-w-4xl mx-auto p-6">
      <PageHeader title={t("dedup_scan.title")} description={t("dedup_scan.desc")}>
        <button onClick={() => scan.mutate()} disabled={scan.isPending}
          className="px-4 py-2 bg-slate-900 dark:bg-slate-700 text-white rounded text-sm hover:bg-slate-800 dark:hover:bg-slate-600 disabled:opacity-50">
          {scan.isPending ? t("dedup_scan.scanning") : t("dedup_scan.scan_now")}
        </button>
      </PageHeader>

      <div className="bg-yellow-50 dark:bg-yellow-900/30 border border-yellow-200 dark:border-yellow-800 rounded-lg p-4 text-sm mb-6">
        <strong>{t("dedup_scan.warning")}</strong> {t("dedup_scan.warning_detail")}
      </div>

      {dups.isLoading && <div className="animate-pulse space-y-2">{Array.from({ length: 3 }).map((_, i) => <div key={i} className="h-12 bg-gray-100 dark:bg-slate-700 rounded" />)}</div>}
      {dups.error && <ErrorState message={(dups.error as Error).message} />}

      {dups.data && dups.data.duplicates.length === 0 && (
        <EmptyState title={t("dedup_scan.no_duplicates")} description={t("dedup_scan.no_duplicates_desc")} />
      )}

      {dups.data?.duplicates.map((d, i) => (
        <div key={i} className="bg-white dark:bg-slate-800 rounded-lg shadow p-4 mb-2 text-sm">
          <div className="flex items-center justify-between mb-2">
            <span className="font-mono text-xs text-gray-500 dark:text-gray-400">{d.source}:{d.source_work_id}</span>
            <span className="text-xs bg-red-100 dark:bg-red-900/30 text-red-700 dark:text-red-400 px-2 py-0.5 rounded">{d.count} {t("dedup_scan.duplicates")}</span>
          </div>
          <div className="flex gap-2 flex-wrap">
            {d.work_ids.map((wid) => (
              <button key={wid} onClick={() => router.push(`/admin/works/${wid}`)}
                className="text-xs text-blue-600 hover:underline font-mono">{wid.slice(0, 8)}</button>
            ))}
          </div>
        </div>
      ))}
    </main>
  );
}

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
          className="btn-primary">
          {scan.isPending ? t("dedup_scan.scanning") : t("dedup_scan.scan_now")}
        </button>
      </PageHeader>

      <div className="mb-6 rounded-md border border-[#fff8c5] bg-[#fff8c5] p-4 text-sm text-[#9a6700] dark:border-[#d29922]/30 dark:bg-[#d29922]/15 dark:text-[#f2cc60]">
        <strong>{t("dedup_scan.warning")}</strong> {t("dedup_scan.warning_detail")}
      </div>

      {dups.isLoading && <div className="animate-pulse space-y-2">{Array.from({ length: 3 }).map((_, i) => <div key={i} className="h-12 rounded-md bg-[#eaeef2] dark:bg-[#21262d]" />)}</div>}
      {dups.error && <ErrorState message={(dups.error as Error).message} />}

      {dups.data && dups.data.duplicates.length === 0 && (
        <EmptyState title={t("dedup_scan.no_duplicates")} description={t("dedup_scan.no_duplicates_desc")} />
      )}

      {dups.data?.duplicates.map((d, i) => (
        <div key={i} className="card mb-2 p-4 text-sm">
          <div className="flex items-center justify-between mb-2">
            <span className="font-mono text-xs text-[#57606a] dark:text-[#8b949e]">{d.source}:{d.source_work_id}</span>
            <span className="badge border-[#ffebe9] bg-[#ffebe9] text-[#cf222e] dark:border-[#da3633]/30 dark:bg-[#da3633]/15 dark:text-[#ff7b72]">{d.count} {t("dedup_scan.duplicates")}</span>
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

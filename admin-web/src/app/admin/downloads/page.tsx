"use client";
import { useMemo, useState, useEffect, Suspense } from "react";
import { useT } from "@/lib/i18n";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api, queryKeys, DownloadJob } from "@/lib/api";
import Link from "next/link";
import { PageHeader, StatusBadge, SourceBadge, EmptyState, ErrorState, ConfirmDialog } from "@/components";
import { useRouter, useSearchParams, usePathname } from "next/navigation";

const STATUS_OPTIONS = ["", "pending", "downloading", "downloaded", "importing", "complete", "failed", "stale"];

function JobRow({ job }: { job: DownloadJob }) {
  const t = useT();
  const [showLog, setShowLog] = useState(false);
  const [confirmRetry, setConfirmRetry] = useState(false);
  const qc = useQueryClient();

  const retry = useMutation({
    mutationFn: () => api.retryDownloadJob(job.id),
    onSuccess: () => { qc.invalidateQueries({ queryKey: queryKeys.downloadJobs.all }); setConfirmRetry(false); },
  });

  return (
    <>
      <tr className="table-row">
        <td className="px-4 py-3">
          <div className="font-mono text-xs text-[#57606a] dark:text-[#8b949e]">{job.id.slice(0, 8)}</div>
        </td>
        <td className="px-4 py-3"><SourceBadge source={job.source} /></td>
        <td className="px-4 py-3"><StatusBadge status={job.status} /></td>
        <td className="px-4 py-3 text-xs text-[#57606a] dark:text-[#8b949e]">{job.retry_count}/3</td>
        <td className="px-4 py-3 text-xs text-[#57606a] dark:text-[#8b949e]">{new Date(job.created_at).toLocaleString()}</td>
        <td className="px-4 py-3">
          <div className="flex gap-2">
            {job.error_log && <button onClick={() => setShowLog(!showLog)} className="text-xs text-blue-600 hover:underline">{t("downloads.log")}</button>}
            {(job.status === "failed" || job.status === "stale") && (
              <button onClick={() => setConfirmRetry(true)} disabled={retry.isPending}
                className="rounded-md border border-[#0969da]/30 bg-[#ddf4ff] px-2 py-0.5 text-xs text-[#0969da] hover:bg-[#b6e3ff] disabled:opacity-50 dark:bg-[#1f6feb26] dark:text-[#58a6ff]">
                {retry.isPending ? "..." : t("downloads.retry")}
              </button>
            )}
          </div>
        </td>
      </tr>
      {showLog && job.error_log && (
        <tr><td colSpan={6} className="bg-[#f6f8fa] px-4 py-3 dark:bg-[#21262d]"><pre className="max-h-48 overflow-auto rounded-md border border-[#d8dee4] bg-white p-3 font-mono text-xs whitespace-pre-wrap dark:border-[#30363d] dark:bg-[#0d1117]">{job.error_log}</pre></td></tr>
      )}
      {confirmRetry && <ConfirmDialog open title={t("downloads.retry_title")} message={t("downloads.retry_msg").replace("{id}", job.id.slice(0, 8))} onConfirm={() => retry.mutate()} onCancel={() => setConfirmRetry(false)} isPending={retry.isPending} error={(retry.error as Error)?.message} />}
    </>
  );
}

function DownloadsContent() {
  const t = useT();
  const sp = useSearchParams();
  const pathname = usePathname();
  const router = useRouter();

  // Filter state derived from URL
  const statusFilter = sp.get("status") ?? "";
  const page = Number(sp.get("p") ?? "0");
  const limit = 25;

  function updateParams(updates: Record<string, string | null>, resetPage = true) {
    const p = new URLSearchParams(sp.toString());
    for (const [k, v] of Object.entries(updates)) {
      if (v === null || v === "") p.delete(k); else p.set(k, v);
    }
    if (resetPage) p.delete("p");
    router.replace(`${pathname}?${p.toString()}`, { scroll: false });
  }
  const jobs = useQuery({ queryKey: [...queryKeys.downloadJobs.all, statusFilter, page], queryFn: () => api.listDownloadJobs({ status: statusFilter || undefined, offset: page * limit, limit }), refetchInterval: statusFilter === "" || statusFilter === "pending" || statusFilter === "downloading" ? 10000 : false });

  return (
    <main className="max-w-7xl mx-auto p-6">
      <PageHeader title={t("downloads.title")} description={jobs.data?.length ? t("common.page").replace("{page}", String(page + 1)) : t("downloads.desc")}>
        <Link href="/admin/jobs" className="btn-ghost text-xs">{t("downloads.view_jobs")}</Link>
      </PageHeader>

      <div className="toolbar mb-4">
        {STATUS_OPTIONS.map((s) => (
          <button key={s} onClick={() => updateParams({ status: s || null })}
            className={`segment border ${statusFilter === s ? "segment-active border-[#d8dee4] dark:border-[#30363d]" : "border-transparent"}`}>
            {s || t("downloads.filter_all")}
          </button>
        ))}
        <button onClick={() => jobs.refetch()} className="btn-ghost ml-auto text-xs">{t("downloads.refresh")}</button>
      </div>

      {jobs.isLoading && <div className="space-y-2">{Array.from({ length: 5 }).map((_, i) => <div key={i} className="h-10 rounded-md bg-[#eaeef2] dark:bg-[#21262d] animate-pulse" />)}</div>}
      {jobs.error && <ErrorState message={(jobs.error as Error).message} onRetry={() => jobs.refetch()} />}
      {jobs.data && !jobs.data.length && <EmptyState title={t("downloads.no_jobs")} description={t("downloads.no_jobs_desc")} />}

      {jobs.data && jobs.data.length > 0 && (
        <div className="table-shell">
          <table className="w-full text-sm">
            <thead><tr className="table-head"><th className="text-left px-4 py-2.5">{t("downloads.col_id")}</th><th className="text-left px-4 py-2.5">{t("downloads.col_source")}</th><th className="text-left px-4 py-2.5">{t("downloads.col_status")}</th><th className="text-left px-4 py-2.5">{t("downloads.col_retries")}</th><th className="text-left px-4 py-2.5">{t("downloads.col_created")}</th><th className="text-left px-4 py-2.5">{t("downloads.col_actions")}</th></tr></thead>
            <tbody>{jobs.data.map((job) => <JobRow key={job.id} job={job} />)}</tbody>
          </table>
        </div>
      )}
      {jobs.data && jobs.data.length > 0 && (
        <div className="flex gap-2 justify-center mt-4">
          <button disabled={page === 0} onClick={() => updateParams({ p: page <= 1 ? null : String(page - 1) }, false)}
            className="btn-ghost disabled:opacity-30">{t("common.prev")}</button>
          <span className="px-3 py-1 text-sm text-[#57606a] dark:text-[#8b949e]">{t("common.page").replace("{page}", String(page + 1))}</span>
          <button onClick={() => updateParams({ p: String(page + 1) }, false)} disabled={!jobs.data || jobs.data.length < limit}
            className="btn-ghost disabled:opacity-30">{t("common.next")}</button>
        </div>
      )}
    </main>
  );
}

export default function DownloadsPage() {
  return (
    <Suspense>
      <DownloadsContent />
    </Suspense>
  );
}

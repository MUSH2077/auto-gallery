"use client";
import { useMemo, useState } from "react";
import { useT } from "@/lib/i18n";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api, queryKeys, DownloadJob } from "@/lib/api";
import Link from "next/link";
import { PageHeader, StatusBadge, SourceBadge, EmptyState, ErrorState, ConfirmDialog } from "@/components";

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
      <tr className="border-b dark:border-slate-700 hover:bg-gray-50 dark:hover:bg-slate-700 dark:bg-slate-800/50">
        <td className="px-4 py-3">
          <div className="font-mono text-xs text-gray-400 dark:text-gray-500">{job.id.slice(0, 8)}</div>
        </td>
        <td className="px-4 py-3"><SourceBadge source={job.source} /></td>
        <td className="px-4 py-3"><StatusBadge status={job.status} /></td>
        <td className="px-4 py-3 text-xs text-gray-500 dark:text-gray-400">{job.retry_count}/3</td>
        <td className="px-4 py-3 text-xs text-gray-400 dark:text-gray-500">{new Date(job.created_at).toLocaleString()}</td>
        <td className="px-4 py-3">
          <div className="flex gap-2">
            {job.error_log && <button onClick={() => setShowLog(!showLog)} className="text-xs text-blue-600 hover:underline">{t("downloads.log")}</button>}
            {(job.status === "failed" || job.status === "stale") && (
              <button onClick={() => setConfirmRetry(true)} disabled={retry.isPending}
                className="text-xs px-2 py-0.5 bg-blue-100 text-blue-700 rounded hover:bg-blue-200 disabled:opacity-50">
                {retry.isPending ? "..." : t("downloads.retry")}
              </button>
            )}
          </div>
        </td>
      </tr>
      {showLog && job.error_log && (
        <tr><td colSpan={6} className="px-4 py-3 bg-gray-50 dark:bg-slate-800/50"><pre className="text-xs font-mono whitespace-pre-wrap max-h-48 overflow-auto bg-gray-100 dark:bg-slate-700 p-3 rounded">{job.error_log}</pre></td></tr>
      )}
      {confirmRetry && <ConfirmDialog open title={t("downloads.retry_title")} message={t("downloads.retry_msg").replace("{id}", job.id.slice(0, 8))} onConfirm={() => retry.mutate()} onCancel={() => setConfirmRetry(false)} isPending={retry.isPending} error={(retry.error as Error)?.message} />}
    </>
  );
}

export default function DownloadsPage() {
  const t = useT();
  const [statusFilter, setStatusFilter] = useState("");
  const [page, setPage] = useState(0);
  const limit = 25;
  const jobs = useQuery({ queryKey: [...queryKeys.downloadJobs.all, statusFilter, page], queryFn: () => api.listDownloadJobs(statusFilter || undefined, page * limit, limit), refetchInterval: statusFilter === "" || statusFilter === "pending" || statusFilter === "downloading" ? 10000 : false });

  return (
    <main className="max-w-7xl mx-auto p-6">
      <PageHeader title={t("downloads.title")} description={jobs.data?.length ? t("common.page").replace("{page}", String(page + 1)) : t("downloads.desc")}>
        <Link href="/admin/jobs" className="px-3 py-1.5 text-xs bg-blue-600 text-white rounded hover:bg-blue-700">{t("downloads.view_jobs")}</Link>
      </PageHeader>

      <div className="flex gap-2 mb-4 flex-wrap">
        {STATUS_OPTIONS.map((s) => (
          <button key={s} onClick={() => { setStatusFilter(s); setPage(0); }}
            className={`px-3 py-1 rounded text-xs font-medium border ${statusFilter === s ? "bg-slate-900 dark:bg-slate-600 text-white border-slate-900 dark:border-slate-500" : "bg-white dark:bg-slate-700 text-gray-600 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-slate-600"}`}>
            {s || t("downloads.filter_all")}
          </button>
        ))}
        <button onClick={() => jobs.refetch()} className="ml-auto px-3 py-1 text-xs border rounded hover:bg-gray-50 dark:hover:bg-slate-700 dark:bg-slate-800/50">{t("downloads.refresh")}</button>
      </div>

      {jobs.isLoading && <div className="space-y-2">{Array.from({ length: 5 }).map((_, i) => <div key={i} className="h-10 bg-gray-100 dark:bg-slate-700 rounded animate-pulse" />)}</div>}
      {jobs.error && <ErrorState message={(jobs.error as Error).message} onRetry={() => jobs.refetch()} />}
      {jobs.data && !jobs.data.length && <EmptyState title={t("downloads.no_jobs")} description={t("downloads.no_jobs_desc")} />}

      {jobs.data && jobs.data.length > 0 && (
        <div className="bg-white dark:bg-slate-800 rounded-lg shadow overflow-x-auto">
          <table className="w-full text-sm">
            <thead><tr className="border-b dark:border-slate-700 bg-gray-50 dark:bg-slate-800/50"><th className="text-left px-4 py-3">{t("downloads.col_id")}</th><th className="text-left px-4 py-3">{t("downloads.col_source")}</th><th className="text-left px-4 py-3">{t("downloads.col_status")}</th><th className="text-left px-4 py-3">{t("downloads.col_retries")}</th><th className="text-left px-4 py-3">{t("downloads.col_created")}</th><th className="text-left px-4 py-3">{t("downloads.col_actions")}</th></tr></thead>
            <tbody>{jobs.data.map((job) => <JobRow key={job.id} job={job} />)}</tbody>
          </table>
        </div>
      )}
      {jobs.data && jobs.data.length > 0 && (
        <div className="flex gap-2 justify-center mt-4">
          <button disabled={page === 0} onClick={() => setPage(page - 1)}
            className="px-3 py-1 text-sm border rounded disabled:opacity-30 dark:border-slate-600 dark:text-gray-300">{t("common.prev")}</button>
          <span className="px-3 py-1 text-sm text-gray-500 dark:text-gray-400">{t("common.page").replace("{page}", String(page + 1))}</span>
          <button onClick={() => setPage(page + 1)} disabled={!jobs.data || jobs.data.length < limit}
            className="px-3 py-1 text-sm border rounded disabled:opacity-30 dark:border-slate-600 dark:text-gray-300">{t("common.next")}</button>
        </div>
      )}
    </main>
  );
}

"use client";
import { useState } from "react";
import { useT } from "@/lib/i18n";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api, queryKeys } from "@/lib/api";
import Link from "next/link";
import { PageHeader, StatusBadge, EmptyState, ErrorState, ConfirmDialog } from "@/components";

const STATUS_OPTIONS = ["", "pending", "running", "complete", "failed", "stale"];

interface ImportJob {
  id: string;
  download_job_id: string;
  status: string;
  error_log?: string;
  created_at: string;
  updated_at?: string;
}

function JobRow({ job }: { job: ImportJob }) {
  const t = useT();
  const [showLog, setShowLog] = useState(false);
  const [confirmRetry, setConfirmRetry] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const qc = useQueryClient();

  const retry = useMutation({
    mutationFn: () => api.retryImportJob(job.id),
    onSuccess: () => { qc.invalidateQueries({ queryKey: queryKeys.importJobs.all }); setConfirmRetry(false); },
  });

  const deleteJob = useMutation({
    mutationFn: () => api.deleteImportJob(job.id),
    onSuccess: () => { qc.invalidateQueries({ queryKey: queryKeys.importJobs.all }); setConfirmDelete(false); },
  });

  const canRetry = job.status === "failed" || job.status === "stale";
  const canDelete = job.status === "complete" || job.status === "failed" || job.status === "stale";

  return (
    <>
      <tr className="border-b dark:border-slate-700 hover:bg-gray-50 dark:hover:bg-slate-700 dark:bg-slate-800/50">
        <td className="px-4 py-3">
          <div className="font-mono text-xs text-gray-400 dark:text-gray-500">{job.id.slice(0, 8)}</div>
        </td>
        <td className="px-4 py-3">
          <div className="font-mono text-xs text-gray-400 dark:text-gray-500">{job.download_job_id.slice(0, 8)}</div>
        </td>
        <td className="px-4 py-3"><StatusBadge status={job.status} /></td>
        <td className="px-4 py-3 text-xs text-gray-400 dark:text-gray-500">{new Date(job.created_at).toLocaleString()}</td>
        <td className="px-4 py-3">
          <div className="flex gap-2">
            {job.error_log && (
              <button onClick={() => setShowLog(!showLog)} className="text-xs text-blue-600 hover:underline">{t("imports.log")}</button>
            )}
            {canRetry && (
              <button onClick={() => setConfirmRetry(true)} disabled={retry.isPending}
                className="text-xs px-2 py-0.5 bg-blue-100 text-blue-700 rounded hover:bg-blue-200 disabled:opacity-50">
                {retry.isPending ? "..." : t("imports.retry")}
              </button>
            )}
            {canDelete && (
              <button onClick={() => setConfirmDelete(true)} disabled={deleteJob.isPending}
                className="text-xs px-2 py-0.5 bg-red-100 dark:bg-red-900/30 text-red-700 dark:text-red-400 rounded hover:bg-red-200 disabled:opacity-50">
                {deleteJob.isPending ? "..." : t("imports.del")}
              </button>
            )}
          </div>
        </td>
      </tr>
      {showLog && job.error_log && (
        <tr><td colSpan={5} className="px-4 py-3 bg-gray-50 dark:bg-slate-800/50">
          <pre className="text-xs font-mono whitespace-pre-wrap max-h-48 overflow-auto bg-gray-100 dark:bg-slate-700 p-3 rounded">{job.error_log}</pre>
        </td></tr>
      )}
      {confirmRetry && <ConfirmDialog open title={t("imports.retry_title")} message={t("imports.retry_msg").replace("{id}", job.id.slice(0, 8))} onConfirm={() => retry.mutate()} onCancel={() => setConfirmRetry(false)} isPending={retry.isPending} error={(retry.error as Error)?.message} />}
      {confirmDelete && <ConfirmDialog open title={t("imports.delete_title")} message={t("imports.delete_msg").replace("{id}", job.id.slice(0, 8))} onConfirm={() => deleteJob.mutate()} onCancel={() => setConfirmDelete(false)} isPending={deleteJob.isPending} error={(deleteJob.error as Error)?.message} />}
    </>
  );
}

export default function ImportJobsPage() {
  const t = useT();
  const qc = useQueryClient();
  const [statusFilter, setStatusFilter] = useState("");
  const jobs = useQuery({
    queryKey: [...queryKeys.importJobs.all, statusFilter],
    queryFn: () => api.listImportJobs(statusFilter || undefined),
    refetchInterval: statusFilter === "" || statusFilter === "pending" || statusFilter === "running" ? 10000 : false,
  });
  const scan = useMutation({ mutationFn: api.scanImports, onSuccess: () => jobs.refetch() });

  return (
    <main className="max-w-7xl mx-auto p-6">
      <PageHeader title={t("imports.title")} description={t("imports.desc")}>
        <button onClick={() => scan.mutate()} disabled={scan.isPending}
          className="px-4 py-2 bg-slate-900 dark:bg-slate-700 text-white rounded text-sm hover:bg-slate-800 dark:hover:bg-slate-600 disabled:opacity-50">
          {scan.isPending ? t("imports.scanning") : t("imports.scan")}
        </button>
      </PageHeader>
      <div className="mb-4">
        <Link href="/admin/jobs" className="text-sm text-blue-600 hover:underline">&larr; {t("imports.view_jobs")}</Link>
      </div>

      <div className="flex gap-2 mb-4 flex-wrap">
        {STATUS_OPTIONS.map((s) => (
          <button key={s} onClick={() => setStatusFilter(s)}
            className={`px-3 py-1 rounded text-xs font-medium border ${statusFilter === s ? "bg-slate-900 dark:bg-slate-600 text-white border-slate-900 dark:border-slate-500" : "bg-white dark:bg-slate-700 text-gray-600 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-slate-600"}`}>
            {s || t("imports.filter_all", "All")}
          </button>
        ))}
        <button onClick={() => jobs.refetch()} className="ml-auto px-3 py-1 text-xs border rounded hover:bg-gray-50 dark:hover:bg-slate-700 dark:bg-slate-800/50">{t("imports.refresh")}</button>
      </div>

      {jobs.isLoading && <div className="space-y-2">{Array.from({ length: 5 }).map((_, i) => <div key={i} className="h-10 bg-gray-100 dark:bg-slate-700 rounded animate-pulse" />)}</div>}
      {jobs.error && <ErrorState message={(jobs.error as Error).message} onRetry={() => jobs.refetch()} />}
      {jobs.data && !jobs.data.length && <EmptyState title={t("imports.no_jobs")} description={t("imports.no_jobs_desc")} />}

      {jobs.data && jobs.data.length > 0 && (
        <div className="bg-white dark:bg-slate-800 rounded-lg shadow overflow-x-auto">
          <table className="w-full text-sm">
            <thead><tr className="border-b dark:border-slate-700 bg-gray-50 dark:bg-slate-800/50">
              <th className="text-left px-4 py-3">{t("imports.col_id")}</th>
              <th className="text-left px-4 py-3">{t("imports.col_dl_job")}</th>
              <th className="text-left px-4 py-3">{t("imports.col_status")}</th>
              <th className="text-left px-4 py-3">{t("imports.col_created")}</th>
              <th className="text-left px-4 py-3">{t("imports.col_actions")}</th>
            </tr></thead>
            <tbody>{jobs.data.map((job) => <JobRow key={job.id} job={job} />)}</tbody>
          </table>
        </div>
      )}
    </main>
  );
}

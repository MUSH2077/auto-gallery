"use client";
import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api, queryKeys, DownloadJob } from "@/lib/api";
import { PageHeader, StatusBadge, SourceBadge, EmptyState, ErrorState, ConfirmDialog } from "@/components";

const STATUS_OPTIONS = ["", "pending", "downloading", "downloaded", "importing", "complete", "failed", "stale"];

function JobRow({ job }: { job: DownloadJob }) {
  const [showLog, setShowLog] = useState(false);
  const [confirmRetry, setConfirmRetry] = useState(false);
  const qc = useQueryClient();

  const retry = useMutation({
    mutationFn: () => api.retryDownloadJob(job.id),
    onSuccess: () => { qc.invalidateQueries({ queryKey: queryKeys.downloadJobs.all }); setConfirmRetry(false); },
  });

  return (
    <>
      <tr className="border-b hover:bg-gray-50">
        <td className="px-4 py-3">
          <div className="font-mono text-xs text-gray-400">{job.id.slice(0, 8)}</div>
        </td>
        <td className="px-4 py-3"><SourceBadge source={job.source} /></td>
        <td className="px-4 py-3"><StatusBadge status={job.status} /></td>
        <td className="px-4 py-3 text-xs text-gray-500">{job.retry_count}/3</td>
        <td className="px-4 py-3 text-xs text-gray-400">{new Date(job.created_at).toLocaleString()}</td>
        <td className="px-4 py-3">
          <div className="flex gap-2">
            {job.error_log && <button onClick={() => setShowLog(!showLog)} className="text-xs text-blue-600 hover:underline">Log</button>}
            {(job.status === "failed" || job.status === "stale") && (
              <button onClick={() => setConfirmRetry(true)} disabled={retry.isPending}
                className="text-xs px-2 py-0.5 bg-blue-100 text-blue-700 rounded hover:bg-blue-200 disabled:opacity-50">
                {retry.isPending ? "..." : "Retry"}
              </button>
            )}
          </div>
        </td>
      </tr>
      {showLog && job.error_log && (
        <tr><td colSpan={6} className="px-4 py-3 bg-gray-50"><pre className="text-xs font-mono whitespace-pre-wrap max-h-48 overflow-auto bg-gray-100 p-3 rounded">{job.error_log}</pre></td></tr>
      )}
      {confirmRetry && <ConfirmDialog open title="Retry Job" message={`Retry download job ${job.id.slice(0, 8)}?`} onConfirm={() => retry.mutate()} onCancel={() => setConfirmRetry(false)} />}
    </>
  );
}

export default function DownloadsPage() {
  const [statusFilter, setStatusFilter] = useState("");
  const jobs = useQuery({ queryKey: [...queryKeys.downloadJobs.all, statusFilter], queryFn: () => api.listDownloadJobs(statusFilter || undefined), refetchInterval: statusFilter === "" || statusFilter === "pending" || statusFilter === "downloading" ? 10000 : false });

  return (
    <main className="max-w-7xl mx-auto p-6">
      <PageHeader title="Download Jobs" description="Monitor and manage download queue" />

      <div className="flex gap-2 mb-4 flex-wrap">
        {STATUS_OPTIONS.map((s) => (
          <button key={s} onClick={() => setStatusFilter(s)}
            className={`px-3 py-1 rounded text-xs font-medium border ${statusFilter === s ? "bg-slate-900 text-white border-slate-900" : "bg-white text-gray-600 hover:bg-gray-50"}`}>
            {s || "All"}
          </button>
        ))}
        <button onClick={() => jobs.refetch()} className="ml-auto px-3 py-1 text-xs border rounded hover:bg-gray-50">Refresh</button>
      </div>

      {jobs.isLoading && <div className="space-y-2">{Array.from({ length: 5 }).map((_, i) => <div key={i} className="h-10 bg-gray-100 rounded animate-pulse" />)}</div>}
      {jobs.error && <ErrorState message={(jobs.error as Error).message} onRetry={() => jobs.refetch()} />}
      {jobs.data && !jobs.data.length && <EmptyState title="No download jobs" description="Create a subscription and trigger a download to see jobs here." />}

      {jobs.data && jobs.data.length > 0 && (
        <div className="bg-white rounded-lg shadow overflow-x-auto">
          <table className="w-full text-sm">
            <thead><tr className="border-b bg-gray-50"><th className="text-left px-4 py-3">Job ID</th><th className="text-left px-4 py-3">Source</th><th className="text-left px-4 py-3">Status</th><th className="text-left px-4 py-3">Retries</th><th className="text-left px-4 py-3">Created</th><th className="text-left px-4 py-3">Actions</th></tr></thead>
            <tbody>{jobs.data.map((job) => <JobRow key={job.id} job={job} />)}</tbody>
          </table>
        </div>
      )}
    </main>
  );
}

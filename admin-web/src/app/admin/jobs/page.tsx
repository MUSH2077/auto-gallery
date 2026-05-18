"use client";
import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { api, queryKeys } from "@/lib/api";
import { PageHeader, EmptyState, ErrorState, ConfirmDialog, SourceBadge } from "@/components";

const STATUS_OPTIONS = ["", "pending", "downloading", "downloaded", "importing", "complete", "failed"];

export default function JobsPage() {
  const router = useRouter();
  const qc = useQueryClient();
  const [dlFilter, setDlFilter] = useState("");
  const [imFilter, setImFilter] = useState("");
  const [retryId, setRetryId] = useState<string | null>(null);
  const [deleteId, setDeleteId] = useState<string | null>(null);

  const downloads = useQuery({
    queryKey: [...queryKeys.downloadJobs.all, dlFilter],
    queryFn: () => api.listDownloadJobs(dlFilter || undefined, 0, 50),
    refetchInterval: 10000,
  });

  const imports = useQuery({
    queryKey: [...queryKeys.importJobs.all, imFilter],
    queryFn: () => api.listImportJobs(imFilter || undefined, 0, 50),
    refetchInterval: 10000,
  });

  const retryDL = useMutation({
    mutationFn: (id: string) => api.retryDownloadJob(id),
    onSuccess: () => { setRetryId(null); qc.invalidateQueries({ queryKey: queryKeys.downloadJobs.all }); },
  });

  const retryIM = useMutation({
    mutationFn: (id: string) => api.retryImportJob(id),
    onSuccess: () => { setRetryId(null); qc.invalidateQueries({ queryKey: queryKeys.importJobs.all }); },
  });

  const delDL = useMutation({
    mutationFn: (id: string) => api.deleteImportJob(id),
    onSuccess: () => { setDeleteId(null); qc.invalidateQueries(); },
  });

  return (
    <main className="max-w-7xl mx-auto p-6">
      <PageHeader title="Jobs" description="Download and import job queues" />

      {/* Download Jobs */}
      <section className="mb-8">
        <div className="flex items-center justify-between mb-3">
          <h2 className="text-lg font-semibold dark:text-white">Download Jobs ({downloads.data?.length || 0})</h2>
          <div className="flex gap-1 bg-gray-100 dark:bg-slate-700 rounded p-0.5">
            {STATUS_OPTIONS.map((s) => (
              <button key={s} onClick={() => setDlFilter(s)}
                className={`px-2.5 py-1 text-xs rounded transition-colors ${dlFilter === s ? "bg-white dark:bg-slate-600 shadow-sm font-medium" : "text-gray-500 hover:text-gray-700 dark:text-gray-400"}`}>
                {s || "All"}
              </button>
            ))}
          </div>
        </div>

        {downloads.isLoading && <div className="space-y-1">{Array.from({ length: 3 }).map((_, i) => <div key={i} className="h-12 bg-gray-100 dark:bg-slate-700 rounded animate-pulse" />)}</div>}
        {downloads.error && <ErrorState message={(downloads.error as Error).message} />}
        {downloads.data && !downloads.data.length && <EmptyState title="No download jobs" description={dlFilter ? "No jobs matching the selected status." : "No downloads have been triggered yet."} />}

        {downloads.data && downloads.data.length > 0 && (
          <div className="space-y-1">
            {downloads.data.map((j) => (
              <div key={j.id} className="bg-white dark:bg-slate-800 rounded-lg shadow-sm p-3 text-sm flex items-center gap-3">
                <span className={`w-2 h-2 rounded-full shrink-0 ${
                  j.status === "complete" || j.status === "downloaded" ? "bg-green-500" :
                  j.status === "failed" ? "bg-red-500" :
                  j.status === "downloading" ? "bg-blue-500 animate-pulse" : "bg-gray-300"
                }`} />
                <span className="font-mono text-xs text-gray-400 dark:text-gray-500 w-20 shrink-0">{j.id.slice(0, 8)}</span>
                <span className="bg-gray-100 dark:bg-slate-700 text-xs px-1.5 py-0.5 rounded w-24 text-center shrink-0">{j.status}</span>
                {j.source && <SourceBadge source={j.source} />}
                <span className="truncate text-gray-600 dark:text-gray-300 flex-1 min-w-0 text-xs">{j.source_url}</span>
                <span className="text-xs text-gray-400 shrink-0">{j.retry_count > 0 && `↻${j.retry_count}`}</span>
                <span className="text-xs text-gray-400 shrink-0 w-28 text-right">{new Date(j.created_at).toLocaleString()}</span>
                <div className="flex gap-1 shrink-0">
                  {j.status === "failed" && (
                    <button onClick={() => { setRetryId(j.id); retryDL.mutate(j.id); }} disabled={retryDL.isPending}
                      className="text-xs text-blue-600 hover:underline">Retry</button>
                  )}
                  <button onClick={() => { setDeleteId(j.id); }} className="text-xs text-red-500 hover:underline">Del</button>
                </div>
              </div>
            ))}
          </div>
        )}
      </section>

      {/* Import Jobs */}
      <section className="mb-8">
        <div className="flex items-center justify-between mb-3">
          <h2 className="text-lg font-semibold dark:text-white">Import Jobs ({imports.data?.length || 0})</h2>
          <div className="flex gap-1 bg-gray-100 dark:bg-slate-700 rounded p-0.5">
            {["", "pending", "running", "complete", "failed"].map((s) => (
              <button key={s} onClick={() => setImFilter(s)}
                className={`px-2.5 py-1 text-xs rounded transition-colors ${imFilter === s ? "bg-white dark:bg-slate-600 shadow-sm font-medium" : "text-gray-500 hover:text-gray-700 dark:text-gray-400"}`}>
                {s || "All"}
              </button>
            ))}
          </div>
        </div>

        {imports.isLoading && <div className="space-y-1">{Array.from({ length: 3 }).map((_, i) => <div key={i} className="h-12 bg-gray-100 dark:bg-slate-700 rounded animate-pulse" />)}</div>}
        {imports.error && <ErrorState message={(imports.error as Error).message} />}
        {imports.data && !imports.data.length && <EmptyState title="No import jobs" description={imFilter ? "No jobs matching the selected status." : "Import jobs appear after downloads complete."} />}

        {imports.data && imports.data.length > 0 && (
          <div className="space-y-1">
            {imports.data.map((j) => (
              <div key={j.id} className="bg-white dark:bg-slate-800 rounded-lg shadow-sm p-3 text-sm flex items-center gap-3">
                <span className={`w-2 h-2 rounded-full shrink-0 ${
                  j.status === "complete" ? "bg-green-500" :
                  j.status === "failed" ? "bg-red-500" :
                  j.status === "running" ? "bg-blue-500 animate-pulse" : "bg-gray-300"
                }`} />
                <span className="font-mono text-xs text-gray-400 dark:text-gray-500 w-20 shrink-0">{j.id.slice(0, 8)}</span>
                <span className="bg-gray-100 dark:bg-slate-700 text-xs px-1.5 py-0.5 rounded w-24 text-center shrink-0">{j.status}</span>
                <span className="font-mono text-xs text-gray-400 truncate flex-1">DL: {j.download_job_id ? j.download_job_id.slice(0, 8) : "—"}</span>
                {j.error_log && <span className="text-xs text-red-500 truncate max-w-xs" title={j.error_log}>{(j.error_log || "").slice(0, 80)}</span>}
                <span className="text-xs text-gray-400 shrink-0 w-28 text-right">{new Date(j.created_at).toLocaleString()}</span>
                <div className="flex gap-1 shrink-0">
                  {j.status === "failed" && (
                    <button onClick={() => { setRetryId(j.id); retryIM.mutate(j.id); }} disabled={retryIM.isPending}
                      className="text-xs text-blue-600 hover:underline">Retry</button>
                  )}
                  <button onClick={() => { setDeleteId(j.id); }} className="text-xs text-red-500 hover:underline">Del</button>
                </div>
              </div>
            ))}
          </div>
        )}
      </section>

      {deleteId && <ConfirmDialog open title="Delete Job" message="Delete this job?" onConfirm={() => delDL.mutate(deleteId)} onCancel={() => setDeleteId(null)} isPending={delDL.isPending} />}
    </main>
  );
}

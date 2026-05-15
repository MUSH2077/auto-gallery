"use client";
import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api, queryKeys } from "@/lib/api";
import { PageHeader, StatusBadge, EmptyState, ErrorState } from "@/components";

export default function ImportJobsPage() {
  const qc = useQueryClient();
  const [statusFilter, setStatusFilter] = useState("");
  const jobs = useQuery({ queryKey: [...queryKeys.importJobs.all, statusFilter], queryFn: () => api.listImportJobs(statusFilter || undefined) });
  const scan = useMutation({ mutationFn: api.scanImports, onSuccess: () => jobs.refetch() });

  return (
    <main className="max-w-6xl mx-auto p-6">
      <PageHeader title="Import Jobs" description="Metadata import pipeline status">
        <button onClick={() => scan.mutate()} disabled={scan.isPending} className="px-4 py-2 bg-slate-900 text-white rounded text-sm hover:bg-slate-800 disabled:opacity-50">{scan.isPending ? "Scanning..." : "Scan for Imports"}</button>
      </PageHeader>

      <div className="flex gap-2 mb-4">
        {["", "pending", "running", "complete", "failed"].map((s) => (
          <button key={s} onClick={() => setStatusFilter(s)} className={`px-3 py-1 rounded text-xs font-medium border ${statusFilter === s ? "bg-slate-900 text-white border-slate-900" : "bg-white text-gray-600 hover:bg-gray-50"}`}>{s || "All"}</button>
        ))}
      </div>

      {jobs.isLoading && <div className="space-y-2">{Array.from({ length: 5 }).map((_, i) => <div key={i} className="h-10 bg-gray-100 rounded animate-pulse" />)}</div>}
      {jobs.error && <ErrorState message={(jobs.error as Error).message} />}
      {jobs.data && !jobs.data.length && <EmptyState title="No import jobs" description="Import jobs are created after downloads complete." />}

      {jobs.data && jobs.data.length > 0 && (
        <div className="bg-white rounded-lg shadow overflow-x-auto">
          <table className="w-full text-sm">
            <thead><tr className="border-b bg-gray-50"><th className="text-left px-4 py-3">Job ID</th><th className="text-left px-4 py-3">Download Job</th><th className="text-left px-4 py-3">Status</th><th className="text-left px-4 py-3">Created</th><th className="text-left px-4 py-3">Error</th></tr></thead>
            <tbody>
              {jobs.data.map((j) => (
                <tr key={j.id} className="border-b hover:bg-gray-50">
                  <td className="px-4 py-3 font-mono text-xs">{j.id.slice(0, 8)}</td>
                  <td className="px-4 py-3 font-mono text-xs text-gray-400">{j.download_job_id.slice(0, 8)}</td>
                  <td className="px-4 py-3"><StatusBadge status={j.status} /></td>
                  <td className="px-4 py-3 text-xs text-gray-400">{new Date(j.created_at).toLocaleString()}</td>
                  <td className="px-4 py-3 text-xs text-red-500 max-w-xs truncate">{j.error_log || "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </main>
  );
}

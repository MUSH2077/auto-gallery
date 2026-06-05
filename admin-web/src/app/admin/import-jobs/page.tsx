"use client";
import { useMemo, useState, useEffect, Suspense } from "react";
import { useT } from "@/lib/i18n";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api, queryKeys } from "@/lib/api";
import Link from "next/link";
import { PageHeader, StatusBadge, EmptyState, ErrorState, ConfirmDialog } from "@/components";
import { useRouter, useSearchParams, usePathname } from "next/navigation";

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
      <tr className="table-row">
        <td className="px-4 py-3">
          <div className="font-mono text-xs text-[#57606a] dark:text-[#8b949e]">{job.id.slice(0, 8)}</div>
        </td>
        <td className="px-4 py-3">
          <div className="font-mono text-xs text-[#57606a] dark:text-[#8b949e]">{job.download_job_id.slice(0, 8)}</div>
        </td>
        <td className="px-4 py-3"><StatusBadge status={job.status} /></td>
        <td className="px-4 py-3 text-xs text-[#57606a] dark:text-[#8b949e]">{new Date(job.created_at).toLocaleString()}</td>
        <td className="px-4 py-3">
          <div className="flex gap-2">
            {job.error_log && (
              <button onClick={() => setShowLog(!showLog)} className="text-xs text-blue-600 hover:underline">{t("imports.log")}</button>
            )}
            {canRetry && (
              <button onClick={() => setConfirmRetry(true)} disabled={retry.isPending}
                className="btn-ghost px-2 py-0.5 text-xs">
                {retry.isPending ? "..." : t("imports.retry")}
              </button>
            )}
            {canDelete && (
              <button onClick={() => setConfirmDelete(true)} disabled={deleteJob.isPending}
                className="btn-danger px-2 py-0.5 text-xs">
                {deleteJob.isPending ? "..." : t("imports.del")}
              </button>
            )}
          </div>
        </td>
      </tr>
      {showLog && job.error_log && (
        <tr><td colSpan={5} className="border-b border-[#d8dee4] bg-[#f6f8fa] px-4 py-3 dark:border-[#30363d] dark:bg-[#0d1117]">
          <pre className="max-h-48 overflow-auto rounded-md border border-[#d8dee4] bg-white p-3 font-mono text-xs whitespace-pre-wrap text-[#24292f] dark:border-[#30363d] dark:bg-[#161b22] dark:text-[#e6edf3]">{job.error_log}</pre>
        </td></tr>
      )}
      {confirmRetry && <ConfirmDialog open title={t("imports.retry_title")} message={t("imports.retry_msg").replace("{id}", job.id.slice(0, 8))} onConfirm={() => retry.mutate()} onCancel={() => setConfirmRetry(false)} isPending={retry.isPending} error={(retry.error as Error)?.message} />}
      {confirmDelete && <ConfirmDialog open title={t("imports.delete_title")} message={t("imports.delete_msg").replace("{id}", job.id.slice(0, 8))} onConfirm={() => deleteJob.mutate()} onCancel={() => setConfirmDelete(false)} isPending={deleteJob.isPending} error={(deleteJob.error as Error)?.message} />}
    </>
  );
}

function ImportJobsContent() {
  const t = useT();
  const qc = useQueryClient();
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
  const jobs = useQuery({
    queryKey: [...queryKeys.importJobs.all, statusFilter, page],
    queryFn: () => api.listImportJobs(statusFilter || undefined, page * limit, limit),
    refetchInterval: statusFilter === "" || statusFilter === "pending" || statusFilter === "running" ? 10000 : false,
  });
  const scan = useMutation({ mutationFn: api.scanImports, onSuccess: () => jobs.refetch() });

  return (
    <main className="max-w-7xl mx-auto p-6">
      <PageHeader title={t("imports.title")} description={(jobs.data?.total ?? 0) > 0 ? t("common.page").replace("{page}", String(page + 1)) : t("imports.desc")}>
        <button onClick={() => scan.mutate()} disabled={scan.isPending}
          className="btn-primary">
          {scan.isPending ? t("imports.scanning") : t("imports.scan")}
        </button>
      </PageHeader>
      <div className="mb-4">
        <Link href="/admin/jobs" className="text-sm text-blue-600 hover:underline">&larr; {t("imports.view_jobs")}</Link>
      </div>

      <div className="toolbar mb-4 flex-wrap">
        <div className="segmented-control">
        {STATUS_OPTIONS.map((s) => (
          <button key={s} onClick={() => updateParams({ status: s || null })}
            className={`segment ${statusFilter === s ? "segment-active" : ""}`}>
            {s || t("imports.filter_all", "All")}
          </button>
        ))}
        </div>
        <button onClick={() => jobs.refetch()} className="btn-ghost ml-auto px-3 py-1 text-xs">{t("imports.refresh")}</button>
      </div>

      {jobs.isLoading && <div className="space-y-2">{Array.from({ length: 5 }).map((_, i) => <div key={i} className="h-10 rounded-md bg-[#eaeef2] animate-pulse dark:bg-[#21262d]" />)}</div>}
      {jobs.error && <ErrorState message={(jobs.error as Error).message} onRetry={() => jobs.refetch()} />}
      {jobs.data && !jobs.data.items?.length && <EmptyState title={t("imports.no_jobs")} description={t("imports.no_jobs_desc")} />}

      {jobs.data && jobs.data.items?.length > 0 && (
        <div className="table-shell">
          <table className="w-full text-sm">
            <thead><tr className="table-head">
              <th className="text-left px-4 py-3">{t("imports.col_id")}</th>
              <th className="text-left px-4 py-3">{t("imports.col_dl_job")}</th>
              <th className="text-left px-4 py-3">{t("imports.col_status")}</th>
              <th className="text-left px-4 py-3">{t("imports.col_created")}</th>
              <th className="text-left px-4 py-3">{t("imports.col_actions")}</th>
            </tr></thead>
            <tbody>{jobs.data.items.map((job) => <JobRow key={job.id} job={job} />)}</tbody>
          </table>
        </div>
      )}

      {(jobs.data?.total ?? 0) > 0 && (
        <div className="flex gap-2 justify-center mt-4">
          <button disabled={page === 0} onClick={() => updateParams({ p: page <= 1 ? null : String(page - 1) }, false)}
            className="btn-ghost px-3 py-1 text-sm disabled:opacity-30">{t("common.prev")}</button>
          <span className="px-3 py-1 text-sm text-[#57606a] dark:text-[#8b949e]">{t("common.page").replace("{page}", String(page + 1))}</span>
          <button onClick={() => updateParams({ p: String(page + 1) }, false)} disabled={!jobs.data || (page + 1) * limit >= jobs.data.total}
            className="btn-ghost px-3 py-1 text-sm disabled:opacity-30">{t("common.next")}</button>
        </div>
      )}
    </main>
  );
}

export default function ImportJobsPage() {
  return (
    <Suspense>
      <ImportJobsContent />
    </Suspense>
  );
}

"use client";
import { useState, useEffect, useCallback, useMemo } from "react";
import { useToast } from "@/components/Toast";
import { useT } from "@/lib/i18n";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { api, queryKeys } from "@/lib/api";
import { PageHeader, EmptyState, ErrorState, ConfirmDialog, SourceBadge } from "@/components";


const REFETCH_ACTIVE_MS = 3000;
const REFETCH_IDLE_MS = 10000;
const PAGE_LIMIT = 200;

const STATUS_OPTIONS = ["", "pending", "downloading", "paused", "downloaded", "importing", "complete", "failed", "stale"];
const SOURCE_OPTIONS = ["", "pixiv", "x", "iwara", "danbooru", "pinterest", "lofter", "weibo", "bilibili"];

function Elapsed({ since, active }: { since: string; active: boolean }) {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    if (!active) return;
    const iv = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(iv);
  }, [active]);
  const seconds = Math.floor((now - new Date(since).getTime()) / 1000);
  if (seconds < 60) return <span className="text-xs text-blue-500 font-mono">{seconds}s</span>;
  if (seconds < 3600) return <span className="text-xs text-blue-500 font-mono">{Math.floor(seconds / 60)}m {seconds % 60}s</span>;
  return <span className="text-xs text-blue-500 font-mono">{Math.floor(seconds / 3600)}h {Math.floor((seconds % 3600) / 60)}m</span>;
}

function ProgressBar({ active }: { active: boolean }) {
  if (!active) return null;
  return <div className="w-20 h-1.5 bg-gray-200 dark:bg-slate-600 rounded-full overflow-hidden shrink-0"><div className="h-full bg-blue-500 rounded-full animate-pulse" style={{ width: "60%" }} /></div>;
}

function ActiveIndicator({ status }: { status: string }) {
  const t = useT();
  const toast = useToast();
  const isActive = status === "downloading" || status === "running" || status === "importing";
  const color = 
    status === "complete" || status === "downloaded" ? "bg-green-100 dark:bg-green-900/30 text-green-700 dark:text-green-400" :
    status === "failed" ? "bg-red-100 dark:bg-red-900/30 text-red-600 dark:text-red-400" :
    status === "stale" ? "bg-orange-100 dark:bg-orange-900/30 text-orange-700 dark:text-orange-400" :
    status === "paused" ? "bg-yellow-100 dark:bg-yellow-900/30 text-yellow-700 dark:text-yellow-400" :
    status === "pending" ? "bg-purple-100 dark:bg-purple-900/30 text-purple-700 dark:text-purple-400" :
    "bg-gray-100 dark:bg-slate-700 text-gray-500 dark:text-gray-400";
  if (isActive) {
    return <span className="flex items-center gap-1.5 shrink-0 w-28">
      <span className="relative flex h-2.5 w-2.5"><span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-blue-400 opacity-75" /><span className="relative inline-flex rounded-full h-2.5 w-2.5 bg-blue-500" /></span>
      <span className="text-xs text-blue-600 dark:text-blue-400 font-medium">{status}</span>
    </span>;
  }
  return <span className={`shrink-0 w-28 text-xs px-1.5 py-0.5 rounded text-center ${color}`}>{status}</span>;
}

export default function JobsPage() {
  const t = useT();
  const toast = useToast();
  const router = useRouter();
  const qc = useQueryClient();

  // Filters
  const [dlStatus, setDlStatus] = useState("");
  const [dlSource, setDlSource] = useState("");
  const [dlSort, setDlSort] = useState("created_at");
  const [dlOrder, setDlOrder] = useState("desc");
  const [imFilter, setImFilter] = useState("");

  const [retryId, setRetryId] = useState<string | null>(null);
  const [deleteId, setDeleteId] = useState<string | null>(null);
  const [deleteType, setDeleteType] = useState<"dl" | "im">("dl");
  const [expandedLog, setExpandedLog] = useState<string | null>(null);
  const [expandedImports, setExpandedImports] = useState<string | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [selectAll, setSelectAll] = useState(false);

  const dlParams = useMemo(() => ({
    status: dlStatus || undefined,
    source: dlSource || undefined,
    sort_by: dlSort,
    sort_order: dlOrder,
    offset: 0, limit: PAGE_LIMIT,
  }), [dlStatus, dlSource, dlSort, dlOrder]);

  const downloads = useQuery({
    queryKey: [...queryKeys.downloadJobs.all, dlParams],
    queryFn: () => api.listDownloadJobs(dlParams),
    refetchInterval: (dlStatus === "downloading" || !dlStatus) ? REFETCH_ACTIVE_MS : REFETCH_IDLE_MS,
  });

  const imports = useQuery({
    queryKey: [...queryKeys.importJobs.all, imFilter],
    queryFn: () => api.listImportJobs(imFilter || undefined, 0, PAGE_LIMIT),
    refetchInterval: (imFilter === "running" || !imFilter) ? REFETCH_ACTIVE_MS : REFETCH_IDLE_MS,
  });

  // --- Mutations ---
  const retryDL = useMutation({
    mutationFn: (id: string) => api.retryDownloadJob(id),
    onSuccess: () => { setRetryId(null); qc.invalidateQueries({ queryKey: queryKeys.downloadJobs.all }); },
  });
  const retryIM = useMutation({
    mutationFn: (id: string) => api.retryImportJob(id),
    onSuccess: () => { setRetryId(null); qc.invalidateQueries({ queryKey: queryKeys.importJobs.all }); },
  });
  const pauseDL = useMutation({
    mutationFn: (id: string) => api.pauseDownloadJob(id),
    onMutate: (id) => {
      qc.setQueriesData({ queryKey: queryKeys.downloadJobs.all }, (old: any) => {
        if (!Array.isArray(old)) return old;
        return old.map((j: any) => j.id === id ? { ...j, status: "paused" } : j);
      });
    },
    onSettled: () => qc.invalidateQueries({ queryKey: queryKeys.downloadJobs.all }),
  });
  const resumeDL = useMutation({
    mutationFn: (id: string) => api.resumeDownloadJob(id),
    onMutate: (id) => {
      qc.setQueriesData({ queryKey: queryKeys.downloadJobs.all }, (old: any) => {
        if (!Array.isArray(old)) return old;
        return old.map((j: any) => j.id === id ? { ...j, status: "pending" } : j);
      });
    },
    onSettled: () => qc.invalidateQueries({ queryKey: queryKeys.downloadJobs.all }),
  });
  const deleteDL = useMutation({
    mutationFn: (id: string) => api.deleteDownloadJob(id),
    onSuccess: () => { setDeleteId(null); qc.invalidateQueries({ queryKey: queryKeys.downloadJobs.all }); },
  });
  const deleteIM = useMutation({
    mutationFn: (id: string) => api.deleteImportJob(id),
    onSuccess: () => { setDeleteId(null); qc.invalidateQueries({ queryKey: queryKeys.importJobs.all }); },
  });

  const clearDL = useMutation({
    mutationFn: (statuses: string[]) => api.clearDownloadJobs(statuses),
    onSuccess: () => { setSelected(new Set()); qc.invalidateQueries({ queryKey: queryKeys.downloadJobs.all }); },
  });
  const killStuck = useMutation({
    mutationFn: () => api.killStuckJobs(),
    onSuccess: () => qc.invalidateQueries({ queryKey: queryKeys.downloadJobs.all }),
  });
  const retryAllFailed = useMutation({
    mutationFn: () => api.retryAllFailedJobs(),
    onSuccess: () => qc.invalidateQueries({ queryKey: queryKeys.downloadJobs.all }),
  });

  // Batch
  const batchDL = useMutation({
    mutationFn: ({ ids, action }: { ids: string[]; action: string }) => api.batchDownloadJobs(ids, action),
    onSuccess: (data: any) => {
      toast.info(t("jobs.batch_result").replace("{succeeded}", String(data.succeeded)).replace("{failed}", String(data.failed)));
      setSelected(new Set()); setSelectAll(false);
      qc.invalidateQueries({ queryKey: queryKeys.downloadJobs.all });
    },
  });

  // Select all toggle
  const handleSelectAll = () => {
    if (selectAll) { setSelected(new Set()); setSelectAll(false); return; }
    if (!downloads.data) return;
    setSelected(new Set(downloads.data.map((j: any) => j.id)));
    setSelectAll(true);
  };

  const toggleSelect = (id: string) => {
    const next = new Set(selected);
    if (next.has(id)) { next.delete(id); setSelectAll(false); } else next.add(id);
    setSelected(next);
  };

  const getEligibleIds = (action: string): string[] => {
    if (!downloads.data) return [];
    const allowed: Record<string, string[]> = {
      retry: ["failed", "stale", "downloading", "complete"],
      pause: ["pending", "downloading"],
      resume: ["paused"],
      delete: ["pending", "downloading", "downloaded", "failed", "stale", "complete", "paused"],
    };
    const ok = allowed[action] || [];
    return downloads.data.filter((j: any) => selected.has(j.id) && ok.includes(j.status)).map((j: any) => j.id);
  };

  const handleBatch = (action: string) => {
    const eligible = getEligibleIds(action);
    if (eligible.length === 0) { toast.info(t("common.no_eligible")); return; }
    if (eligible.length < selected.size && !confirm(t("common.partial_selected"))) return;
    batchDL.mutate({ ids: eligible, action });
  };

  // Clear helpers
  const handleClear = (statuses: string[]) => {
    if (confirm(`Delete all ${statuses.join(", ")} jobs?`)) clearDL.mutate(statuses);
  };

  return (
    <main className="max-w-7xl mx-auto p-6">
      <PageHeader title={t("jobs.download")} description={downloads.data ? `${downloads.data?.length} ${t("common.items")}` : ""}>
        <div className="flex gap-2">
          <button onClick={handleSelectAll} className="px-3 py-1.5 text-xs bg-slate-100 dark:bg-slate-700 rounded hover:bg-slate-200 dark:hover:bg-slate-600">{selectAll ? t("common.deselect_all") : t("common.select_all")}</button>
          <button onClick={() => handleClear(["failed", "stale"])} className="px-3 py-1.5 text-xs bg-red-100 dark:bg-red-900/30 text-red-700 rounded hover:bg-red-200 dark:hover:bg-red-900/50">{t("jobs.clear_failed")}</button>
          <button onClick={() => handleClear(["complete"])} className="px-3 py-1.5 text-xs bg-green-100 dark:bg-green-900/30 text-green-700 rounded hover:bg-green-200 dark:hover:bg-green-900/50">{t("jobs.clear_complete")}</button>
          <button onClick={() => killStuck.mutate()} disabled={killStuck.isPending} className="px-3 py-1.5 text-xs bg-orange-100 dark:bg-orange-900/30 text-orange-700 rounded hover:bg-orange-200 dark:hover:bg-orange-900/50 disabled:opacity-50">{t("jobs.kill_stuck")}</button>
          <button onClick={() => retryAllFailed.mutate()} disabled={retryAllFailed.isPending} className="px-3 py-1.5 text-xs bg-blue-600 text-white rounded hover:bg-blue-700 disabled:opacity-50">{t("jobs.retry_all_failed")}</button>
        </div>
      </PageHeader>

      {/* Filters */}
      <div className="flex flex-wrap items-center gap-2 mb-4">
        <select value={dlStatus} onChange={(e) => setDlStatus(e.target.value)} className="border rounded px-2 py-1.5 text-xs dark:bg-slate-700 dark:text-white dark:border-slate-600">
          <option value="">{t("jobs.filter_all_status")}</option>
          {STATUS_OPTIONS.filter(Boolean).map(s => <option key={s} value={s}>{s}</option>)}
        </select>
        <select value={dlSource} onChange={(e) => setDlSource(e.target.value)} className="border rounded px-2 py-1.5 text-xs dark:bg-slate-700 dark:text-white dark:border-slate-600">
          <option value="">{t("jobs.filter_all_source")}</option>
          {SOURCE_OPTIONS.filter(Boolean).map(s => <option key={s} value={s}>{s}</option>)}
        </select>
        <select value={`${dlSort}-${dlOrder}`} onChange={(e) => { const [k, o] = e.target.value.split("-"); setDlSort(k); setDlOrder(o); }} className="border rounded px-2 py-1.5 text-xs dark:bg-slate-700 dark:text-white dark:border-slate-600">
          <option value="created_at-desc">{t("jobs.sort_newest")}</option>
          <option value="created_at-asc">{t("jobs.sort_oldest")}</option>
          <option value="status-asc">{t("jobs.sort_status")}</option>
          <option value="source-asc">{t("jobs.sort_source")}</option>
        </select>

        {selected.size > 0 && (
          <div className="flex gap-1 ml-auto bg-yellow-50 dark:bg-yellow-900/20 rounded px-3 py-1.5 items-center">
            <span className="text-xs text-yellow-700 dark:text-yellow-300">{selected.size} {t("common.selected")}</span>
            <button onClick={() => handleBatch("pause")} className="px-2 py-0.5 text-xs bg-yellow-500 text-white rounded hover:bg-yellow-600">{t("jobs.batch_pause")}</button>
            <button onClick={() => handleBatch("resume")} className="px-2 py-0.5 text-xs bg-green-500 text-white rounded hover:bg-green-600">{t("jobs.batch_resume")}</button>
            <button onClick={() => handleBatch("retry")} className="px-2 py-0.5 text-xs bg-blue-500 text-white rounded hover:bg-blue-600">{t("jobs.batch_retry")}</button>
            <button onClick={() => handleBatch("delete")} className="px-2 py-0.5 text-xs bg-red-500 text-white rounded hover:bg-red-600">{t("jobs.batch_delete")}</button>
          </div>
        )}
      </div>

      {/* Download Jobs list */}
      <section className="mb-8">
        {downloads.isLoading && <div className="space-y-2">{Array.from({ length: 5 }).map((_, i) => <div key={i} className="h-14 bg-gray-100 dark:bg-slate-700 rounded animate-pulse" />)}</div>}
        {downloads.error && <ErrorState message={(downloads.error as Error).message} onRetry={() => downloads.refetch()} />}
        {downloads.data && !downloads.data?.length && <EmptyState title={t("jobs.no_dl")} description={t("jobs.no_dl_desc")} />}
        {downloads.data && downloads.data?.length > 0 && (
          <div className="space-y-1">
            {downloads.data.map((j: any) => {
              const active = j.status === "downloading" || j.status === "pending";
              return (
                <div key={j.id}>
                  <div className={`bg-white dark:bg-slate-800 rounded-lg shadow-sm p-3 text-sm flex items-center gap-3 ${active ? "border-l-2 border-blue-500" : j.status === "failed" ? "border-l-2 border-red-400" : j.status === "paused" ? "border-l-2 border-yellow-400" : j.status === "stale" ? "border-l-2 border-orange-400" : ""}`}>
                    <input type="checkbox" checked={selected.has(j.id)} onChange={() => toggleSelect(j.id)} className="w-4 h-4 rounded border-gray-300 shrink-0" />
                    <ActiveIndicator status={j.status} />
                    <span className="font-mono text-xs text-gray-400 dark:text-gray-500 w-16 shrink-0">{j.id.slice(0, 8)}</span>
                    {j.source && <SourceBadge source={j.source} />}
                    <span className="truncate text-gray-600 dark:text-gray-300 flex-1 min-w-0 text-xs" title={j.source_url}>{j.source_url}</span>
                    <ProgressBar active={active} />
                    {active ? (
                      <Elapsed since={j.created_at} active={true} />
                    ) : (
                      <span className="text-xs text-gray-400 shrink-0 w-20 text-right">
                        {j.retry_count > 0 && <span className="mr-1">↻{j.retry_count}</span>}
                        {new Date(j.created_at).toLocaleTimeString()}
                      </span>
                    )}
                    <div className="flex gap-1 shrink-0">
                      {j.error_log && (
                        <button onClick={() => setExpandedLog(expandedLog === j.id ? null : j.id)} className="text-xs text-orange-500 hover:underline">{expandedLog === j.id ? "▲" : t("downloads.log")}</button>
                      )}
                      <button onClick={() => setExpandedImports(expandedImports === j.id ? null : j.id)} className="text-xs text-purple-500 hover:underline">{t("jobs.imports")}</button>
                      {(j.status === "pending" || j.status === "downloading") && (
                        <button onClick={() => pauseDL.mutate(j.id)} disabled={pauseDL.isPending} className="text-xs text-yellow-600 hover:underline">{t("jobs.pause")}</button>
                      )}
                      {j.status === "paused" && (
                        <button onClick={() => resumeDL.mutate(j.id)} disabled={resumeDL.isPending} className="text-xs text-green-600 hover:underline">{t("jobs.resume")}</button>
                      )}
                      {(j.status === "failed" || j.status === "stale" || j.status === "complete") && (
                        <button onClick={() => { setRetryId(j.id); retryDL.mutate(j.id); }} disabled={retryDL.isPending} className="text-xs text-blue-600 hover:underline">{t("jobs.retry")}</button>
                      )}
                      <button onClick={() => { setDeleteId(j.id); setDeleteType("dl"); }} className="text-xs text-red-500 hover:underline">{t("jobs.del")}</button>
                    </div>
                  </div>
                  {expandedLog === j.id && j.error_log && (
                    <pre className="text-xs font-mono whitespace-pre-wrap bg-red-50 dark:bg-red-900/20 border border-red-100 dark:border-red-900 rounded p-3 mt-1 max-h-48 overflow-auto">{j.error_log}</pre>
                  )}
                  {expandedImports === j.id && (
                    <ImportJobsList downloadJobId={j.id} />
                  )}
                </div>
              );
            })}
          </div>
        )}
      </section>

      {/* Import Jobs */}
      <section className="mb-8">
        <h3 className="text-base font-semibold mb-2 flex items-center gap-3">
          {t("jobs.import")}
          <select value={imFilter} onChange={(e) => setImFilter(e.target.value)} className="border rounded px-2 py-1 text-xs font-normal dark:bg-slate-700 dark:text-white dark:border-slate-600">
            <option value="">{t("jobs.filter_all_status")}</option>
            <option value="pending">pending</option>
            <option value="running">running</option>
            <option value="complete">complete</option>
            <option value="failed">failed</option>
          </select>
        </h3>
        {imports.isLoading && <div className="space-y-2">{Array.from({ length: 3 }).map((_, i) => <div key={i} className="h-12 bg-gray-100 dark:bg-slate-700 rounded animate-pulse" />)}</div>}
        {imports.data?.items && !imports.data?.items.length && <p className="text-sm text-gray-400 dark:text-gray-500">{t("jobs.no_im")}</p>}
        {imports.data?.items && imports.data?.items.length > 0 && (
          <div className="space-y-1">
            {imports.data?.items?.map((j: any) => {
              const active = j.status === "running";
              return (
                <div key={j.id} className={`bg-white dark:bg-slate-800 rounded-lg shadow-sm p-3 text-sm flex items-center gap-3 ${active ? "border-l-2 border-blue-500" : j.status === "failed" ? "border-l-2 border-red-400" : ""}`}>
                  <ActiveIndicator status={j.status} />
                  <span className="font-mono text-xs text-gray-400 dark:text-gray-500 w-16 shrink-0">{j.id.slice(0, 8)}</span>
                  <span className="font-mono text-xs text-gray-400 truncate flex-1">{j.download_job_id?.slice(0, 8) || "-"}</span>
                  {j.error_log && (
                    <button onClick={() => setExpandedLog(expandedLog === j.id ? null : j.id)} className="text-xs text-orange-500 hover:underline">{expandedLog === j.id ? "▲" : t("downloads.log")}</button>
                  )}
                  {j.status === "failed" && <button onClick={() => { setRetryId(j.id); retryIM.mutate(j.id); }} disabled={retryIM.isPending} className="text-xs text-blue-600 hover:underline">{t("jobs.retry")}</button>}
                  <button onClick={() => { setDeleteId(j.id); setDeleteType("im"); }} className="text-xs text-red-500 hover:underline">{t("jobs.del")}</button>
                </div>
              );
            })}
          </div>
        )}
      </section>

      {(deleteId && deleteType === "dl") && (
        <ConfirmDialog open title={t("jobs.delete_dl_title")} message={t("jobs.delete_dl_msg")} onConfirm={() => deleteDL.mutate(deleteId!)} onCancel={() => setDeleteId(null)} isPending={deleteDL.isPending} error={(deleteDL.error as Error)?.message} />
      )}
      {(deleteId && deleteType === "im") && (
        <ConfirmDialog open title={t("jobs.delete_im_title")} message={t("jobs.delete_im_msg")} onConfirm={() => deleteIM.mutate(deleteId!)} onCancel={() => setDeleteId(null)} isPending={deleteIM.isPending} error={(deleteIM.error as Error)?.message} />
      )}
    </main>
  );
}

// Import jobs for a specific download job
function ImportJobsList({ downloadJobId }: { downloadJobId: string }) {
  const t = useT();
  const toast = useToast();
  const imports = useQuery({
    queryKey: ["import-jobs", downloadJobId],
    queryFn: () => api.getDownloadJobImports(downloadJobId),
  });
  if (imports.isLoading) return <div className="ml-8 mt-1 text-xs text-gray-400">{t("common.loading")}...</div>;
  if (!imports.data?.length) return <div className="ml-8 mt-1 text-xs text-gray-400">{t("jobs.no_imports_yet")}</div>;
  return (
    <div className="ml-8 mt-1 space-y-0.5">
      {imports.data?.map((imp: any) => (
        <div key={imp.id} className="flex items-center gap-2 text-xs bg-gray-50 dark:bg-slate-700/50 rounded px-2 py-1">
          <span className="font-mono text-gray-400">{imp.id.slice(0, 8)}</span>
          <span className={`px-1 rounded ${imp.status === "complete" ? "bg-green-100 text-green-700" : imp.status === "failed" ? "bg-red-100 text-red-600" : imp.status === "running" ? "bg-blue-100 text-blue-600" : "bg-gray-100 text-gray-500"}`}>{imp.status}</span>
          {imp.error_log && <span className="text-orange-500 truncate max-w-xs">{imp.error_log.slice(0, 100)}</span>}
        </div>
      ))}
    </div>
  );
}

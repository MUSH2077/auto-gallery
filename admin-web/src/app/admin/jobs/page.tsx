"use client";
import { useState, useEffect, useCallback } from "react";
import { useT } from "@/lib/i18n";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { api, queryKeys } from "@/lib/api";
import { PageHeader, EmptyState, ErrorState, ConfirmDialog, SourceBadge } from "@/components";

const STATUS_OPTIONS = ["", "pending", "downloading", "paused", "downloaded", "importing", "complete", "failed"];

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
  return (
    <div className="w-20 h-1.5 bg-gray-200 dark:bg-slate-600 rounded-full overflow-hidden shrink-0">
      <div className="h-full bg-blue-500 rounded-full animate-pulse" style={{ width: "60%" }} />
    </div>
  );
}

function ActiveIndicator({ status }: { status: string }) {
  const isActive = status === "downloading" || status === "running" || status === "importing";
  if (isActive) {
    return (
      <span className="flex items-center gap-1.5 shrink-0 w-24">
        <span className="relative flex h-2.5 w-2.5">
          <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-blue-400 opacity-75" />
          <span className="relative inline-flex rounded-full h-2.5 w-2.5 bg-blue-500" />
        </span>
        <span className="text-xs text-blue-600 dark:text-blue-400 font-medium">{status}</span>
      </span>
    );
  }
  return (
    <span className={`shrink-0 w-24 text-xs px-1.5 py-0.5 rounded text-center ${
      status === "complete" || status === "downloaded" ? "bg-green-100 dark:bg-green-900/30 text-green-700 dark:text-green-400" :
      status === "failed" ? "bg-red-100 dark:bg-red-900/30 text-red-600 dark:text-red-400" :
      status === "paused" ? "bg-yellow-100 dark:bg-yellow-900/30 text-yellow-700 dark:text-yellow-400" :
      "bg-gray-100 dark:bg-slate-700 text-gray-500 dark:text-gray-400"
    }`}>{status}</span>
  );
}

export default function JobsPage() {
  const t = useT();
  const router = useRouter();
  const qc = useQueryClient();
  const [dlFilter, setDlFilter] = useState("");
  const [imFilter, setImFilter] = useState("");
  const [retryId, setRetryId] = useState<string | null>(null);
  const [deleteId, setDeleteId] = useState<string | null>(null);
  const [deleteType, setDeleteType] = useState<"dl" | "im">("dl");
  const [expandedLog, setExpandedLog] = useState<string | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [batchConfirm, setBatchConfirm] = useState<string | null>(null);

  const downloads = useQuery({
    queryKey: [...queryKeys.downloadJobs.all, dlFilter],
    queryFn: () => api.listDownloadJobs(dlFilter || undefined, 0, 200),
    refetchInterval: dlFilter === "downloading" || !dlFilter ? 3000 : 10000,
  });

  const imports = useQuery({
    queryKey: [...queryKeys.importJobs.all, imFilter],
    queryFn: () => api.listImportJobs(imFilter || undefined, 0, 200),
    refetchInterval: imFilter === "running" || !imFilter ? 3000 : 10000,
  });

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

  const delDL = useMutation({
    mutationFn: (id: string) => api.deleteDownloadJob(id),
    onSuccess: () => { setDeleteId(null); qc.invalidateQueries(); },
  });

  const delIM = useMutation({
    mutationFn: (id: string) => api.deleteImportJob(id),
    onSuccess: () => { setDeleteId(null); qc.invalidateQueries(); },
  });

  const batchDL = useMutation({
    mutationFn: ({ ids, action }: { ids: string[]; action: string }) => api.batchDownloadJobs(ids, action),
    onSuccess: (data) => {
      setBatchConfirm(null);
      setSelected(new Set());
      qc.invalidateQueries({ queryKey: queryKeys.downloadJobs.all });
      alert(t("jobs.batch_result").replace("{succeeded}", String(data.succeeded)).replace("{failed}", String(data.failed)));
    },
  });

  const getEligibleIds = (action: string): string[] => {
    if (!downloads.data) return [];
    const allowed: Record<string, string[]> = {
      retry: ["failed", "stale", "downloading"],
      pause: ["pending", "downloading"],
      resume: ["paused"],
      delete: ["failed", "stale", "complete", "paused"],
    };
    const ok = allowed[action] || [];
    return downloads.data.filter(j => selected.has(j.id) && ok.includes(j.status)).map(j => j.id);
  };

  const handleBatch = (action: string) => {
    const eligible = getEligibleIds(action);
    if (eligible.length === 0) {
      alert(t("jobs.no_eligible").replace("{action}", t(`jobs.batch_${action}`)));
      return;
    }
    if (eligible.length < selected.size) {
      if (!confirm(
        t("jobs.partial_batch")
          .replace("{eligible}", String(eligible.length))
          .replace("{selected}", String(selected.size))
          .replace("{action}", t(`jobs.batch_${action}`))
      )) return;
    }
    setBatchConfirm(action);
  };

  const toggleSelect = useCallback((id: string) => {
    setSelected(prev => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });
  }, []);

  const toggleAll = useCallback(() => {
    if (!downloads.data) return;
    if (selected.size === downloads.data.length) {
      setSelected(new Set());
    } else {
      setSelected(new Set(downloads.data.map(j => j.id)));
    }
  }, [downloads.data, selected.size]);

  return (
    <main className="max-w-7xl mx-auto p-6">
      <PageHeader title={t("jobs.title")} description={t("jobs.desc")} />

      {/* Download Jobs */}
      <section className="mb-8">
        <div className="flex items-center justify-between mb-3">
          <h2 className="text-lg font-semibold dark:text-white">{t("jobs.downloads_section").replace("{count}", String(downloads.data?.length || 0))}</h2>
          <div className="flex gap-1 bg-gray-100 dark:bg-slate-700 rounded p-0.5">
            {STATUS_OPTIONS.map((s) => (
              <button key={s} onClick={() => { setDlFilter(s); setSelected(new Set()); }}
                className={`px-2.5 py-1 text-xs rounded transition-colors ${dlFilter === s ? "bg-white dark:bg-slate-600 shadow-sm font-medium" : "text-gray-500 hover:text-gray-700 dark:text-gray-400"}`}>
                {s || t("jobs.filter_all")}
              </button>
            ))}
          </div>
        </div>

        {/* Batch action bar */}
        {selected.size > 0 && (
          <div className="flex items-center gap-2 mb-2 px-3 py-2 bg-blue-50 dark:bg-blue-900/20 border border-blue-200 dark:border-blue-800 rounded-lg text-sm">
            <span className="text-blue-700 dark:text-blue-300 text-xs font-medium">{t("jobs.selected").replace("{count}", String(selected.size))}</span>
            <div className="flex gap-1 ml-auto">
              <button onClick={() => handleBatch("retry")}
                className="px-2 py-0.5 text-xs bg-blue-600 text-white rounded hover:bg-blue-700">{t("jobs.batch_retry")}</button>
              <button onClick={() => handleBatch("pause")}
                className="px-2 py-0.5 text-xs bg-yellow-500 text-white rounded hover:bg-yellow-600">{t("jobs.batch_pause")}</button>
              <button onClick={() => handleBatch("resume")}
                className="px-2 py-0.5 text-xs bg-green-600 text-white rounded hover:bg-green-700">{t("jobs.batch_resume")}</button>
              <button onClick={() => handleBatch("delete")}
                className="px-2 py-0.5 text-xs bg-red-500 text-white rounded hover:bg-red-600">{t("jobs.batch_delete")}</button>
              <button onClick={() => setSelected(new Set())}
                className="px-2 py-0.5 text-xs border rounded hover:bg-gray-100 dark:hover:bg-slate-700">{t("common.cancel")}</button>
            </div>
          </div>
        )}

        {downloads.isLoading && <div className="space-y-1">{Array.from({ length: 3 }).map((_, i) => <div key={i} className="h-12 bg-gray-100 dark:bg-slate-700 rounded animate-pulse" />)}</div>}
        {downloads.error && <ErrorState message={(downloads.error as Error).message} />}
        {downloads.data && !downloads.data.length && <EmptyState title={t("jobs.no_downloads")} description={dlFilter ? t("jobs.no_downloads_filter") : t("jobs.no_downloads_desc")} />}

        {downloads.data && downloads.data.length > 0 && (
          <div className="space-y-1">
            {/* Select all checkbox */}
            <div className="flex items-center gap-3 px-3 py-1">
              <input type="checkbox" checked={selected.size === downloads.data.length && downloads.data.length > 0}
                onChange={toggleAll} className="w-4 h-4 rounded border-gray-300" />
              <span className="text-xs text-gray-400 dark:text-gray-500">{t("jobs.select_all")}</span>
            </div>
            {downloads.data.map((j) => {
              const active = j.status === "downloading" || j.status === "pending";
              return (
                <div key={j.id}>
                  <div className={`bg-white dark:bg-slate-800 rounded-lg shadow-sm p-3 text-sm flex items-center gap-3 ${active ? "border-l-2 border-blue-500" : j.status === "failed" ? "border-l-2 border-red-400" : j.status === "paused" ? "border-l-2 border-yellow-400" : ""}`}>
                    <input type="checkbox" checked={selected.has(j.id)} onChange={() => toggleSelect(j.id)}
                      className="w-4 h-4 rounded border-gray-300 shrink-0" />
                    <ActiveIndicator status={j.status} />
                    <span className="font-mono text-xs text-gray-400 dark:text-gray-500 w-16 shrink-0">{j.id.slice(0, 8)}</span>
                    {j.source && <SourceBadge source={j.source} />}
                    <span className="truncate text-gray-600 dark:text-gray-300 flex-1 min-w-0 text-xs">{j.source_url}</span>
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
                      {(j.status === "pending" || j.status === "downloading") && (
                        <button onClick={() => pauseDL.mutate(j.id)} disabled={pauseDL.isPending}
                          className="text-xs text-yellow-600 hover:underline">{t("jobs.pause")}</button>
                      )}
                      {j.status === "paused" && (
                        <button onClick={() => resumeDL.mutate(j.id)} disabled={resumeDL.isPending}
                          className="text-xs text-green-600 hover:underline">{t("jobs.resume")}</button>
                      )}
                      {j.status === "failed" && (
                        <button onClick={() => { setRetryId(j.id); retryDL.mutate(j.id); }} disabled={retryDL.isPending}
                          className="text-xs text-blue-600 hover:underline">{t("jobs.retry")}</button>
                      )}
                      <button onClick={() => { setDeleteId(j.id); setDeleteType("dl"); }} className="text-xs text-red-500 hover:underline">{t("jobs.del")}</button>
                    </div>
                  </div>
                  {expandedLog === j.id && j.error_log && (
                    <pre className="text-xs font-mono whitespace-pre-wrap bg-red-50 dark:bg-red-900/20 border border-red-100 dark:border-red-900 rounded p-3 mt-1 max-h-48 overflow-auto">{j.error_log}</pre>
                  )}
                </div>
              );
            })}
          </div>
        )}
      </section>

      {/* Import Jobs */}
      <section className="mb-8">
        <div className="flex items-center justify-between mb-3">
          <h2 className="text-lg font-semibold dark:text-white">{t("jobs.imports_section").replace("{count}", String(imports.data?.total ?? 0))}</h2>
          <div className="flex gap-1 bg-gray-100 dark:bg-slate-700 rounded p-0.5">
            {["", "pending", "running", "complete", "failed"].map((s) => (
              <button key={s} onClick={() => setImFilter(s)}
                className={`px-2.5 py-1 text-xs rounded transition-colors ${imFilter === s ? "bg-white dark:bg-slate-600 shadow-sm font-medium" : "text-gray-500 hover:text-gray-700 dark:text-gray-400"}`}>
                {s || t("jobs.filter_all")}
              </button>
            ))}
          </div>
        </div>

        {imports.isLoading && <div className="space-y-1">{Array.from({ length: 3 }).map((_, i) => <div key={i} className="h-12 bg-gray-100 dark:bg-slate-700 rounded animate-pulse" />)}</div>}
        {imports.error && <ErrorState message={(imports.error as Error).message} />}
        {imports.data && !imports.data.items?.length && <EmptyState title={t("jobs.no_imports")} description={imFilter ? t("jobs.no_downloads_filter") : t("jobs.no_imports_desc")} />}

        {imports.data && (imports.data.items?.length ?? 0) > 0 && (
          <div className="space-y-1">
            {imports.data.items.map((j) => {
              const active = j.status === "running";
              return (
                <div key={j.id}>
                  <div className={`bg-white dark:bg-slate-800 rounded-lg shadow-sm p-3 text-sm flex items-center gap-3 ${active ? "border-l-2 border-blue-500" : j.status === "failed" ? "border-l-2 border-red-400" : ""}`}>
                    <ActiveIndicator status={j.status} />
                    <span className="font-mono text-xs text-gray-400 dark:text-gray-500 w-16 shrink-0">{j.id.slice(0, 8)}</span>
                    <span className="font-mono text-xs text-gray-400 truncate flex-1">DL: {j.download_job_id ? j.download_job_id.slice(0, 8) : "—"}</span>
                    <ProgressBar active={active} />
                    {active ? (
                      <Elapsed since={j.created_at} active={true} />
                    ) : (
                      <span className="text-xs text-gray-400 shrink-0 w-20 text-right">{new Date(j.created_at).toLocaleTimeString()}</span>
                    )}
                    <div className="flex gap-1 shrink-0">
                      {j.error_log && (
                        <button onClick={() => setExpandedLog(expandedLog === j.id ? null : j.id)} className="text-xs text-orange-500 hover:underline">{expandedLog === j.id ? "▲" : t("downloads.log")}</button>
                      )}
                      {j.status === "failed" && (
                        <button onClick={() => { setRetryId(j.id); retryIM.mutate(j.id); }} disabled={retryIM.isPending}
                          className="text-xs text-blue-600 hover:underline">{t("jobs.retry")}</button>
                      )}
                      <button onClick={() => { setDeleteId(j.id); setDeleteType("im"); }} className="text-xs text-red-500 hover:underline">{t("jobs.del")}</button>
                    </div>
                  </div>
                  {expandedLog === j.id && j.error_log && (
                    <pre className="text-xs font-mono whitespace-pre-wrap bg-red-50 dark:bg-red-900/20 border border-red-100 dark:border-red-900 rounded p-3 mt-1 max-h-48 overflow-auto">{j.error_log}</pre>
                  )}
                </div>
              );
            })}
          </div>
        )}
      </section>

      {deleteId && <ConfirmDialog open title={t("jobs.delete_title")} message={t("jobs.delete_msg")} onConfirm={() => deleteType === "dl" ? delDL.mutate(deleteId) : delIM.mutate(deleteId)} onCancel={() => setDeleteId(null)} isPending={delDL.isPending || delIM.isPending} />}

      {batchConfirm && (
        <ConfirmDialog open title={t(`jobs.batch_${batchConfirm}`)} message={`${t(`jobs.batch_${batchConfirm}`)} ${getEligibleIds(batchConfirm).length} jobs?`}
          onConfirm={() => batchDL.mutate({ ids: getEligibleIds(batchConfirm), action: batchConfirm })}
          onCancel={() => setBatchConfirm(null)} isPending={batchDL.isPending} />
      )}
    </main>
  );
}

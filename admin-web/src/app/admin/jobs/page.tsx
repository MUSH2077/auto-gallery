"use client";
import Link from "next/link";
import { Suspense, useState, useEffect, useMemo, type ReactNode } from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useToast } from "@/components/Toast";
import { useT } from "@/lib/i18n";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api, DownloadJob, ImportJob, queryKeys } from "@/lib/api";
import { PageHeader, EmptyState, ErrorState, ConfirmDialog, SourceBadge, RealProgressBar, PipelineVisualizer, BatchByFilter } from "@/components";
import { useJobWebSocket } from "@/lib/useWebSocket";
import { statusLabel, useI18nFormat } from "@/lib/i18n-format";


const REFETCH_ACTIVE_MS = 3000;
const REFETCH_IDLE_MS = 10000;
const PAGE_LIMIT = 200;

const STATUS_OPTIONS = ["", "enqueued", "downloading", "paused", "downloaded", "importing", "complete", "failed", "stale", "cancelled"];
const IMPORT_STATUS_OPTIONS = ["", "pending", "running", "complete", "failed", "stale"];
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

// ProgressBar replaced by RealProgressBar from components — data-driven with actual percent/current/total

function ActiveIndicator({ status }: { status: string }) {
  const t = useT();
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
      <span className="text-xs text-blue-600 dark:text-blue-400 font-medium">{statusLabel(t, status)}</span>
    </span>;
  }
  return <span className={`shrink-0 w-28 text-xs px-1.5 py-0.5 rounded text-center ${color}`}>{statusLabel(t, status)}</span>;
}

function SummaryCard({ label, value, sub, tone = "neutral" }: { label: string; value: number | string; sub?: string; tone?: "neutral" | "active" | "danger" | "warning" }) {
  const color = tone === "danger" ? "text-[#cf222e] dark:text-[#f85149]"
    : tone === "warning" ? "text-[#9a6700] dark:text-[#d29922]"
      : tone === "active" ? "text-[#0969da] dark:text-[#58a6ff]"
        : "text-[#24292f] dark:text-[#e6edf3]";
  return (
    <div className="card p-4">
      <div className={`tabular text-2xl font-semibold ${color}`}>{value}</div>
      <div className="mt-1 text-xs font-medium uppercase text-[#57606a] dark:text-[#8b949e]">{label}</div>
      {sub && <div className="mt-1 text-xs text-[#8c959f] dark:text-[#6e7681]">{sub}</div>}
    </div>
  );
}

function JobLifecycle({ status }: { status: string }) {
  const t = useT();
  const steps = ["created", "downloading", "downloaded", "importing", status === "failed" || status === "stale" ? status : "complete"];
  const activeIndex = status === "pending" ? 0
    : status === "downloading" ? 1
      : status === "downloaded" ? 2
        : status === "importing" ? 3
          : status === "complete" ? 4
            : status === "failed" || status === "stale" ? 4
              : 0;
  const failed = status === "failed" || status === "stale";
  return (
    <div className="hidden min-w-[240px] items-center gap-1 lg:flex">
      {steps.map((step, index) => {
        const done = index < activeIndex || (index === activeIndex && status === "complete");
        const active = index === activeIndex && status !== "complete";
        const danger = failed && index === activeIndex;
        return (
          <div key={`${step}-${index}`} className="flex min-w-0 flex-1 items-center gap-1">
            <span
              title={t(`jobs.lifecycle_${step}`, step)}
              className={`h-2 w-2 shrink-0 rounded-full ${
                danger ? "bg-[#cf222e]" : active ? "animate-pulse bg-[#0969da]" : done ? "bg-[#1a7f37]" : "bg-[#d8dee4] dark:bg-[#30363d]"
              }`}
            />
            {index < steps.length - 1 && <span className={`h-px flex-1 ${done ? "bg-[#1a7f37]" : "bg-[#d8dee4] dark:bg-[#30363d]"}`} />}
          </div>
        );
      })}
    </div>
  );
}

function ErrorExcerpt({ value }: { value?: string | null }) {
  if (!value) return null;
  const firstLine = value.split("\n").find(Boolean) || value;
  return (
    <div className="mt-1 line-clamp-1 rounded-md border border-[#cf222e]/20 bg-[#ffebe9] px-2 py-1 text-xs text-[#cf222e] dark:border-[#f85149]/30 dark:bg-[#f8514926] dark:text-[#f85149]">
      {firstLine.slice(0, 180)}
    </div>
  );
}

function shortId(id?: string | null) {
  return id ? id.slice(0, 8) : "-";
}

function JsonBlock({ value }: { value: unknown }) {
  if (!value) return null;
  return (
    <pre className="max-h-64 overflow-auto rounded-md border border-[#d8dee4] bg-[#f6f8fa] p-3 font-mono text-xs whitespace-pre-wrap dark:border-[#30363d] dark:bg-[#0d1117]">
      {JSON.stringify(value, null, 2)}
    </pre>
  );
}

function DetailRow({ label, value }: { label: string; value?: ReactNode }) {
  return (
    <div className="grid grid-cols-[120px_minmax(0,1fr)] gap-3 border-b border-[#d8dee4] py-2 text-sm last:border-b-0 dark:border-[#30363d]">
      <dt className="text-xs font-medium uppercase text-[#57606a] dark:text-[#8b949e]">{label}</dt>
      <dd className="min-w-0 break-all">{value || "—"}</dd>
    </div>
  );
}

function JobDetailDrawer({
  kind,
  id,
  onClose,
  onRetryDownload,
  onPauseDownload,
  onResumeDownload,
  onDeleteDownload,
  onRetryImport,
  onDeleteImport,
}: {
  kind: "download" | "import";
  id: string | null;
  onClose: () => void;
  onRetryDownload: (id: string) => void;
  onPauseDownload: (id: string) => void;
  onResumeDownload: (id: string) => void;
  onDeleteDownload: (id: string) => void;
  onRetryImport: (id: string) => void;
  onDeleteImport: (id: string) => void;
}) {
  const t = useT();
  const fmt = useI18nFormat();
  const download = useQuery({
    queryKey: queryKeys.downloadJobs.detail(id || ""),
    queryFn: () => api.getDownloadJob(id || ""),
    enabled: !!id && kind === "download",
  });
  const imports = useQuery({
    queryKey: queryKeys.downloadJobs.imports(id || ""),
    queryFn: () => api.getDownloadJobImports(id || ""),
    enabled: !!id && kind === "download",
  });
  const importJob = useQuery({
    queryKey: [...queryKeys.importJobs.all, "detail", id],
    queryFn: () => api.getImportJob(id || ""),
    enabled: !!id && kind === "import",
  });

  if (!id) return null;
  const dl = download.data as DownloadJob | undefined;
  const im = importJob.data as ImportJob | undefined;
  const loading = kind === "download" ? download.isLoading : importJob.isLoading;
  const error = kind === "download" ? download.error : importJob.error;

  return (
    <aside className="fixed inset-y-0 right-0 z-40 flex w-full max-w-xl flex-col border-l border-[#d8dee4] bg-white shadow-xl dark:border-[#30363d] dark:bg-[#161b22]" aria-label={t("jobs.detail_title")}>
      <div className="flex items-center justify-between border-b border-[#d8dee4] px-4 py-3 dark:border-[#30363d]">
        <div className="min-w-0">
          <div className="text-sm font-semibold">{kind === "download" ? t("jobs.download_detail") : t("jobs.import_detail")}</div>
          <div className="font-mono text-xs text-[#57606a] dark:text-[#8b949e]">{shortId(id)}</div>
        </div>
        <button onClick={onClose} className="btn-icon border-0 text-lg leading-none" aria-label={t("common.close")}>×</button>
      </div>
      <div className="min-h-0 flex-1 overflow-auto p-4">
        {loading && <div className="h-24 animate-pulse rounded-md bg-[#eaeef2] dark:bg-[#21262d]" />}
        {error && <ErrorState message={(error as Error).message} onRetry={() => (kind === "download" ? download.refetch() : importJob.refetch())} />}

        {kind === "download" && dl && (
          <div className="space-y-4">
            <div className="flex flex-wrap gap-2">
              <button onClick={() => onRetryDownload(dl.id)} className="btn-primary text-xs">{t("jobs.retry")}</button>
              {["pending", "downloading"].includes(dl.status) && <button onClick={() => onPauseDownload(dl.id)} className="btn-ghost text-xs">{t("jobs.pause")}</button>}
              {dl.status === "paused" && <button onClick={() => onResumeDownload(dl.id)} className="btn-ghost text-xs">{t("jobs.resume")}</button>}
              <button onClick={() => onDeleteDownload(dl.id)} className="btn-danger text-xs">{t("jobs.del")}</button>
            </div>
            <dl className="rounded-md border border-[#d8dee4] px-3 dark:border-[#30363d]">
              <DetailRow label={t("jobs.status")} value={statusLabel(t, dl.status)} />
              <DetailRow label={t("jobs.source")} value={<span className="inline-flex items-center gap-2"><SourceBadge source={dl.source} />{dl.source}</span>} />
              <DetailRow label={t("jobs.source_url")} value={dl.source_url} />
              <DetailRow label={t("jobs.creator")} value={dl.creator_id ? <Link href={`/admin/creators/${dl.creator_id}`} className="text-[#0969da] hover:underline dark:text-[#58a6ff]">{dl.creator_name || shortId(dl.creator_id)}</Link> : dl.creator_name} />
              <DetailRow label={t("jobs.subscription")} value={dl.subscription_id ? <Link href={`/admin/subscriptions/${dl.subscription_id}`} className="text-[#0969da] hover:underline dark:text-[#58a6ff]">{dl.subscription_name || shortId(dl.subscription_id)}</Link> : undefined} />
              <DetailRow label={t("jobs.repository")} value={dl.subscription_source_id ? <Link href={`/admin/repositories/${dl.subscription_source_id}`} className="text-[#0969da] hover:underline dark:text-[#58a6ff]">{shortId(dl.subscription_source_id)}</Link> : undefined} />
              <DetailRow label={t("jobs.created")} value={fmt.dateTime(dl.created_at)} />
              <DetailRow label={t("jobs.updated")} value={fmt.dateTime(dl.updated_at)} />
            </dl>
            {dl.error_log && (
              <section>
                <h3 className="mb-2 text-sm font-semibold">{t("jobs.error_log")}</h3>
                <pre className="max-h-64 overflow-auto rounded-md border border-[#cf222e]/20 bg-[#ffebe9] p-3 font-mono text-xs whitespace-pre-wrap text-[#cf222e] dark:border-[#f85149]/30 dark:bg-[#f8514926] dark:text-[#f85149]">{dl.error_log}</pre>
              </section>
            )}
            {dl.manifest && (
              <section>
                <h3 className="mb-2 text-sm font-semibold">{t("jobs.manifest")}</h3>
                <JsonBlock value={dl.manifest} />
              </section>
            )}
            <section>
              <h3 className="mb-2 text-sm font-semibold">{t("jobs.related_imports")}</h3>
              {imports.isLoading && <div className="h-12 animate-pulse rounded bg-[#eaeef2] dark:bg-[#21262d]" />}
              {imports.data?.length ? (
                <div className="space-y-1">
                  {imports.data.map((job: ImportJob) => (
                    <Link key={job.id} href={`/admin/jobs?tab=imports&download_job_id=${dl.id}&import_job=${job.id}`} className="flex items-center justify-between rounded-md border border-[#d8dee4] px-3 py-2 text-sm hover:bg-[#f6f8fa] dark:border-[#30363d] dark:hover:bg-[#21262d]">
                      <span className="font-mono text-xs">{shortId(job.id)}</span>
                      <span>{statusLabel(t, job.status)}</span>
                    </Link>
                  ))}
                </div>
              ) : <p className="text-xs text-[#57606a] dark:text-[#8b949e]">{t("jobs.no_imports_yet")}</p>}
            </section>
          </div>
        )}

        {kind === "import" && im && (
          <div className="space-y-4">
            <div className="flex flex-wrap gap-2">
              {["failed", "stale"].includes(im.status) && <button onClick={() => onRetryImport(im.id)} className="btn-primary text-xs">{t("jobs.retry")}</button>}
              <button onClick={() => onDeleteImport(im.id)} className="btn-danger text-xs">{t("jobs.del")}</button>
              <Link href={`/admin/jobs?tab=downloads&job=${im.download_job_id}`} className="btn-ghost text-xs">{t("jobs.open_download")}</Link>
            </div>
            <dl className="rounded-md border border-[#d8dee4] px-3 dark:border-[#30363d]">
              <DetailRow label={t("jobs.status")} value={statusLabel(t, im.status)} />
              <DetailRow label={t("jobs.download_job")} value={<Link href={`/admin/jobs?tab=downloads&job=${im.download_job_id}`} className="text-[#0969da] hover:underline dark:text-[#58a6ff]">{shortId(im.download_job_id)}</Link>} />
              <DetailRow label={t("jobs.created")} value={fmt.dateTime(im.created_at)} />
              <DetailRow label={t("jobs.updated")} value={fmt.dateTime(im.updated_at)} />
            </dl>
            {im.error_log && (
              <section>
                <h3 className="mb-2 text-sm font-semibold">{t("jobs.error_log")}</h3>
                <pre className="max-h-80 overflow-auto rounded-md border border-[#cf222e]/20 bg-[#ffebe9] p-3 font-mono text-xs whitespace-pre-wrap text-[#cf222e] dark:border-[#f85149]/30 dark:bg-[#f8514926] dark:text-[#f85149]">{im.error_log}</pre>
              </section>
            )}
          </div>
        )}
      </div>
    </aside>
  );
}

function JobsContent() {
  const t = useT();
  const fmt = useI18nFormat();
  const toast = useToast();
  const qc = useQueryClient();
  const [downloadProgress, setDownloadProgress] = useState<Record<string, { stage: string; current: number; total: number; percent: number }>>({});
  const [downloadPipeline, setDownloadPipeline] = useState<Record<string, { current_stage: string; stages: Array<{ name: string; status: string }> }>>({});

  // WebSocket: invalidate queries on status change, update progress on progress events
  useJobWebSocket({
    onStatusChange: (msg) => {
      qc.invalidateQueries({ queryKey: queryKeys.downloadJobs.all });
      qc.invalidateQueries({ queryKey: queryKeys.importJobs.all });
      qc.invalidateQueries({ queryKey: ["workbench"] });
      if (msg.new_status) {
        toast.info(`${msg.task_id.slice(0, 8)}: ${msg.old_status} → ${msg.new_status}`);
      }
    },
    onProgress: (msg) => {
      if (msg.task_type === "download" && msg.progress) {
        setDownloadProgress(prev => ({ ...prev, [msg.task_id]: msg.progress as any }));
      }
    },
  });
  const router = useRouter();
  const pathname = usePathname();
  const sp = useSearchParams();

  const activeTab = (sp.get("tab") === "imports" ? "imports" : "downloads") as "downloads" | "imports";
  const status = sp.get("status") || "";
  const dlSource = sp.get("source") || "";
  const subscriptionSourceId = sp.get("subscription_source_id") || "";
  const downloadJobId = sp.get("download_job_id") || "";
  const search = sp.get("q") || "";
  const dlSort = sp.get("sort") || "created_at";
  const dlOrder = sp.get("order") || "desc";
  const selectedDownloadJobId = sp.get("job");
  const selectedImportJobId = sp.get("import_job");

  const [retryId, setRetryId] = useState<string | null>(null);
  const [deleteId, setDeleteId] = useState<string | null>(null);
  const [deleteType, setDeleteType] = useState<"dl" | "im">("dl");
  const [expandedLog, setExpandedLog] = useState<string | null>(null);
  const [expandedImports, setExpandedImports] = useState<string | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [selectAll, setSelectAll] = useState(false);

  const updateParams = (updates: Record<string, string | null>, replace = true) => {
    const next = new URLSearchParams(sp.toString());
    Object.entries(updates).forEach(([key, value]) => {
      if (!value) next.delete(key); else next.set(key, value);
    });
    const href = next.toString() ? `${pathname}?${next.toString()}` : pathname;
    if (replace) router.replace(href, { scroll: false }); else router.push(href, { scroll: false });
  };

  const openDownloadDetail = (id: string) => updateParams({ tab: "downloads", job: id, import_job: null }, false);
  const openImportDetail = (id: string) => updateParams({ tab: "imports", import_job: id, job: null }, false);
  const closeDetail = () => updateParams({ job: null, import_job: null });

  const dlParams = useMemo(() => ({
    status: activeTab === "downloads" ? status || undefined : undefined,
    source: dlSource || undefined,
    subscription_source_id: subscriptionSourceId || undefined,
    q: search || undefined,
    sort_by: dlSort,
    sort_order: dlOrder,
    offset: 0, limit: PAGE_LIMIT,
  }), [activeTab, status, dlSource, subscriptionSourceId, search, dlSort, dlOrder]);

  const downloads = useQuery({
    queryKey: [...queryKeys.downloadJobs.all, dlParams],
    queryFn: () => api.listDownloadJobs(dlParams),
    refetchInterval: (status === "downloading" || !status) ? REFETCH_ACTIVE_MS : REFETCH_IDLE_MS,
  });

  const workbench = useQuery({
    queryKey: queryKeys.workbench,
    queryFn: api.workbench,
    refetchInterval: (query) => {
      const active = (query.state.data?.queue.active_download_count || 0) + (query.state.data?.queue.active_import_count || 0);
      return active > 0 ? REFETCH_ACTIVE_MS : REFETCH_IDLE_MS;
    },
  });

  const imports = useQuery({
    queryKey: [...queryKeys.importJobs.all, activeTab, status, downloadJobId, search],
    queryFn: () => api.listImportJobs({
      status: activeTab === "imports" ? status || undefined : undefined,
      download_job_id: downloadJobId || undefined,
      q: search || undefined,
      offset: 0,
      limit: PAGE_LIMIT,
    }),
    refetchInterval: (status === "running" || !status) ? REFETCH_ACTIVE_MS : REFETCH_IDLE_MS,
  });

  const activeFilterCount = [
    status,
    dlSource,
    subscriptionSourceId,
    downloadJobId,
    search,
    dlSort !== "created_at" || dlOrder !== "desc",
  ].filter(Boolean).length;
  const lastUpdated = Math.max(downloads.dataUpdatedAt || 0, imports.dataUpdatedAt || 0, workbench.dataUpdatedAt || 0);
  const refreshAll = () => {
    qc.invalidateQueries({ queryKey: queryKeys.downloadJobs.all });
    qc.invalidateQueries({ queryKey: queryKeys.importJobs.all });
    qc.invalidateQueries({ queryKey: queryKeys.workbench });
  };
  const clearFilters = () => updateParams({
    status: null,
    source: null,
    subscription_source_id: null,
    download_job_id: null,
    q: null,
    sort: null,
    order: null,
  });

  // --- Mutations ---
  const retryDL = useMutation({
    mutationFn: (id: string) => api.retryDownloadJob(id),
    onSuccess: () => { setRetryId(null); qc.invalidateQueries({ queryKey: queryKeys.downloadJobs.all }); qc.invalidateQueries({ queryKey: queryKeys.downloadJobs.detail(selectedDownloadJobId || "") }); },
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
    onSettled: () => { qc.invalidateQueries({ queryKey: queryKeys.downloadJobs.all }); qc.invalidateQueries({ queryKey: queryKeys.downloadJobs.detail(selectedDownloadJobId || "") }); },
  });
  const resumeDL = useMutation({
    mutationFn: (id: string) => api.resumeDownloadJob(id),
    onMutate: (id) => {
      qc.setQueriesData({ queryKey: queryKeys.downloadJobs.all }, (old: any) => {
        if (!Array.isArray(old)) return old;
        return old.map((j: any) => j.id === id ? { ...j, status: "pending" } : j);
      });
    },
    onSettled: () => { qc.invalidateQueries({ queryKey: queryKeys.downloadJobs.all }); qc.invalidateQueries({ queryKey: queryKeys.downloadJobs.detail(selectedDownloadJobId || "") }); },
  });
  const deleteDL = useMutation({
    mutationFn: (id: string) => api.deleteDownloadJob(id),
    onSuccess: () => { setDeleteId(null); closeDetail(); qc.invalidateQueries({ queryKey: queryKeys.downloadJobs.all }); },
  });
  const deleteIM = useMutation({
    mutationFn: (id: string) => api.deleteImportJob(id),
    onSuccess: () => { setDeleteId(null); closeDetail(); qc.invalidateQueries({ queryKey: queryKeys.importJobs.all }); },
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
      toast.info(t("jobs.batch_result", { succeeded: data.succeeded, failed: data.failed }));
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
      pause: ["enqueued", "downloading", "downloaded", "importing", "failed", "stale"],
      resume: ["paused"],
      delete: ["enqueued", "downloading", "downloaded", "failed", "stale", "complete", "paused", "cancelled"],
      cancel: ["enqueued", "downloading", "downloaded", "importing", "failed", "stale", "paused"],
    };
    const ok = allowed[action] || [];
    return downloads.data.filter((j: any) => selected.has(j.id) && ok.includes(j.status)).map((j: any) => j.id);
  };

  const handleBatch = (action: string) => {
    const eligible = getEligibleIds(action);
    if (eligible.length === 0) { toast.warning({ message: t("common.no_eligible") }); return; }
    if (eligible.length < selected.size && !confirm(t("common.partial_selected"))) return;
    if (action === "cancel") {
      // Cancel each job individually (different API)
      Promise.all(eligible.map((id: string) => api.cancelDownloadJob(id).catch(() => null)))
        .then(() => { qc.invalidateQueries({ queryKey: queryKeys.downloadJobs.all }); toast.info(t("jobs.batch_result", { succeeded: eligible.length, failed: 0 })); });
      setSelected(new Set()); setSelectAll(false);
      return;
    }
    batchDL.mutate({ ids: eligible, action });
  };

  // Clear helpers
  const handleClear = (statuses: string[]) => {
    if (confirm(t("jobs.delete_all_confirm", { statuses: statuses.map((s) => statusLabel(t, s)).join(", ") }))) clearDL.mutate(statuses);
  };

  return (
    <main className="max-w-7xl mx-auto p-6">
      <PageHeader title={t("jobs.title")} description={t("jobs.desc")}>
        <div className="flex flex-wrap gap-2">
          <button onClick={refreshAll} className="btn-ghost text-xs">{t("jobs.refresh")}</button>
          <BatchByFilter />
          <button onClick={() => handleClear(["failed", "stale"])} className="btn-danger text-xs">{t("jobs.clear_failed")}</button>
          <button onClick={() => handleClear(["complete"])} className="btn-ghost text-xs">{t("jobs.clear_complete")}</button>
          <button onClick={() => killStuck.mutate()} disabled={killStuck.isPending} className="btn-ghost text-xs">{t("jobs.kill_stuck")}</button>
          <button onClick={() => retryAllFailed.mutate()} disabled={retryAllFailed.isPending} className="btn-primary text-xs">{t("jobs.retry_all_failed")}</button>
        </div>
      </PageHeader>

      {workbench.data && (
        <div className="mb-4 grid grid-cols-2 gap-3 md:grid-cols-5">
          <SummaryCard label={t("jobs.summary_active")} value={workbench.data.queue.active_download_count + workbench.data.queue.active_import_count} sub={t("jobs.summary_active_sub")} tone="active" />
          <SummaryCard label={t("jobs.summary_queued")} value={workbench.data.queue.default} sub={t("jobs.summary_queued_sub")} />
          <SummaryCard label={t("jobs.summary_importing")} value={workbench.data.queue.active_import_count} sub={t("jobs.summary_importing_sub")} tone={workbench.data.queue.active_import_count ? "active" : "neutral"} />
          <SummaryCard label={t("jobs.summary_failed")} value={workbench.data.queue.failed_download_count + workbench.data.queue.failed_import_count} tone={workbench.data.queue.failed_download_count + workbench.data.queue.failed_import_count ? "danger" : "neutral"} />
          <SummaryCard label={t("jobs.summary_stale")} value={workbench.data.queue.stale_count} sub={t("jobs.summary_stale_sub")} tone={workbench.data.queue.stale_count ? "warning" : "neutral"} />
        </div>
      )}

      <div className="mb-4 flex flex-col gap-3 rounded-md border border-[#d8dee4] bg-white p-3 dark:border-[#30363d] dark:bg-[#161b22]">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="flex gap-1 rounded-md bg-[#f6f8fa] p-1 dark:bg-[#21262d]">
            <button onClick={() => updateParams({ tab: "downloads", status: null, import_job: null })} className={`rounded px-3 py-1.5 text-xs font-medium ${activeTab === "downloads" ? "bg-white shadow-sm dark:bg-[#30363d]" : "text-[#57606a] dark:text-[#8b949e]"}`}>{t("jobs.download")}</button>
            <button onClick={() => updateParams({ tab: "imports", status: null, job: null })} className={`rounded px-3 py-1.5 text-xs font-medium ${activeTab === "imports" ? "bg-white shadow-sm dark:bg-[#30363d]" : "text-[#57606a] dark:text-[#8b949e]"}`}>{t("jobs.import")}</button>
          </div>
          <div className="flex flex-wrap items-center gap-2 text-xs text-[#57606a] dark:text-[#8b949e]">
            {activeFilterCount > 0 && <span className="rounded-full bg-[#ddf4ff] px-2 py-0.5 font-medium text-[#0969da] dark:bg-[#1f6feb26] dark:text-[#58a6ff]">{t("jobs.active_filters", { count: activeFilterCount })}</span>}
            <span>{t("jobs.last_refreshed", { time: lastUpdated ? fmt.time(new Date(lastUpdated).toISOString()) : "—" })}</span>
            {activeFilterCount > 0 && <button onClick={clearFilters} className="text-[#0969da] hover:underline dark:text-[#58a6ff]">{t("jobs.clear_filters")}</button>}
          </div>
        </div>

      {/* Filters */}
        <div className="flex flex-wrap items-center gap-2">
          <input value={search} onChange={(e) => updateParams({ q: e.target.value || null })} className="input min-w-[220px] px-3 py-1.5 text-xs" placeholder={t("jobs.search_placeholder")} aria-label={t("jobs.search_placeholder")} />
        <select value={status} onChange={(e) => updateParams({ status: e.target.value || null })} className="select px-2 py-1.5 text-xs">
          <option value="">{t("jobs.filter_all_status")}</option>
          {(activeTab === "imports" ? IMPORT_STATUS_OPTIONS : STATUS_OPTIONS).filter(Boolean).map(s => <option key={s} value={s}>{statusLabel(t, s)}</option>)}
        </select>
        {activeTab === "downloads" && <select value={dlSource} onChange={(e) => updateParams({ source: e.target.value || null })} className="select px-2 py-1.5 text-xs">
          <option value="">{t("jobs.filter_all_source")}</option>
          {SOURCE_OPTIONS.filter(Boolean).map(s => <option key={s} value={s}>{s}</option>)}
        </select>}
        {subscriptionSourceId && <span className="rounded-md border border-[#d8dee4] px-2 py-1 text-xs font-mono dark:border-[#30363d]">{t("jobs.repository")} {shortId(subscriptionSourceId)}</span>}
        {downloadJobId && <span className="rounded-md border border-[#d8dee4] px-2 py-1 text-xs font-mono dark:border-[#30363d]">{t("jobs.download_job")} {shortId(downloadJobId)}</span>}
        {activeTab === "downloads" && <select value={`${dlSort}-${dlOrder}`} onChange={(e) => { const [k, o] = e.target.value.split("-"); updateParams({ sort: k === "created_at" && o === "desc" ? null : k, order: o === "desc" ? null : o }); }} className="select px-2 py-1.5 text-xs">
          <option value="created_at-desc">{t("jobs.sort_newest")}</option>
          <option value="created_at-asc">{t("jobs.sort_oldest")}</option>
          <option value="status-asc">{t("jobs.sort_status")}</option>
          <option value="source-asc">{t("jobs.sort_source")}</option>
        </select>}
        {activeTab === "downloads" && <button onClick={handleSelectAll} className="btn-ghost text-xs">{selectAll ? t("common.deselect_all") : t("common.select_all")}</button>}

        {selected.size > 0 && (
          <div className="ml-auto flex items-center gap-1 rounded-md border border-[#bf8700]/30 bg-[#fff8c5] px-3 py-1.5 dark:bg-[#bb800926]">
            <span className="text-xs text-[#9a6700] dark:text-[#d29922]">{selected.size} {t("common.selected")}</span>
            <button onClick={() => handleBatch("pause")} className="px-2 py-0.5 text-xs bg-yellow-500 text-white rounded hover:bg-yellow-600">{t("jobs.batch_pause")}</button>
            <button onClick={() => handleBatch("resume")} className="px-2 py-0.5 text-xs bg-green-500 text-white rounded hover:bg-green-600">{t("jobs.batch_resume")}</button>
            <button onClick={() => handleBatch("retry")} className="px-2 py-0.5 text-xs bg-blue-500 text-white rounded hover:bg-blue-600">{t("jobs.batch_retry")}</button>
            <button onClick={() => handleBatch("cancel")} className="px-2 py-0.5 text-xs bg-gray-500 text-white rounded hover:bg-gray-600">{t("jobs.batch_cancel")}</button>
            <button onClick={() => handleBatch("delete")} className="px-2 py-0.5 text-xs bg-red-500 text-white rounded hover:bg-red-600">{t("jobs.batch_delete")}</button>
          </div>
        )}
        </div>
      </div>

      {/* Download Jobs list */}
      {activeTab === "downloads" && <section className="mb-8">
        {downloads.isLoading && <div className="space-y-2">{Array.from({ length: 5 }).map((_, i) => <div key={i} className="h-14 rounded-md bg-[#eaeef2] dark:bg-[#21262d] animate-pulse" />)}</div>}
        {downloads.error && <ErrorState message={(downloads.error as Error).message} onRetry={() => downloads.refetch()} />}
        {downloads.data && !downloads.data?.length && <EmptyState title={t("jobs.no_dl")} description={t("jobs.no_dl_desc")} />}
        {downloads.data && downloads.data?.length > 0 && (
          <div className="overflow-x-auto pb-2">
          <div className="min-w-[980px] space-y-1">
            {downloads.data.map((j: any) => {
              const active = j.status === "downloading" || j.status === "pending";
              return (
                <div key={j.id}>
                  <div onClick={() => openDownloadDetail(j.id)} className={`card flex cursor-pointer items-center gap-3 p-3 text-sm hover:border-[#0969da]/50 ${active ? "border-l-2 border-l-[#0969da]" : j.status === "failed" ? "border-l-2 border-l-[#cf222e]" : j.status === "paused" ? "border-l-2 border-l-[#bf8700]" : j.status === "stale" ? "border-l-2 border-l-[#d29922]" : ""}`}>
                    <input type="checkbox" checked={selected.has(j.id)} onClick={(e) => e.stopPropagation()} onChange={() => toggleSelect(j.id)} className="w-4 h-4 rounded border-gray-300 shrink-0" />
                    <ActiveIndicator status={j.status} />
                    <span className="w-16 shrink-0 font-mono text-xs text-[#57606a] dark:text-[#8b949e]">{j.id.slice(0, 8)}</span>
                    {j.source && <SourceBadge source={j.source} />}
                    <div className="min-w-[9rem] max-w-[12rem] shrink-0 leading-tight">
                      <Link
                        href={`/admin/subscriptions/${j.subscription_id}`}
                        onClick={(e) => e.stopPropagation()}
                        className="block truncate text-xs font-medium text-[#0969da] hover:underline dark:text-[#58a6ff]"
                        title={j.creator_name || j.subscription_name || j.subscription_id}
                      >
                        {j.creator_name || j.subscription_name || j.subscription_id.slice(0, 8)}
                      </Link>
                      <Link
                        href={`/admin/subscriptions/${j.subscription_id}`}
                        onClick={(e) => e.stopPropagation()}
                        className="block truncate text-[11px] text-[#57606a] hover:underline dark:text-[#8b949e]"
                        title={j.subscription_name || j.subscription_id}
                      >
                        {j.subscription_name || `${t("jobs.subscription")} ${j.subscription_id.slice(0, 8)}`}
                      </Link>
                    </div>
                    <span className="min-w-0 flex-1 truncate text-xs text-[#57606a] dark:text-[#8b949e]" title={j.source_url}>{j.source_url}</span>
                    {/* Download progress from WebSocket or polling */}
                    <PipelineVisualizer stages={downloadPipeline[j.id]?.stages ?? []} />
                    {active ? (
                      <Elapsed since={j.created_at} active={true} />
                    ) : (
                      <span className="text-xs text-gray-400 shrink-0 w-20 text-right">
                        {j.retry_count > 0 && <span className="mr-1">↻{j.retry_count}</span>}
                        {fmt.time(j.created_at)}
                      </span>
                    )}
                    <div className="flex gap-1 shrink-0" onClick={(e) => e.stopPropagation()}>
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
                  {j.error_log && expandedLog !== j.id && <ErrorExcerpt value={j.error_log} />}
                  {expandedLog === j.id && j.error_log && (
                    <pre className="mt-1 max-h-48 overflow-auto rounded-md border border-[#cf222e]/20 bg-[#ffebe9] p-3 font-mono text-xs whitespace-pre-wrap text-[#cf222e] dark:border-[#f85149]/30 dark:bg-[#f8514926] dark:text-[#f85149]">{j.error_log}</pre>
                  )}
                  {expandedImports === j.id && (
                    <ImportJobsList downloadJobId={j.id} />
                  )}
                </div>
              );
            })}
          </div>
          </div>
        )}
      </section>}

      {/* Import Jobs */}
      {activeTab === "imports" && <section className="mb-8">
        <h3 className="text-base font-semibold mb-2 flex items-center gap-3">
          {t("jobs.import")}
          <span className="text-xs font-normal text-[#57606a] dark:text-[#8b949e]">{imports.data?.total ?? 0} {t("common.items")}</span>
        </h3>
        {imports.isLoading && <div className="space-y-2">{Array.from({ length: 3 }).map((_, i) => <div key={i} className="h-12 rounded-md bg-[#eaeef2] dark:bg-[#21262d] animate-pulse" />)}</div>}
        {imports.data?.items && !imports.data?.items.length && <p className="text-sm text-[#57606a] dark:text-[#8b949e]">{t("jobs.no_im")}</p>}
        {imports.data?.items && imports.data?.items.length > 0 && (
          <div className="overflow-x-auto pb-2">
          <div className="min-w-[720px] space-y-1">
            {imports.data?.items?.map((j: any) => {
              const active = j.status === "running";
              return (
                <div key={j.id} onClick={() => openImportDetail(j.id)} className={`card flex cursor-pointer items-center gap-3 p-3 text-sm hover:border-[#0969da]/50 ${active ? "border-l-2 border-l-[#0969da]" : j.status === "failed" ? "border-l-2 border-l-[#cf222e]" : ""}`}>
                  <ActiveIndicator status={j.status} />
                  <span className="font-mono text-xs text-gray-400 dark:text-gray-500 w-16 shrink-0">{j.id.slice(0, 8)}</span>
                  <span className="font-mono text-xs text-gray-400 truncate flex-1">{j.download_job_id?.slice(0, 8) || "-"}</span>
                  <Link href={`/admin/jobs?tab=downloads&job=${j.download_job_id}`} onClick={(e) => e.stopPropagation()} className="text-xs text-[#0969da] hover:underline dark:text-[#58a6ff]">{t("jobs.open_download")}</Link>
                  <div className="flex gap-2" onClick={(e) => e.stopPropagation()}>
                  {j.error_log && (
                    <button onClick={() => setExpandedLog(expandedLog === j.id ? null : j.id)} className="text-xs text-orange-500 hover:underline">{expandedLog === j.id ? "▲" : t("downloads.log")}</button>
                  )}
                  {j.status === "failed" && <button onClick={() => { setRetryId(j.id); retryIM.mutate(j.id); }} disabled={retryIM.isPending} className="text-xs text-blue-600 hover:underline">{t("jobs.retry")}</button>}
                  <button onClick={() => { setDeleteId(j.id); setDeleteType("im"); }} className="text-xs text-red-500 hover:underline">{t("jobs.del")}</button>
                  </div>
                </div>
              );
            })}
          </div>
          </div>
        )}
      </section>}

      <JobDetailDrawer
        kind={selectedImportJobId ? "import" : "download"}
        id={selectedImportJobId || selectedDownloadJobId}
        onClose={closeDetail}
        onRetryDownload={(id) => retryDL.mutate(id)}
        onPauseDownload={(id) => pauseDL.mutate(id)}
        onResumeDownload={(id) => resumeDL.mutate(id)}
        onDeleteDownload={(id) => { setDeleteId(id); setDeleteType("dl"); }}
        onRetryImport={(id) => retryIM.mutate(id)}
        onDeleteImport={(id) => { setDeleteId(id); setDeleteType("im"); }}
      />

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
          <span className={`px-1 rounded ${imp.status === "complete" ? "bg-green-100 text-green-700" : imp.status === "failed" ? "bg-red-100 text-red-600" : imp.status === "running" ? "bg-blue-100 text-blue-600" : "bg-gray-100 text-gray-500"}`}>{statusLabel(t, imp.status)}</span>
          {imp.error_log && <span className="text-orange-500 truncate max-w-xs">{imp.error_log.slice(0, 100)}</span>}
        </div>
      ))}
    </div>
  );
}

export default function JobsPage() {
  return (
    <Suspense>
      <JobsContent />
    </Suspense>
  );
}

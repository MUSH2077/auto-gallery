"use client";
import Link from "next/link";
import { Suspense, useState, useEffect, useMemo, type ReactNode } from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useToast } from "@/components/Toast";
import { useT } from "@/lib/i18n";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api, DownloadJob, ImportJob, JobProgress, queryKeys, TaskRun } from "@/lib/api";
import { PageHeader, EmptyState, ErrorState, ConfirmDialog, SourceBadge, RealProgressBar, BatchByFilter, StatusBadge, StatCard } from "@/components";
import { useJobWebSocket } from "@/lib/useWebSocket";
import { statusLabel, useI18nFormat } from "@/lib/i18n-format";
import { classifyJob, categoryBorderClass, classifyError, estimatedRetryBackoff } from "@/lib/jobCategory";
import { POLL_ACTIVE_MS as REFETCH_ACTIVE_MS, POLL_IDLE_MS as REFETCH_IDLE_MS } from "@/lib/polling";


const PAGE_LIMIT = 200;

const STATUS_OPTIONS = ["", "enqueued", "downloading", "paused", "downloaded", "importing", "complete", "failed", "stale", "cancelled"];
const IMPORT_STATUS_OPTIONS = ["", "enqueued", "running", "complete", "failed", "stale"];
const TASK_STATUS_OPTIONS = ["", "enqueued", "running", "paused", "recovering", "complete", "failed", "cancelled", "stale"];
const SOURCE_OPTIONS = ["", "pixiv", "x", "iwara", "danbooru", "pinterest", "lofter", "weibo", "bilibili"];
type JobsTab = "all" | "downloads" | "imports" | "admin";
function isActiveDownload(status: string) {
  return ["pending", "enqueued", "downloading", "downloaded", "importing"].includes(status);
}

function isActiveImport(status: string) {
  return ["pending", "enqueued", "running"].includes(status);
}

function isActiveTask(status: string) {
  return ["enqueued", "running", "recovering"].includes(status);
}

function fallbackProgress(stage: string): JobProgress {
  return { stage };
}

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
                danger ? "bg-danger" : active ? "animate-pulse bg-accent" : done ? "bg-success" : "bg-border dark:bg-border"
              }`}
            />
            {index < steps.length - 1 && <span className={`h-px flex-1 ${done ? "bg-success" : "bg-border dark:bg-border"}`} />}
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
    <div className="mt-1 line-clamp-1 rounded-md border border-danger/20 bg-danger-subtle px-2 py-1 text-xs text-danger dark:border-danger/30 dark:bg-danger-subtle dark:text-danger">
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
    <pre className="max-h-64 overflow-auto rounded-md border border-border bg-subtle p-3 font-mono text-xs whitespace-pre-wrap dark:border-border dark:bg-canvas">
      {JSON.stringify(value, null, 2)}
    </pre>
  );
}

function DetailRow({ label, value }: { label: string; value?: ReactNode }) {
  return (
    <div className="grid grid-cols-[120px_minmax(0,1fr)] gap-3 border-b border-border py-2 text-sm last:border-b-0 dark:border-border">
      <dt className="text-xs font-medium uppercase text-muted">{label}</dt>
      <dd className="min-w-0 break-all">{value || "—"}</dd>
    </div>
  );
}

function UnifiedTaskList({
  tasks,
  isLoading,
  error,
  onRetry,
  openTaskDetail,
  openDownloadDetail,
  openImportDetail,
}: {
  tasks?: { total: number; items: TaskRun[] };
  isLoading: boolean;
  error: unknown;
  onRetry: () => void;
  openTaskDetail: (id: string) => void;
  openDownloadDetail: (id: string) => void;
  openImportDetail: (id: string) => void;
}) {
  const t = useT();
  const fmt = useI18nFormat();
  const rows = tasks?.items || [];
  const labelForKind = (task: TaskRun) => {
    if (task.operation_type === "admin-rebuild") return t("scheduler.rebuild_library", "重建索引库");
    if (task.operation_type === "admin-disk-import") return t("datamgmt.disk_import", "从盘导入");
    if (task.operation_type === "admin-clear") return t("datamgmt.clear_all", "清理数据");
    if (task.kind === "download") return t("jobs.download");
    if (task.kind === "import") return t("jobs.import");
    return task.operation_type || task.kind;
  };

  return (
    <section className="mb-8">
      <h3 className="mb-2 flex items-center gap-3 text-base font-semibold">
        {t("jobs.title")}
        <span className="text-xs font-normal text-muted">{tasks?.total ?? 0} {t("common.items")}</span>
      </h3>
      {isLoading && <div className="space-y-2">{Array.from({ length: 5 }).map((_, i) => <div key={i} className="h-14 animate-pulse rounded-md bg-subtle dark:bg-subtle" />)}</div>}
      {Boolean(error) && <ErrorState message={(error as Error).message} onRetry={onRetry} />}
      {!isLoading && !error && rows.length === 0 && <EmptyState title={t("jobs.no_dl")} description={t("jobs.no_dl_desc")} />}
      {rows.length > 0 && (
        <div className="overflow-x-auto pb-2">
          <div className="min-w-[900px] space-y-1">
            {rows.map((task) => {
              const progress = task.progress_data as JobProgress | null
                || (task.progress_stage ? { stage: task.progress_stage, current: task.progress_current || undefined, total: task.progress_total || undefined } : null);
              const subjectId = task.subject_id;
              const clickableDownload = task.subject_type === "download_job" && subjectId;
              const clickableImport = task.subject_type === "import_job" && subjectId;
              return (
                <div
                  key={task.id}
                  onClick={() => openTaskDetail(task.id)}
                  className="card flex cursor-pointer items-center gap-3 p-3 text-sm hover:border-accent/50"
                >
                  <div className="w-28 shrink-0"><StatusBadge status={task.status} /></div>
                  <span className="w-24 shrink-0 truncate text-xs font-medium">{labelForKind(task)}</span>
                  <span className="w-16 shrink-0 font-mono text-xs text-muted">{shortId(task.id)}</span>
                  {task.source && <SourceBadge source={task.source} />}
                  <div className="min-w-0 flex-1">
                    <div className="truncate text-xs font-medium" title={task.title || undefined}>{task.title || task.operation_type || task.kind}</div>
                    <div className="truncate text-[11px] text-muted" title={task.source_url || undefined}>
                      {task.source_url || task.queue_name || task.rq_job_id || task.subject_type || "-"}
                    </div>
                  </div>
                  <RealProgressBar progress={isActiveTask(task.status) ? progress : null} />
                  {task.error_log && <span className="max-w-[180px] truncate text-xs text-danger dark:text-danger">{task.error_log}</span>}
                  {task.result_data?.message && <span className="max-w-[180px] truncate text-xs text-muted">{task.result_data.message}</span>}
                  {isActiveTask(task.status) && task.created_at ? (
                    <Elapsed since={task.created_at} active />
                  ) : (
                    <span className="w-20 shrink-0 text-right text-xs text-muted">{task.created_at ? fmt.time(task.created_at) : "-"}</span>
                  )}
                  {clickableDownload && (
                    <button onClick={(e) => { e.stopPropagation(); openDownloadDetail(subjectId); }} className="shrink-0 text-xs text-accent hover:underline dark:text-accent">
                      {t("jobs.open_download")}
                    </button>
                  )}
                  {clickableImport && (
                    <button onClick={(e) => { e.stopPropagation(); openImportDetail(subjectId); }} className="shrink-0 text-xs text-accent hover:underline dark:text-accent">
                      {t("jobs.import")}
                    </button>
                  )}
                </div>
              );
            })}
          </div>
        </div>
      )}
    </section>
  );
}

function TaskDetailDrawer({
  id,
  onClose,
  onRetryTask,
  onOpenDownload,
  onOpenImport,
}: {
  id: string | null;
  onClose: () => void;
  onRetryTask: (id: string) => void;
  onOpenDownload: (id: string) => void;
  onOpenImport: (id: string) => void;
}) {
  const t = useT();
  const fmt = useI18nFormat();
  const task = useQuery({
    queryKey: queryKeys.tasks.detail(id || ""),
    queryFn: () => api.getTask(id || ""),
    enabled: !!id,
  });
  if (!id) return null;
  const item = task.data;
  const retryable = item?.operation_type === "admin-disk-import" || item?.operation_type === "admin-rebuild";
  const canRetry = retryable && ["failed", "stale", "cancelled"].includes(item?.status || "");

  return (
    <aside className="fixed inset-y-0 right-0 z-50 flex w-full max-w-2xl flex-col border-l border-border bg-white shadow-xl dark:border-border dark:bg-surface" aria-label={t("jobs.task_detail", "任务详情")}>
      <div className="flex items-center justify-between border-b border-border px-4 py-3 dark:border-border">
        <div className="min-w-0">
          <div className="text-sm font-semibold">{item?.title || t("jobs.task_detail", "任务详情")}</div>
          <div className="font-mono text-xs text-muted">{shortId(id)}</div>
        </div>
        <button onClick={onClose} className="btn-icon border-0 text-lg leading-none" aria-label={t("common.close")}>×</button>
      </div>
      <div className="min-h-0 flex-1 overflow-auto p-4">
        {task.isLoading && <div className="h-24 animate-pulse rounded-md bg-subtle dark:bg-subtle" />}
        {task.error && <ErrorState message={(task.error as Error).message} onRetry={() => task.refetch()} />}
        {item && (
          <div className="space-y-4">
            <div className="flex flex-wrap gap-2">
              {canRetry && <button onClick={() => onRetryTask(item.id)} className="btn-primary text-xs">{t("jobs.retry")}</button>}
              {item.subject_type === "download_job" && item.subject_id && (
                <button onClick={() => onOpenDownload(item.subject_id!)} className="btn-ghost text-xs">{t("jobs.open_download")}</button>
              )}
              {item.subject_type === "import_job" && item.subject_id && (
                <button onClick={() => onOpenImport(item.subject_id!)} className="btn-ghost text-xs">{t("jobs.import_detail")}</button>
              )}
            </div>
            <dl className="rounded-md border border-border px-3 dark:border-border">
              <DetailRow label={t("jobs.status")} value={<StatusBadge status={item.status} />} />
              <DetailRow label={t("jobs.kind", "类型")} value={item.kind} />
              <DetailRow label={t("jobs.operation", "操作")} value={item.operation_type || item.kind} />
              <DetailRow label={t("jobs.queue", "队列")} value={item.queue_name} />
              <DetailRow label="RQ" value={item.rq_job_id} />
              <DetailRow label={t("jobs.subject", "主体")} value={item.subject_type && item.subject_id ? `${item.subject_type}:${item.subject_id}` : undefined} />
              <DetailRow label={t("jobs.source")} value={item.source ? <span className="inline-flex items-center gap-2"><SourceBadge source={item.source} />{item.source}</span> : undefined} />
              <DetailRow label={t("jobs.source_url")} value={item.source_url} />
              <DetailRow label={t("jobs.created")} value={item.created_at ? fmt.dateTime(item.created_at) : undefined} />
              <DetailRow label={t("jobs.updated")} value={item.updated_at ? fmt.dateTime(item.updated_at) : undefined} />
              <DetailRow label={t("jobs.started", "开始")} value={item.started_at ? fmt.dateTime(item.started_at) : undefined} />
              <DetailRow label={t("jobs.finished", "结束")} value={item.finished_at ? fmt.dateTime(item.finished_at) : undefined} />
            </dl>
            {item.error_log && (
              <section>
                <h3 className="mb-2 text-sm font-semibold">{t("jobs.error_log")}</h3>
                <pre className="max-h-96 overflow-auto rounded-md border border-danger/20 bg-danger-subtle p-3 font-mono text-xs whitespace-pre-wrap text-danger dark:border-danger/30 dark:bg-danger-subtle dark:text-danger">{item.error_log}</pre>
              </section>
            )}
            <section>
              <h3 className="mb-2 text-sm font-semibold">{t("jobs.progress", "进度")}</h3>
              <JsonBlock value={item.progress_data || { stage: item.progress_stage, current: item.progress_current, total: item.progress_total }} />
            </section>
            <section>
              <h3 className="mb-2 text-sm font-semibold">{t("jobs.result", "结果")}</h3>
              <JsonBlock value={item.result_data} />
            </section>
            <section>
              <h3 className="mb-2 text-sm font-semibold">{t("jobs.meta", "元数据")}</h3>
              <JsonBlock value={item.meta} />
            </section>
            <section>
              <h3 className="mb-2 text-sm font-semibold">{t("jobs.events", "事件")}</h3>
              {item.events?.length ? (
                <div className="space-y-1">
                  {item.events.map((event) => (
                    <div key={event.id} className="rounded-md border border-border px-3 py-2 text-xs dark:border-border">
                      <div className="flex items-center justify-between gap-3">
                        <span className="font-medium">{event.event_type}</span>
                        <span className="text-muted">{event.created_at ? fmt.dateTime(event.created_at) : "—"}</span>
                      </div>
                      {(event.from_status || event.to_status || event.message) && (
                        <div className="mt-1 text-muted">{event.from_status || "—"} → {event.to_status || "—"} {event.message || ""}</div>
                      )}
                      {event.payload && <JsonBlock value={event.payload} />}
                    </div>
                  ))}
                </div>
              ) : <p className="text-xs text-muted">{t("jobs.no_events", "暂无事件")}</p>}
            </section>
          </div>
        )}
      </div>
    </aside>
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
    <aside className="fixed inset-y-0 right-0 z-40 flex w-full max-w-xl flex-col border-l border-border bg-white shadow-xl dark:border-border dark:bg-surface" aria-label={t("jobs.detail_title")}>
      <div className="flex items-center justify-between border-b border-border px-4 py-3 dark:border-border">
        <div className="min-w-0">
          <div className="text-sm font-semibold">{kind === "download" ? t("jobs.download_detail") : t("jobs.import_detail")}</div>
          <div className="font-mono text-xs text-muted">{shortId(id)}</div>
        </div>
        <button onClick={onClose} className="btn-icon border-0 text-lg leading-none" aria-label={t("common.close")}>×</button>
      </div>
      <div className="min-h-0 flex-1 overflow-auto p-4">
        {loading && <div className="h-24 animate-pulse rounded-md bg-subtle dark:bg-subtle" />}
        {error && <ErrorState message={(error as Error).message} onRetry={() => (kind === "download" ? download.refetch() : importJob.refetch())} />}

        {kind === "download" && dl && (
          <div className="space-y-4">
            <div className="flex flex-wrap gap-2">
              <button onClick={() => onRetryDownload(dl.id)} className="btn-primary text-xs">{t("jobs.retry")}</button>
              {["enqueued","downloading","downloaded","importing","failed","stale"].includes(dl.status) && <button onClick={() => onPauseDownload(dl.id)} className="btn-ghost text-xs">{t("jobs.pause")}</button>}
              {dl.status === "paused" && <button onClick={() => onResumeDownload(dl.id)} className="btn-ghost text-xs">{t("jobs.resume")}</button>}
              <button onClick={() => onDeleteDownload(dl.id)} className="btn-danger text-xs">{t("jobs.del")}</button>
            </div>
            <dl className="rounded-md border border-border px-3 dark:border-border">
              <DetailRow label={t("jobs.status")} value={statusLabel(t, dl.status)} />
              <DetailRow label={t("jobs.source")} value={<span className="inline-flex items-center gap-2"><SourceBadge source={dl.source} />{dl.source}</span>} />
              <DetailRow label={t("jobs.source_url")} value={dl.source_url} />
              <DetailRow label={t("jobs.creator")} value={dl.creator_id ? <Link href={`/admin/creators/${dl.creator_id}`} className="text-accent hover:underline dark:text-accent">{dl.creator_name || shortId(dl.creator_id)}</Link> : dl.creator_name} />
              <DetailRow label={t("jobs.subscription")} value={dl.subscription_id ? <Link href={`/admin/subscriptions/${dl.subscription_id}`} className="text-accent hover:underline dark:text-accent">{dl.subscription_name || shortId(dl.subscription_id)}</Link> : undefined} />
              <DetailRow label={t("jobs.repository")} value={dl.subscription_source_id ? <Link href={`/admin/repositories/${dl.subscription_source_id}`} className="text-accent hover:underline dark:text-accent">{shortId(dl.subscription_source_id)}</Link> : undefined} />
              <DetailRow label={t("jobs.created")} value={fmt.dateTime(dl.created_at)} />
              <DetailRow label={t("jobs.updated")} value={fmt.dateTime(dl.updated_at)} />
              {dl.last_heartbeat_at && (
                <DetailRow label={t("jobs.last_heartbeat")} value={
                  <span className="text-xs">
                    {fmt.relative(dl.last_heartbeat_at)}
                    {dl.status === "stale" && (
                      <span className="ml-2 text-orange-500 font-medium">
                        {t("jobs.stale_lost_heartbeat", { time: fmt.relative(dl.last_heartbeat_at) || "—" })}
                      </span>
                    )}
                  </span>
                } />
              )}
              {dl.retry_count > 0 && (
                <DetailRow label={t("jobs.recovery_retry", { current: String(dl.retry_count), max: "3" })} value={
                  <span className="text-xs text-blue-600 dark:text-blue-400 font-medium">{dl.retry_count} / 3</span>
                } />
              )}
            </dl>
            {dl.error_log && (() => {
              const errInfo = classifyError(dl.error_log);
              const hintKey = `jobs.error_type_${errInfo.type}`;
              return (
              <section>
                <h3 className="mb-2 text-sm font-semibold flex items-center gap-2">
                  {t("jobs.error_log")}
                  {errInfo.type !== "unknown" && (
                    <span className={`text-xs px-2 py-0.5 rounded-full ${
                      errInfo.type === "auth" ? "bg-red-100 text-red-700 dark:bg-red-900/30 dark:text-red-400" :
                      errInfo.type === "timeout" || errInfo.type === "stall" ? "bg-yellow-100 text-yellow-700 dark:bg-yellow-900/30 dark:text-yellow-400" :
                      "bg-subtle text-muted dark:bg-subtle dark:text-muted"
                    }`}>{t(hintKey)}</span>
                  )}
                </h3>
                {errInfo.type === "auth" && (
                  <p className="text-xs text-red-600 dark:text-red-400 mb-2">
                    {t("auth.desc")}: <Link href="/admin/settings/auth-status" className="underline">{t("settings.auth")} →</Link>
                  </p>
                )}
                {errInfo.type === "timeout" && (
                  <p className="text-xs text-yellow-600 dark:text-yellow-400 mb-2">
                    {t("dldefaults.timeout.desc")}: <Link href="/admin/settings/download-defaults" className="underline">{t("dldefaults.title")} →</Link>
                  </p>
                )}
                {errInfo.type === "stall" && (
                  <p className="text-xs text-yellow-600 dark:text-yellow-400 mb-2">
                    {t("dldefaults.stall_timeout.desc")}: <Link href="/admin/settings/download-defaults" className="underline">{t("dldefaults.title")} →</Link>
                  </p>
                )}
                <pre className="max-h-64 overflow-auto rounded-md border border-danger/20 bg-danger-subtle p-3 font-mono text-xs whitespace-pre-wrap text-danger dark:border-danger/30 dark:bg-danger-subtle dark:text-danger">{dl.error_log}</pre>
              </section>
              );
            })()}
            {dl.manifest && (
              <section>
                <h3 className="mb-2 text-sm font-semibold">{t("jobs.manifest")}</h3>
                <JsonBlock value={dl.manifest} />
              </section>
            )}
            <section>
              <h3 className="mb-2 text-sm font-semibold">{t("jobs.related_imports")}</h3>
              {imports.isLoading && <div className="h-12 animate-pulse rounded bg-subtle dark:bg-subtle" />}
              {imports.data?.length ? (
                <div className="space-y-1">
                  {imports.data.map((job: ImportJob) => (
                    <Link key={job.id} href={`/admin/jobs?tab=imports&download_job_id=${dl.id}&import_job=${job.id}`} className="flex items-center justify-between rounded-md border border-border px-3 py-2 text-sm hover:bg-subtle dark:border-border dark:hover:bg-subtle">
                      <span className="font-mono text-xs">{shortId(job.id)}</span>
                      <span>{statusLabel(t, job.status)}</span>
                    </Link>
                  ))}
                </div>
              ) : <p className="text-xs text-muted">{t("jobs.no_imports_yet")}</p>}
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
            <dl className="rounded-md border border-border px-3 dark:border-border">
              <DetailRow label={t("jobs.status")} value={statusLabel(t, im.status)} />
              <DetailRow label={t("jobs.download_job")} value={<Link href={`/admin/jobs?tab=downloads&job=${im.download_job_id}`} className="text-accent hover:underline dark:text-accent">{shortId(im.download_job_id)}</Link>} />
              <DetailRow label={t("jobs.created")} value={fmt.dateTime(im.created_at)} />
              <DetailRow label={t("jobs.updated")} value={fmt.dateTime(im.updated_at)} />
            </dl>
            {im.error_log && (
              <section>
                <h3 className="mb-2 text-sm font-semibold">{t("jobs.error_log")}</h3>
                <pre className="max-h-80 overflow-auto rounded-md border border-danger/20 bg-danger-subtle p-3 font-mono text-xs whitespace-pre-wrap text-danger dark:border-danger/30 dark:bg-danger-subtle dark:text-danger">{im.error_log}</pre>
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
  const [downloadProgress, setDownloadProgress] = useState<Record<string, JobProgress>>({});
  const [importProgress, setImportProgress] = useState<Record<string, JobProgress>>({});

  // WebSocket: invalidate queries on status change, update progress on progress events
  const { connected, status: wsStatus } = useJobWebSocket({
    onStatusChange: (msg) => {
      qc.invalidateQueries({ queryKey: queryKeys.downloadJobs.all });
      qc.invalidateQueries({ queryKey: queryKeys.importJobs.all });
      qc.invalidateQueries({ queryKey: queryKeys.tasks.all });
      qc.invalidateQueries({ queryKey: ["workbench"] });
      if (msg.task_id && msg.new_status) {
        toast.info(`${msg.task_id.slice(0, 8)}: ${msg.old_status} → ${msg.new_status}`);
      }
    },
    onProgress: (msg) => {
      const taskId = msg.task_id;
      if (taskId && msg.task_type === "download" && msg.progress) {
        setDownloadProgress(prev => ({ ...prev, [taskId]: msg.progress as JobProgress }));
      }
      if (taskId && msg.task_type === "import" && msg.progress) {
        setImportProgress(prev => ({ ...prev, [taskId]: msg.progress as JobProgress }));
      }
    },
  });
  const router = useRouter();
  const pathname = usePathname();
  const sp = useSearchParams();

  const tabParam = sp.get("tab");
  const activeTab = (tabParam === "downloads" || tabParam === "imports" || tabParam === "admin" ? tabParam : "all") as JobsTab;
  const status = sp.get("status") || "";
  const dlSource = sp.get("source") || "";
  const subscriptionSourceId = sp.get("subscription_source_id") || "";
  const downloadJobId = sp.get("download_job_id") || "";
  const search = sp.get("q") || "";
  const dlSort = sp.get("sort") || "created_at";
  const dlOrder = sp.get("order") || "desc";
  const selectedDownloadJobId = sp.get("job");
  const selectedImportJobId = sp.get("import_job");
  const selectedTaskId = sp.get("task");

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

  const openTaskDetail = (id: string) => updateParams({ task: id, job: null, import_job: null }, false);
  const openDownloadDetail = (id: string) => updateParams({ tab: "downloads", job: id, import_job: null, task: null }, false);
  const openImportDetail = (id: string) => updateParams({ tab: "imports", import_job: id, job: null, task: null }, false);
  const closeDetail = () => updateParams({ job: null, import_job: null, task: null });

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
    enabled: activeTab === "downloads",
    refetchInterval: (!status || isActiveDownload(status)) ? REFETCH_ACTIVE_MS : REFETCH_IDLE_MS,
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
    enabled: activeTab === "imports",
    refetchInterval: (!status || isActiveImport(status)) ? REFETCH_ACTIVE_MS : REFETCH_IDLE_MS,
  });

  const taskParams = useMemo(() => ({
    kind: activeTab === "admin" ? "admin" : undefined,
    status: status || undefined,
    source: activeTab === "admin" ? undefined : dlSource || undefined,
    q: search || undefined,
    offset: 0,
    limit: PAGE_LIMIT,
  }), [activeTab, status, dlSource, search]);

  const tasks = useQuery({
    queryKey: [...queryKeys.tasks.all, taskParams],
    queryFn: () => api.listTasks(taskParams),
    enabled: activeTab === "all" || activeTab === "admin",
    refetchInterval: (!status || isActiveTask(status)) ? REFETCH_ACTIVE_MS : REFETCH_IDLE_MS,
  });

  useEffect(() => {
    const rows = downloads.data ?? [];
    setDownloadProgress((prev) => {
      const next = { ...prev };
      const visibleIds = new Set(rows.map((job) => job.id));
      for (const id of Object.keys(next)) {
        if (!visibleIds.has(id)) delete next[id];
      }
      for (const job of rows) {
        if (!isActiveDownload(job.status)) {
          delete next[job.id];
        } else if (job.progress_data) {
          next[job.id] = job.progress_data;
        } else if (job.pipeline_stage && !next[job.id]) {
          next[job.id] = fallbackProgress(job.pipeline_stage);
        }
      }
      return next;
    });
  }, [downloads.data]);

  useEffect(() => {
    const rows = imports.data?.items ?? [];
    setImportProgress((prev) => {
      const next = { ...prev };
      const visibleIds = new Set(rows.map((job) => job.id));
      for (const id of Object.keys(next)) {
        if (!visibleIds.has(id)) delete next[id];
      }
      for (const job of rows) {
        if (!isActiveImport(job.status)) {
          delete next[job.id];
        } else if (job.progress_data) {
          next[job.id] = job.progress_data;
        } else if (job.progress_stage && !next[job.id]) {
          next[job.id] = fallbackProgress(job.progress_stage);
        }
      }
      return next;
    });
  }, [imports.data]);

  const activeFilterCount = [
    status,
    activeTab === "admin" ? "" : dlSource,
    subscriptionSourceId,
    downloadJobId,
    search,
    activeTab === "downloads" && (dlSort !== "created_at" || dlOrder !== "desc"),
  ].filter(Boolean).length;
  const lastUpdated = Math.max(downloads.dataUpdatedAt || 0, imports.dataUpdatedAt || 0, tasks.dataUpdatedAt || 0, workbench.dataUpdatedAt || 0);
  const refreshAll = () => {
    qc.invalidateQueries({ queryKey: queryKeys.downloadJobs.all });
    qc.invalidateQueries({ queryKey: queryKeys.importJobs.all });
    qc.invalidateQueries({ queryKey: queryKeys.tasks.all });
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
  const retryTask = useMutation({
    mutationFn: (id: string) => api.retryTask(id),
    onSuccess: (_data, id) => {
      qc.invalidateQueries({ queryKey: queryKeys.tasks.all });
      qc.invalidateQueries({ queryKey: queryKeys.tasks.detail(id) });
      qc.invalidateQueries({ queryKey: queryKeys.workbench });
      toast.info(t("jobs.retry_queued", "任务已重新入队"));
    },
    onError: (err) => toast.error((err as Error).message),
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
        return old.map((j: any) => j.id === id ? { ...j, status: "enqueued" } : j);
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

  const statusOptions = activeTab === "imports"
    ? IMPORT_STATUS_OPTIONS
    : activeTab === "downloads"
      ? STATUS_OPTIONS
      : TASK_STATUS_OPTIONS;

  return (
    <main className="max-w-7xl mx-auto p-6">
      <PageHeader title={t("jobs.title")} description={t("jobs.desc")}>
        <div className="flex flex-wrap gap-2">
          <button onClick={refreshAll} className="btn-ghost text-xs">{t("jobs.refresh")}</button>
          <BatchByFilter onSuccess={() => { qc.invalidateQueries({ queryKey: queryKeys.downloadJobs.all }); }} />
          <button onClick={() => handleClear(["failed", "stale"])} className="btn-danger text-xs">{t("jobs.clear_failed")}</button>
          <button onClick={() => handleClear(["complete"])} className="btn-ghost text-xs">{t("jobs.clear_complete")}</button>
          <button onClick={() => killStuck.mutate()} disabled={killStuck.isPending} className="btn-ghost text-xs">{t("jobs.kill_stuck")}</button>
          <button onClick={() => retryAllFailed.mutate()} disabled={retryAllFailed.isPending} className="btn-primary text-xs">{t("jobs.retry_all_failed")}</button>
          <span
            className={`inline-flex items-center gap-1 rounded-full border px-2 py-1 text-xs ${
              connected
                ? "border-success/30 bg-success-subtle text-success dark:border-success/30 dark:bg-success-subtle dark:text-success"
                : "border-border bg-subtle text-muted dark:border-border dark:bg-subtle dark:text-muted"
            }`}
            title={connected ? t("jobs.live_connected") : t("jobs.live_polling_title")}
          >
            <span className={`h-1.5 w-1.5 rounded-full ${connected ? "bg-current" : "animate-pulse bg-current"}`} />
            {connected ? t("jobs.live_connected") : wsStatus === "connecting" ? t("jobs.live_connecting") : t("jobs.live_polling")}
          </span>
        </div>
      </PageHeader>

      {workbench.data && (
        <div className="mb-4 grid grid-cols-2 gap-3 md:grid-cols-5">
          <StatCard label={t("jobs.summary_active")} value={workbench.data.queue.active_download_count + workbench.data.queue.active_import_count} sub={t("jobs.summary_active_sub")} tone="active" />
          <StatCard label={t("jobs.summary_queued")} value={workbench.data.queue.default} sub={t("jobs.summary_queued_sub")} />
          <StatCard label={t("jobs.summary_importing")} value={workbench.data.queue.active_import_count} sub={t("jobs.summary_importing_sub")} tone={workbench.data.queue.active_import_count ? "active" : "neutral"} />
          <StatCard label={t("jobs.summary_failed")} value={workbench.data.queue.failed_download_count + workbench.data.queue.failed_import_count} tone={workbench.data.queue.failed_download_count + workbench.data.queue.failed_import_count ? "danger" : "neutral"} />
          <StatCard label={t("jobs.summary_stale")} value={workbench.data.queue.stale_count} sub={t("jobs.summary_stale_sub")} tone={workbench.data.queue.stale_count ? "warning" : "neutral"} />
        </div>
      )}

      <div className="mb-4 flex flex-col gap-3 rounded-md border border-border bg-white p-3 dark:border-border dark:bg-surface">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="flex gap-1 rounded-md bg-subtle p-1 dark:bg-subtle">
            <button onClick={() => updateParams({ tab: null, status: null, job: null, import_job: null, task: null })} className={`rounded px-3 py-1.5 text-xs font-medium ${activeTab === "all" ? "bg-white shadow-sm dark:bg-border" : "text-muted"}`}>{t("common.all")}</button>
            <button onClick={() => updateParams({ tab: "downloads", status: null, import_job: null, task: null })} className={`rounded px-3 py-1.5 text-xs font-medium ${activeTab === "downloads" ? "bg-white shadow-sm dark:bg-border" : "text-muted"}`}>{t("jobs.download")}</button>
            <button onClick={() => updateParams({ tab: "imports", status: null, job: null, task: null })} className={`rounded px-3 py-1.5 text-xs font-medium ${activeTab === "imports" ? "bg-white shadow-sm dark:bg-border" : "text-muted"}`}>{t("jobs.import")}</button>
            <button onClick={() => updateParams({ tab: "admin", status: null, job: null, import_job: null, task: null, source: null })} className={`rounded px-3 py-1.5 text-xs font-medium ${activeTab === "admin" ? "bg-white shadow-sm dark:bg-border" : "text-muted"}`}>{t("scheduler.admin_operations", "管理操作")}</button>
          </div>
          <div className="flex flex-wrap items-center gap-2 text-xs text-muted">
            {activeFilterCount > 0 && <span className="rounded-full bg-accent-subtle px-2 py-0.5 font-medium text-accent dark:bg-accent-subtle dark:text-accent">{t("jobs.active_filters", { count: activeFilterCount })}</span>}
            <span>{t("jobs.last_refreshed", { time: lastUpdated ? fmt.time(new Date(lastUpdated).toISOString()) : "—" })}</span>
            {activeFilterCount > 0 && <button onClick={clearFilters} className="text-accent hover:underline dark:text-accent">{t("jobs.clear_filters")}</button>}
          </div>
        </div>

      {/* Filters */}
        <div className="flex flex-wrap items-center gap-2">
          <input value={search} onChange={(e) => updateParams({ q: e.target.value || null })} className="input min-w-[220px] px-3 py-1.5 text-xs" placeholder={t("jobs.search_placeholder")} aria-label={t("jobs.search_placeholder")} />
        <select value={status} onChange={(e) => updateParams({ status: e.target.value || null })} className="select px-2 py-1.5 text-xs">
          <option value="">{t("jobs.filter_all_status")}</option>
          {statusOptions.filter(Boolean).map(s => <option key={s} value={s}>{statusLabel(t, s)}</option>)}
        </select>
        {activeTab !== "imports" && activeTab !== "admin" && <select value={dlSource} onChange={(e) => updateParams({ source: e.target.value || null })} className="select px-2 py-1.5 text-xs">
          <option value="">{t("jobs.filter_all_source")}</option>
          {SOURCE_OPTIONS.filter(Boolean).map(s => <option key={s} value={s}>{s}</option>)}
        </select>}
        {subscriptionSourceId && <span className="rounded-md border border-border px-2 py-1 text-xs font-mono dark:border-border">{t("jobs.repository")} {shortId(subscriptionSourceId)}</span>}
        {downloadJobId && <span className="rounded-md border border-border px-2 py-1 text-xs font-mono dark:border-border">{t("jobs.download_job")} {shortId(downloadJobId)}</span>}
        {activeTab === "downloads" && <select value={`${dlSort}-${dlOrder}`} onChange={(e) => { const [k, o] = e.target.value.split("-"); updateParams({ sort: k === "created_at" && o === "desc" ? null : k, order: o === "desc" ? null : o }); }} className="select px-2 py-1.5 text-xs">
          <option value="created_at-desc">{t("jobs.sort_newest")}</option>
          <option value="created_at-asc">{t("jobs.sort_oldest")}</option>
          <option value="status-asc">{t("jobs.sort_status")}</option>
          <option value="source-asc">{t("jobs.sort_source")}</option>
        </select>}
        {activeTab === "downloads" && <button onClick={handleSelectAll} className="btn-ghost text-xs">{selectAll ? t("common.deselect_all") : t("common.select_all")}</button>}

        {selected.size > 0 && (
          <div className="ml-auto flex items-center gap-1 rounded-md border border-warning/30 bg-warning-subtle px-3 py-1.5 dark:bg-warning-subtle">
            <span className="text-xs text-warning dark:text-warning">{selected.size} {t("common.selected")}</span>
            <button onClick={() => handleBatch("pause")} className="px-2 py-0.5 text-xs bg-yellow-500 text-white rounded hover:bg-yellow-600">{t("jobs.batch_pause")}</button>
            <button onClick={() => handleBatch("resume")} className="px-2 py-0.5 text-xs bg-green-500 text-white rounded hover:bg-green-600">{t("jobs.batch_resume")}</button>
            <button onClick={() => handleBatch("retry")} className="px-2 py-0.5 text-xs bg-blue-500 text-white rounded hover:bg-blue-600">{t("jobs.batch_retry")}</button>
            <button onClick={() => handleBatch("cancel")} className="px-2 py-0.5 text-xs bg-subtle text-white rounded hover:bg-subtle">{t("jobs.batch_cancel")}</button>
            <button onClick={() => handleBatch("delete")} className="px-2 py-0.5 text-xs bg-red-500 text-white rounded hover:bg-red-600">{t("jobs.batch_delete")}</button>
          </div>
        )}
        </div>
      </div>

      {(activeTab === "all" || activeTab === "admin") && (
        <UnifiedTaskList
          tasks={tasks.data}
          isLoading={tasks.isLoading}
          error={tasks.error}
          onRetry={() => tasks.refetch()}
          openTaskDetail={openTaskDetail}
          openDownloadDetail={openDownloadDetail}
          openImportDetail={openImportDetail}
        />
      )}

      {/* Download Jobs list */}
      {activeTab === "downloads" && <section className="mb-8">
        {downloads.isLoading && <div className="space-y-2">{Array.from({ length: 5 }).map((_, i) => <div key={i} className="h-14 rounded-md bg-subtle dark:bg-subtle animate-pulse" />)}</div>}
        {downloads.error && <ErrorState message={(downloads.error as Error).message} onRetry={() => downloads.refetch()} />}
        {downloads.data && !downloads.data?.length && <EmptyState title={t("jobs.no_dl")} description={t("jobs.no_dl_desc")} />}
        {downloads.data && downloads.data?.length > 0 && (
          <div className="overflow-x-auto pb-2">
          <div className="min-w-[980px] space-y-1">
            {downloads.data.map((j: any) => {
              const active = isActiveDownload(j.status);
              const progress = active
                ? downloadProgress[j.id] || (j.progress_data as JobProgress | null) || fallbackProgress(j.pipeline_stage || j.status)
                : null;
              return (
                <div key={j.id}>
                  <div onClick={() => openDownloadDetail(j.id)} className={`card flex cursor-pointer items-center gap-3 p-3 text-sm hover:border-accent/50 ${(() => { const cat = classifyJob(j.status, j.retry_count, 3); const cls = categoryBorderClass(cat); return cls ? `border-l-2 ${cls}` : ""; })()}`}>
                    <input type="checkbox" checked={selected.has(j.id)} onClick={(e) => e.stopPropagation()} onChange={() => toggleSelect(j.id)} className="w-4 h-4 rounded border-border shrink-0" />
                    <div className="w-28 shrink-0"><StatusBadge status={j.status} /></div>
                    {j.operation_type && j.operation_type !== "download" && (
                      <span className="text-[10px] px-1.5 py-0.5 rounded-full bg-purple-100 text-purple-700 dark:bg-purple-900/30 dark:text-purple-400 shrink-0">
                        {t(`jobs.op_${j.operation_type}`, j.operation_type)}
                      </span>
                    )}
                    <span className="w-16 shrink-0 font-mono text-xs text-muted">{j.id.slice(0, 8)}</span>
                    {j.source && <SourceBadge source={j.source} />}
                    <div className="min-w-[9rem] max-w-[12rem] shrink-0 leading-tight">
                      <Link
                        href={`/admin/subscriptions/${j.subscription_id}`}
                        onClick={(e) => e.stopPropagation()}
                        className="block truncate text-xs font-medium text-accent hover:underline dark:text-accent"
                        title={j.creator_name || j.subscription_name || j.subscription_id}
                      >
                        {j.creator_name || j.subscription_name || j.subscription_id.slice(0, 8)}
                      </Link>
                      <Link
                        href={`/admin/subscriptions/${j.subscription_id}`}
                        onClick={(e) => e.stopPropagation()}
                        className="block truncate text-[11px] text-muted hover:underline dark:text-muted"
                        title={j.subscription_name || j.subscription_id}
                      >
                        {j.subscription_name || `${t("jobs.subscription")} ${j.subscription_id.slice(0, 8)}`}
                      </Link>
                    </div>
                    <span className="min-w-0 flex-1 truncate text-xs text-muted" title={j.source_url}>{j.source_url}</span>
                    <RealProgressBar progress={progress} />
                    {active ? (
                      <Elapsed since={j.created_at} active={true} />
                    ) : classifyJob(j.status, j.retry_count, 3) === "retrying" ? (
                      <span className="text-xs text-blue-500 shrink-0 w-22 text-right font-medium">
                        ↻ {t("jobs.recovery_retry", { current: String(j.retry_count), max: "3" })}
                        {estimatedRetryBackoff(j.retry_count, 60) != null && (
                          <span className="block text-[10px] text-muted">
                            {t("jobs.recovery_waiting", { seconds: String(estimatedRetryBackoff(j.retry_count, 60)) })}
                          </span>
                        )}
                      </span>
                    ) : (
                      <span className="text-xs text-muted shrink-0 w-20 text-right">
                        {j.retry_count > 0 && <span className="mr-1">↻{j.retry_count}</span>}
                        {fmt.time(j.created_at)}
                      </span>
                    )}
                    <div className="flex gap-1 shrink-0" onClick={(e) => e.stopPropagation()}>
                      {j.error_log && (
                        <button onClick={() => setExpandedLog(expandedLog === j.id ? null : j.id)} className="text-xs text-orange-500 hover:underline">{expandedLog === j.id ? "▲" : t("downloads.log")}</button>
                      )}
                      <button onClick={() => setExpandedImports(expandedImports === j.id ? null : j.id)} className="text-xs text-purple-500 hover:underline">{t("jobs.imports")}</button>
                      {["enqueued","downloading","downloaded","importing","failed","stale"].includes(j.status) && (
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
                    <pre className="mt-1 max-h-48 overflow-auto rounded-md border border-danger/20 bg-danger-subtle p-3 font-mono text-xs whitespace-pre-wrap text-danger dark:border-danger/30 dark:bg-danger-subtle dark:text-danger">{j.error_log}</pre>
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
          <span className="text-xs font-normal text-muted">{imports.data?.total ?? 0} {t("common.items")}</span>
        </h3>
        {imports.isLoading && <div className="space-y-2">{Array.from({ length: 3 }).map((_, i) => <div key={i} className="h-12 rounded-md bg-subtle dark:bg-subtle animate-pulse" />)}</div>}
        {imports.data?.items && !imports.data?.items.length && <p className="text-sm text-muted">{t("jobs.no_im")}</p>}
        {imports.data?.items && imports.data?.items.length > 0 && (
          <div className="overflow-x-auto pb-2">
          <div className="min-w-[980px] space-y-1">
            {imports.data?.items?.map((j: any) => {
              const active = isActiveImport(j.status);
              const progress = active
                ? importProgress[j.id] || (j.progress_data as JobProgress | null) || fallbackProgress(j.progress_stage || j.status)
                : null;
              const worksDone = j.progress_works_done;
              const worksTotal = j.progress_works_total;
              return (
                <div key={j.id}>
                  <div onClick={() => openImportDetail(j.id)} className={`card flex cursor-pointer items-center gap-3 p-3 text-sm hover:border-accent/50 ${active ? "border-l-2 border-l-accent" : j.status === "failed" ? "border-l-2 border-l-danger" : ""}`}>
                    <div className="w-28 shrink-0"><StatusBadge status={j.status} /></div>
                    <span className="w-16 shrink-0 font-mono text-xs text-muted">{shortId(j.id)}</span>
                    {j.source && <SourceBadge source={j.source} />}
                    <div className="min-w-[9rem] max-w-[12rem] shrink-0 leading-tight">
                      {j.subscription_id ? (
                        <Link href={`/admin/subscriptions/${j.subscription_id}`} onClick={(e) => e.stopPropagation()} className="block truncate text-xs font-medium text-accent hover:underline dark:text-accent" title={j.creator_name || j.subscription_name || undefined}>
                          {j.creator_name || j.subscription_name || shortId(j.subscription_id)}
                        </Link>
                      ) : (
                        <span className="block truncate text-xs text-muted">—</span>
                      )}
                      <span className="block truncate text-[11px] text-muted" title={j.subscription_name || undefined}>
                        {j.subscription_name || `${t("jobs.import")} · ${shortId(j.download_job_id)}`}
                      </span>
                    </div>
                    <span className="min-w-0 flex-1 truncate text-xs text-muted" title={j.source_url || undefined}>{j.source_url || "-"}</span>
                    {(worksTotal != null || worksDone != null) && (
                      <span className="w-14 shrink-0 text-right font-mono text-[11px] text-muted tabular-nums" title={t("jobs.works", "作品")}>{worksDone ?? 0}/{worksTotal ?? "?"}</span>
                    )}
                    <RealProgressBar progress={progress} />
                    {active ? (
                      <Elapsed since={j.created_at} active />
                    ) : (
                      <span className="w-20 shrink-0 text-right text-xs text-muted">{fmt.time(j.created_at)}</span>
                    )}
                    <div className="flex shrink-0 gap-2" onClick={(e) => e.stopPropagation()}>
                      <Link href={`/admin/jobs?tab=downloads&job=${j.download_job_id}`} className="text-xs text-accent hover:underline dark:text-accent">{t("jobs.open_download")}</Link>
                      {j.error_log && (
                        <button onClick={() => setExpandedLog(expandedLog === j.id ? null : j.id)} className="text-xs text-orange-500 hover:underline">{expandedLog === j.id ? "▲" : t("downloads.log")}</button>
                      )}
                      {(j.status === "failed" || j.status === "stale") && <button onClick={() => { setRetryId(j.id); retryIM.mutate(j.id); }} disabled={retryIM.isPending} className="text-xs text-blue-600 hover:underline">{t("jobs.retry")}</button>}
                      <button onClick={() => { setDeleteId(j.id); setDeleteType("im"); }} className="text-xs text-red-500 hover:underline">{t("jobs.del")}</button>
                    </div>
                  </div>
                  {j.error_log && expandedLog !== j.id && <ErrorExcerpt value={j.error_log} />}
                  {expandedLog === j.id && j.error_log && (
                    <pre className="mt-1 max-h-48 overflow-auto rounded-md border border-danger/20 bg-danger-subtle p-3 font-mono text-xs whitespace-pre-wrap text-danger dark:border-danger/30 dark:bg-danger-subtle dark:text-danger">{j.error_log}</pre>
                  )}
                </div>
              );
            })}
          </div>
          </div>
        )}
      </section>}

      <TaskDetailDrawer
        id={selectedTaskId}
        onClose={closeDetail}
        onRetryTask={(id) => retryTask.mutate(id)}
        onOpenDownload={openDownloadDetail}
        onOpenImport={openImportDetail}
      />

      <JobDetailDrawer
        kind={selectedImportJobId ? "import" : "download"}
        id={selectedTaskId ? null : selectedImportJobId || selectedDownloadJobId}
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
  if (imports.isLoading) return <div className="ml-8 mt-1 text-xs text-muted">{t("common.loading")}...</div>;
  if (!imports.data?.length) return <div className="ml-8 mt-1 text-xs text-muted">{t("jobs.no_imports_yet")}</div>;
  return (
    <div className="ml-8 mt-1 space-y-0.5">
      {imports.data?.map((imp: any) => (
        <div key={imp.id} className="flex items-center gap-2 text-xs bg-subtle rounded px-2 py-1">
          <span className="font-mono text-muted">{imp.id.slice(0, 8)}</span>
          <StatusBadge status={imp.status} className="px-2 py-0 text-[10px]" />
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

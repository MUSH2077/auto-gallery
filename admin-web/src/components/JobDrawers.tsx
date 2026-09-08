"use client";
import Link from "next/link";
import { useRef, useState, type ReactNode } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { api, DownloadJob, ImportJob, queryKeys } from "@/lib/api";
import { ConfirmDialog, DownloadConflictDialog, ErrorState, StatusBadge, SourceBadge, SyncOutcomeNotice } from "@/components";
import { useT, type TFunction } from "@/lib/i18n";
import { usePresence, motionTokens } from "@/lib/motion";
import { statusLabel, useI18nFormat } from "@/lib/i18n-format";
import { classifyError } from "@/lib/jobCategory";
import { adminRoutes } from "@/lib/adminRoutes";
import { parseSyncOutcome } from "@/lib/syncOutcome";
import { hasTaskAction } from "@/lib/task-actions";
import { POLL_ACTIVE_MS } from "@/lib/polling";
import { secureRandomUuid } from "@/lib/random";

export function shortId(id?: string | null) {
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

function resourceReasonLabel(t: TFunction, reason?: string | null) {
  if (!reason) return null;
  const [code, detail] = reason.split(":", 2);
  const key = `jobs.resource.reason.${code}`;
  const translated = t(key);
  if (translated === key) return reason;
  return detail ? `${translated} (${detail})` : translated;
}

export function TaskDetailDrawer({
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
  const { mounted, closing } = usePresence(!!id, motionTokens.duration.base);
  const [showConflict, setShowConflict] = useState(false);
  const [confirmAction, setConfirmAction] = useState<"repeat_sync" | "delete" | null>(null);
  // Hold the last id through the slide-out so content doesn't blank mid-exit.
  const lastId = useRef<string | null>(null);
  if (id) lastId.current = id;
  const heldId = id ?? lastId.current;
  const task = useQuery({
    queryKey: queryKeys.tasks.detail(heldId || ""),
    queryFn: () => api.getTask(heldId || ""),
    enabled: !!id,
    refetchInterval: (query) => {
      const current = query.state.data;
      if (current?.operation_type !== "subscription-sync-batch") return false;
      const cleanupPending = current.status === "cancelled"
        && (current.result_data as { cleanup_pending?: boolean } | null)?.cleanup_pending !== false;
      return cleanupPending || !["complete", "failed", "stale", "cancelled"].includes(current.status)
        ? POLL_ACTIVE_MS
        : false;
    },
    refetchIntervalInBackground: true,
  });
  const taskAction = useMutation({
    mutationFn: async (action: "pause" | "resume" | "cancel" | "delete" | "acknowledge" | "repeat_sync") => {
      if (!heldId) throw new Error("Missing task");
      if (action === "pause") return api.pauseTask(heldId);
      if (action === "resume") return api.resumeTask(heldId);
      if (action === "cancel") return api.cancelTask(heldId);
      if (action === "delete") return api.deleteTask(heldId);
      if (action === "acknowledge") return api.acknowledgeTask(heldId);
      const key = `auto-gallery-repeat-sync:task:${heldId}`;
      let requestId = localStorage.getItem(key);
      if (!requestId) { requestId = secureRandomUuid(); localStorage.setItem(key, requestId); }
      const accepted = await api.repeatTask(heldId, requestId);
      if (accepted.request_id !== requestId || accepted.action !== "repeat_sync" || !accepted.task_id || !accepted.job_id || (task.data?.subject_id && accepted.previous_job_id !== task.data.subject_id)) throw new Error(t("jobs.repeat_identity_invalid"));
      localStorage.removeItem(key);
      return accepted;
    },
    onSuccess: () => { setConfirmAction(null); void task.refetch(); },
  });
  if (!mounted || !heldId) return null;
  const item = task.data;
  const outcome = parseSyncOutcome(item?.result_data);
  const removedCount = item?.result_data && typeof item.result_data === "object"
    && typeof (item.result_data as { removed?: unknown }).removed === "number"
    ? (item.result_data as { removed: number }).removed
    : null;
  const canRetry = hasTaskAction(item, "retry");
  const existingResolution = item?.meta?.staging_conflict_resolution as { resolution_id: string; expires_at?: string } | undefined;
  const hasConflictWorkflow = item?.reason_code === "download_staging_conflict" || !!existingResolution;

  return (
    <>
    <div className={`fixed inset-0 z-[49] bg-black/30 ${closing ? "overlay-backdrop-exit" : "overlay-backdrop"}`} onClick={onClose} aria-hidden />
    <aside className={`fixed inset-y-0 right-0 z-50 flex w-full max-w-2xl flex-col border-l border-border bg-white shadow-xl dark:border-border dark:bg-surface ${closing ? "drawer-panel-exit" : "drawer-panel"}`} aria-label={t("jobs.task_detail")}>
      <div className="flex items-center justify-between border-b border-border px-4 py-3 dark:border-border">
        <div className="min-w-0">
          <div className="text-sm font-semibold">{item?.title || t("jobs.task_detail")}</div>
          <div className="font-mono text-xs text-muted">{shortId(heldId)}</div>
        </div>
        <button onClick={onClose} className="btn-icon border-0 text-lg leading-none" aria-label={t("common.close")}>×</button>
      </div>
      <div className="min-h-0 flex-1 overflow-auto p-4">
        {task.isLoading && <div className="h-24 animate-pulse rounded-md bg-subtle dark:bg-subtle" />}
        {task.error && <ErrorState message={(task.error as Error).message} onRetry={() => task.refetch()} />}
        {item && (
          <div className="space-y-4">
            <div className="flex flex-wrap gap-2">
              {hasConflictWorkflow && <button onClick={() => setShowConflict(true)} className="btn-primary text-xs">{existingResolution ? t("jobs.conflict.view_resolution") : t("jobs.conflict.compare")}</button>}
              {canRetry && <button onClick={() => onRetryTask(item.id)} className="btn-primary text-xs">{t("jobs.retry")}</button>}
              {hasTaskAction(item, "pause") && <button onClick={() => taskAction.mutate("pause")} className="btn-ghost text-xs">{t("jobs.pause")}</button>}
              {hasTaskAction(item, "resume") && <button onClick={() => taskAction.mutate("resume")} className="btn-ghost text-xs">{t("jobs.resume")}</button>}
              {hasTaskAction(item, "cancel") && <button onClick={() => taskAction.mutate("cancel")} className="btn-ghost text-xs">{t("common.cancel")}</button>}
              {hasTaskAction(item, "acknowledge") && <button onClick={() => taskAction.mutate("acknowledge")} className="btn-ghost text-xs">{t("operations.acknowledge")}</button>}
              {hasTaskAction(item, "repeat_sync") && <button onClick={() => setConfirmAction("repeat_sync")} className="btn-primary text-xs">{t("jobs.repeat_sync")}</button>}
              {hasTaskAction(item, "delete") && <button onClick={() => setConfirmAction("delete")} className="btn-danger text-xs">{t("jobs.del")}</button>}
              {item.subject_type === "download_job" && item.subject_id && (
                <button onClick={() => onOpenDownload(item.subject_id!)} className="btn-ghost text-xs">{t("jobs.open_download")}</button>
              )}
              {item.subject_type === "import_job" && item.subject_id && (
                <button onClick={() => onOpenImport(item.subject_id!)} className="btn-ghost text-xs">{t("jobs.import_detail")}</button>
              )}
            </div>
            <dl className="rounded-md border border-border px-3 dark:border-border">
              <DetailRow label={t("jobs.status")} value={<StatusBadge status={item.status} />} />
              {item.resource_state ? (
                <DetailRow
                  label={t("jobs.resource.label")}
                  value={(
                    <span className="flex flex-wrap items-center gap-2">
                      <StatusBadge
                        status={item.resource_state}
                        label={t(`jobs.resource.${item.resource_state}`)}
                      />
                      {item.resource_reason ? (
                        <span className="text-xs text-muted">{resourceReasonLabel(t, item.resource_reason)}</span>
                      ) : null}
                    </span>
                  )}
                />
              ) : null}
              <DetailRow label={t("jobs.kind")} value={item.kind} />
              <DetailRow label={t("jobs.operation")} value={item.operation_type || item.kind} />
              <DetailRow label={t("jobs.queue")} value={item.queue_name} />
              <DetailRow label="RQ" value={item.rq_job_id} />
              <DetailRow label={t("jobs.subject")} value={item.subject_type && item.subject_id ? `${item.subject_type}:${item.subject_id}` : undefined} />
              <DetailRow label={t("jobs.source")} value={item.source ? <span className="inline-flex items-center gap-2"><SourceBadge source={item.source} />{item.source}</span> : undefined} />
              <DetailRow label={t("jobs.source_url")} value={item.source_url} />
              <DetailRow label={t("jobs.created")} value={item.created_at ? fmt.dateTime(item.created_at) : undefined} />
              <DetailRow label={t("jobs.updated")} value={item.updated_at ? fmt.dateTime(item.updated_at) : undefined} />
              <DetailRow label={t("jobs.started")} value={item.started_at ? fmt.dateTime(item.started_at) : undefined} />
              <DetailRow label={t("jobs.finished")} value={item.finished_at ? fmt.dateTime(item.finished_at) : undefined} />
            </dl>
            {item.error_log && ["failed", "stale"].includes(item.status) && (
              <section>
                <h3 className="mb-2 text-sm font-semibold">{t("jobs.error_log")}</h3>
                <pre className="max-h-96 overflow-auto rounded-md border border-danger/20 bg-danger-subtle p-3 font-mono text-xs whitespace-pre-wrap text-danger dark:border-danger/30 dark:bg-danger-subtle dark:text-danger">{item.error_log}</pre>
              </section>
            )}
            <section>
              <h3 className="mb-2 text-sm font-semibold">{t("jobs.progress")}</h3>
              <JsonBlock value={item.progress_data || { stage: item.progress_stage, current: item.progress_current, total: item.progress_total }} />
            </section>
            <section>
              <h3 className="mb-2 text-sm font-semibold">{t("jobs.result")}</h3>
              {removedCount !== null && (
                <p className="mb-2 text-sm font-medium text-fg">{t("datamgmt.cleanup_json_done", { count: removedCount })}</p>
              )}
              {outcome ? <SyncOutcomeNotice outcome={outcome} /> : <JsonBlock value={item.result_data} />}
            </section>
            <section>
              <h3 className="mb-2 text-sm font-semibold">{t("jobs.meta")}</h3>
              <JsonBlock value={item.meta} />
            </section>
            <section>
              <h3 className="mb-2 text-sm font-semibold">{t("jobs.events")}</h3>
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
              ) : <p className="text-xs text-muted">{t("jobs.no_events")}</p>}
            </section>
          </div>
        )}
      </div>
    </aside>
    {confirmAction && <ConfirmDialog open title={confirmAction === "repeat_sync" ? t("jobs.repeat_sync_title") : t("jobs.delete_task_title")} message={confirmAction === "repeat_sync" ? t("jobs.repeat_sync_confirm") : t("jobs.delete_task_confirm")} onConfirm={() => taskAction.mutate(confirmAction)} onCancel={() => setConfirmAction(null)} isPending={taskAction.isPending} error={(taskAction.error as Error)?.message} />}
    {hasConflictWorkflow && (
      <DownloadConflictDialog
        open={showConflict}
        taskId={heldId}
        existingResolution={existingResolution}
        onClose={() => setShowConflict(false)}
      />
    )}
    </>
  );
}

export function JobDetailDrawer({
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
  const { mounted, closing } = usePresence(!!id, motionTokens.duration.base);
  const lastId = useRef<string | null>(null);
  if (id) lastId.current = id;
  const heldId = id ?? lastId.current;
  const download = useQuery({
    queryKey: queryKeys.downloadJobs.detail(heldId || ""),
    queryFn: () => api.getDownloadJob(heldId || ""),
    enabled: !!id && kind === "download",
  });
  const imports = useQuery({
    queryKey: queryKeys.downloadJobs.imports(heldId || ""),
    queryFn: () => api.getDownloadJobImports(heldId || ""),
    enabled: !!id && kind === "download",
  });
  const importJob = useQuery({
    queryKey: [...queryKeys.importJobs.all, "detail", heldId],
    queryFn: () => api.getImportJob(heldId || ""),
    enabled: !!id && kind === "import",
  });

  if (!mounted || !heldId) return null;
  const dl = download.data as DownloadJob | undefined;
  const im = importJob.data as ImportJob | undefined;
  const loading = kind === "download" ? download.isLoading : importJob.isLoading;
  const error = kind === "download" ? download.error : importJob.error;

  return (
    <>
    <div className={`fixed inset-0 z-[39] bg-black/30 ${closing ? "overlay-backdrop-exit" : "overlay-backdrop"}`} onClick={onClose} aria-hidden />
    <aside className={`fixed inset-y-0 right-0 z-40 flex w-full max-w-xl flex-col border-l border-border bg-white shadow-xl dark:border-border dark:bg-surface ${closing ? "drawer-panel-exit" : "drawer-panel"}`} aria-label={t("jobs.detail_title")}>
      <div className="flex items-center justify-between border-b border-border px-4 py-3 dark:border-border">
        <div className="min-w-0">
          <div className="text-sm font-semibold">{kind === "download" ? t("jobs.download_detail") : t("jobs.import_detail")}</div>
          <div className="font-mono text-xs text-muted">{shortId(heldId)}</div>
        </div>
        <button onClick={onClose} className="btn-icon border-0 text-lg leading-none" aria-label={t("common.close")}>×</button>
      </div>
      <div className="min-h-0 flex-1 overflow-auto p-4">
        {loading && <div className="h-24 animate-pulse rounded-md bg-subtle dark:bg-subtle" />}
        {error && <ErrorState message={(error as Error).message} onRetry={() => (kind === "download" ? download.refetch() : importJob.refetch())} />}

        {kind === "download" && dl && (
          <div className="space-y-4">
            <div className="flex flex-wrap gap-2">
              {hasTaskAction(dl, "retry") && <button onClick={() => onRetryDownload(dl.id)} className="btn-primary text-xs">{t("jobs.retry")}</button>}
              {hasTaskAction(dl, "pause") && <button onClick={() => onPauseDownload(dl.id)} className="btn-ghost text-xs">{t("jobs.pause")}</button>}
              {hasTaskAction(dl, "resume") && <button onClick={() => onResumeDownload(dl.id)} className="btn-ghost text-xs">{t("jobs.resume")}</button>}
              {hasTaskAction(dl, "delete") && <button onClick={() => onDeleteDownload(dl.id)} className="btn-danger text-xs">{t("jobs.del")}</button>}
            </div>
            {dl.retryable === false && dl.reason_code && (
              <div className="rounded-md border border-warning/40 bg-warning-subtle px-3 py-2 text-sm text-warning" role="alert">
                {t("jobs.staging_conflict_manual")}
              </div>
            )}
            <dl className="rounded-md border border-border px-3 dark:border-border">
              <DetailRow label={t("jobs.status")} value={statusLabel(t, dl.status)} />
              <DetailRow label={t("jobs.source")} value={<span className="inline-flex items-center gap-2"><SourceBadge source={dl.source} />{dl.source}</span>} />
              <DetailRow label={t("jobs.source_url")} value={dl.source_url} />
              <DetailRow label={t("jobs.creator")} value={dl.creator_id ? <Link href={`/admin/creators/${dl.creator_id}`} className="text-accent hover:underline dark:text-accent">{dl.creator_name || shortId(dl.creator_id)}</Link> : dl.creator_name} />
              <DetailRow label={t("jobs.subscription")} value={dl.subscription_id ? <Link href={`/admin/subscriptions/${dl.subscription_id}`} className="text-accent hover:underline dark:text-accent">{dl.subscription_name || shortId(dl.subscription_id)}</Link> : undefined} />
              <DetailRow label={t("jobs.repository")} value={dl.subscription_source_id ? <Link href={adminRoutes.repository(dl.subscription_source_id)} className="text-accent hover:underline dark:text-accent">{shortId(dl.subscription_source_id)}</Link> : undefined} />
              <DetailRow label={t("jobs.created")} value={fmt.dateTime(dl.created_at)} />
              <DetailRow label={t("jobs.updated")} value={fmt.dateTime(dl.updated_at)} />
              {dl.last_heartbeat_at && (
                <DetailRow label={t("jobs.last_heartbeat")} value={
                  <span className="text-xs">
                    {fmt.relative(dl.last_heartbeat_at)}
                    {dl.status === "stale" && (
                      <span className="ml-2 font-medium text-warning">
                        {t("jobs.stale_lost_heartbeat", { time: fmt.relative(dl.last_heartbeat_at) || "—" })}
                      </span>
                    )}
                  </span>
                } />
              )}
              {dl.retry_count > 0 && (
                <DetailRow label={t("jobs.recovery_retry", { current: String(dl.retry_count), max: "3" })} value={
                  <span className="text-xs text-accent font-medium">{dl.retry_count} / 3</span>
                } />
              )}
            </dl>
            {dl.outcome && <SyncOutcomeNotice outcome={dl.outcome} />}
            {dl.error_log && ["failed", "stale"].includes(dl.status) && (() => {
              const errInfo = classifyError(dl.error_log);
              const hintKey = `jobs.error_type_${errInfo.type}`;
              return (
              <section>
                <h3 className="mb-2 text-sm font-semibold flex items-center gap-2">
                  {t("jobs.error_log")}
                  {errInfo.type !== "unknown" && (
                    <span className={`text-xs px-2 py-0.5 rounded-full ${
                      errInfo.type === "auth" ? "bg-danger-subtle text-danger" :
                      errInfo.type === "timeout" || errInfo.type === "stall" ? "bg-warning-subtle text-warning" :
                      "bg-subtle text-muted dark:bg-subtle dark:text-muted"
                    }`}>{t(hintKey)}</span>
                  )}
                </h3>
                {errInfo.type === "auth" && (
                  <p className="text-xs text-danger mb-2">
                    {t("auth.desc")}: <Link href={adminRoutes.schedulerAuth} className="underline">{t("settings.auth")} →</Link>
                  </p>
                )}
                {errInfo.type === "timeout" && (
                  <p className="text-xs text-warning mb-2">
                    {t("dldefaults.timeout.desc")}: <Link href="/admin/settings/download-defaults" className="underline">{t("dldefaults.title")} →</Link>
                  </p>
                )}
                {errInfo.type === "stall" && (
                  <p className="text-xs text-warning mb-2">
                    {t("dldefaults.stall_timeout.desc")}: <Link href="/admin/settings/download-defaults" className="underline">{t("dldefaults.title")} →</Link>
                  </p>
                )}
                <pre className="max-h-64 overflow-auto rounded-md border border-danger/20 bg-danger-subtle p-3 font-mono text-xs whitespace-pre-wrap text-danger dark:border-danger/30 dark:bg-danger-subtle dark:text-danger">{dl.error_log}</pre>
              </section>
              );
            })()}
            {dl.conflict_details && dl.conflict_details.length > 0 && (
              <section>
                <h3 className="mb-2 text-sm font-semibold">{t("jobs.conflict_details")}</h3>
                <JsonBlock value={dl.conflict_details} />
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
              {imports.isLoading && <div className="h-12 animate-pulse rounded bg-subtle dark:bg-subtle" />}
              {imports.data?.length ? (
                <div className="space-y-1">
                  {imports.data.map((job: ImportJob) => (
                    <Link key={job.id} href={`/admin/jobs?tab=imports&q=${encodeURIComponent(`kind:import "${dl.id}"`)}&import_job=${job.id}`} className="flex items-center justify-between rounded-md border border-border px-3 py-2 text-sm hover:bg-subtle dark:border-border dark:hover:bg-subtle">
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
              {hasTaskAction(im, "retry") && <button onClick={() => onRetryImport(im.id)} className="btn-primary text-xs">{t("jobs.retry")}</button>}
              {hasTaskAction(im, "delete") && <button onClick={() => onDeleteImport(im.id)} className="btn-danger text-xs">{t("jobs.del")}</button>}
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
    </>
  );
}

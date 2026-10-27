"use client";
import Link from "next/link";
import { useRef, type ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, DownloadJob, ImportJob, queryKeys } from "@/lib/api";
import ErrorState from "@/components/ErrorState";
import SourceBadge from "@/components/SourceBadge";
import { SyncOutcomeNotice } from "@/components/SyncOutcomeBadge";
import { useT } from "@/lib/i18n";
import { usePresence, motionTokens } from "@/lib/motion";
import { statusLabel, useI18nFormat } from "@/lib/i18n-format";
import { classifyError } from "@/lib/jobCategory";
import { adminRoutes } from "@/lib/adminRoutes";
import { actionErrorReason, hasTaskAction } from "@/lib/task-actions";
import { usePermissions } from "@/lib/usePermissions";

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

export function JobDetailDrawer({
  kind,
  id,
  onClose,
  onRetryDownload,
  onPauseDownload,
  onResumeDownload,
  onRepeatDownload,
  onDeleteDownload,
  onRetryImport,
  onDeleteImport,
  actionPending = false,
  actionError,
  embedded = false,
}: {
  kind: "download" | "import";
  id: string | null;
  onClose: () => void;
  onRetryDownload: (id: string) => void;
  onPauseDownload: (id: string) => void;
  onResumeDownload: (id: string) => void;
  onRepeatDownload: (id: string) => void;
  onDeleteDownload: (id: string) => void;
  onRetryImport: (id: string) => void;
  onDeleteImport: (id: string) => void;
  actionPending?: boolean;
  actionError?: unknown;
  embedded?: boolean;
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
    {!embedded && <div className={`fixed inset-0 z-[39] bg-black/30 ${closing ? "overlay-backdrop-exit" : "overlay-backdrop"}`} onClick={onClose} aria-hidden />}
    <aside className={embedded ? "min-w-0" : `fixed inset-y-0 right-0 z-40 flex w-full max-w-xl flex-col border-l border-border bg-white shadow-xl dark:border-border dark:bg-surface ${closing ? "drawer-panel-exit" : "drawer-panel"}`} aria-label={t("jobs.detail_title")}>
      {!embedded && <>
      <div className="flex items-center justify-between border-b border-border px-4 py-3 dark:border-border">
        <div className="min-w-0">
          <div className="text-sm font-semibold">{kind === "download" ? t("jobs.download_detail") : t("jobs.import_detail")}</div>
          <div className="font-mono text-xs text-muted">{shortId(heldId)}</div>
        </div>
        <button onClick={onClose} className="btn-icon border-0 text-lg leading-none" aria-label={t("common.close")}>×</button>
      </div>
      </>}
      <div className="min-h-0 flex-1 overflow-auto p-4">
        {loading && <div className="h-24 animate-pulse rounded-md bg-subtle dark:bg-subtle" />}
        {error && <ErrorState message={(error as Error).message} onRetry={() => (kind === "download" ? download.refetch() : importJob.refetch())} />}

        {kind === "download" && dl && (
          <div className="space-y-4">
            <div className="flex flex-wrap gap-2">
              {hasTaskAction(dl, "retry") && <button disabled={actionPending} onClick={() => onRetryDownload(dl.id)} className="btn-primary text-xs">{t("jobs.retry")}</button>}
              {hasTaskAction(dl, "pause") && <button disabled={actionPending} onClick={() => onPauseDownload(dl.id)} className="btn-ghost text-xs">{t("jobs.pause")}</button>}
              {hasTaskAction(dl, "resume") && <button disabled={actionPending} onClick={() => onResumeDownload(dl.id)} className="btn-ghost text-xs">{t("jobs.resume")}</button>}
              {hasTaskAction(dl, "repeat_sync") && <button disabled={actionPending} onClick={() => onRepeatDownload(dl.id)} className="btn-primary text-xs">{t("jobs.repeat_sync")}</button>}
              {hasTaskAction(dl, "delete") && <button disabled={actionPending} onClick={() => onDeleteDownload(dl.id)} className="btn-danger text-xs">{t(embedded ? "common.delete" : "jobs.del")}</button>}
            </div>
            {Boolean(actionError) && <p role="alert" className="text-sm text-danger">{actionErrorReason(actionError)}</p>}
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
              {hasTaskAction(im, "retry") && <button disabled={actionPending} onClick={() => onRetryImport(im.id)} className="btn-primary text-xs">{t("jobs.retry")}</button>}
              {hasTaskAction(im, "delete") && <button disabled={actionPending} onClick={() => onDeleteImport(im.id)} className="btn-danger text-xs">{t(embedded ? "common.delete" : "jobs.del")}</button>}
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

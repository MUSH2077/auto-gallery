"use client";

import Link from "next/link";
import { useMemo, useState } from "react";
import {
  AlertTriangle,
  Box,
  ChevronRight,
  Database,
  Download,
  ImageOff,
  Layers3,
  RefreshCw,
  Search,
  Server,
} from "lucide-react";

import SourceBadge from "@/components/SourceBadge";
import StatusBadge from "@/components/StatusBadge";
import { api, type WorkbenchSummary } from "@/lib/api";
import { useT } from "@/lib/i18n";
import { useI18nFormat } from "@/lib/i18n-format";
import { useStaggeredEntrance } from "@/lib/motion";

type DashboardActivity = {
  key: string;
  id: string;
  kind: "download" | "import" | "sync";
  source?: string | null;
  title: string;
  detail: string;
  status: string;
  timestamp?: string | null;
  href: string;
  progress?: number | null;
  progressLabel?: string | null;
  retryable: boolean;
};

type StatusTone = "ok" | "info" | "danger" | "warning" | "muted";

const ACTIVE_STATUSES = new Set([
  "pending",
  "enqueued",
  "downloading",
  "downloaded",
  "importing",
  "running",
  "configuring",
  "post_download",
  "enqueuing_import",
  "import_indexing",
]);
const FAILED_STATUSES = new Set(["failed", "stale", "error"]);
const COMPLETE_STATUSES = new Set(["complete", "completed"]);

function fmtBytes(bytes?: number | null): string {
  if (!bytes) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  const unit = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
  return `${(bytes / Math.pow(1024, unit)).toFixed(unit > 0 ? 1 : 0)} ${units[unit]}`;
}

function progressValue(
  data?: { percent?: number; current?: number; total?: number } | null,
  current?: number | null,
  total?: number | null,
): { percent: number | null; label: string | null } {
  const resolvedCurrent = data?.current ?? current ?? null;
  const resolvedTotal = data?.total ?? total ?? null;
  const percent = data?.percent
    ?? (resolvedCurrent !== null && resolvedTotal ? (resolvedCurrent / resolvedTotal) * 100 : null);
  return {
    percent: percent === null ? null : Math.min(100, Math.max(0, percent)),
    label: resolvedCurrent !== null && resolvedTotal ? `${resolvedCurrent}/${resolvedTotal}` : null,
  };
}

function sourceHandle(sourceUrl?: string | null): string {
  if (!sourceUrl) return "";
  try {
    const parsed = new URL(sourceUrl);
    return parsed.pathname.split("/").filter(Boolean).at(-1) || parsed.hostname;
  } catch {
    return sourceUrl;
  }
}

function activityGroup(status: string): "active" | "failed" | "complete" | "other" {
  const normalized = status.toLowerCase();
  if (ACTIVE_STATUSES.has(normalized)) return "active";
  if (FAILED_STATUSES.has(normalized)) return "failed";
  if (COMPLETE_STATUSES.has(normalized)) return "complete";
  return "other";
}

function toneDot(tone: StatusTone): string {
  return {
    ok: "bg-success",
    info: "bg-accent",
    danger: "bg-danger",
    warning: "bg-warning",
    muted: "bg-placeholder",
  }[tone];
}

function DashboardStatusLink({
  label,
  value,
  href,
  tone,
  testId,
}: {
  label: string;
  value: string;
  href: string;
  tone: StatusTone;
  testId: string;
}) {
  const t = useT();
  return (
    <Link
      href={href}
      data-testid={testId}
      className="group flex min-h-[108px] min-w-0 flex-col justify-between border-b border-r border-border p-4 outline-none transition-colors hover:bg-subtle focus-visible:z-10 focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-accent/50 md:min-h-[112px] xl:border-b-0"
    >
      <div className="flex min-w-0 items-center gap-2 text-xs font-medium uppercase tracking-wide text-muted">
        <span>{label}</span>
        <span className={`h-2 w-2 shrink-0 rounded-full ${toneDot(tone)}`} />
      </div>
      <strong className="mt-3 truncate text-xl font-semibold tabular text-fg">{value}</strong>
      <span className="mt-2 inline-flex min-h-6 items-center gap-1 text-sm font-medium text-accent">
        {t("dashboard.open")}
        <ChevronRight className="h-3.5 w-3.5 transition-transform group-hover:translate-x-0.5" aria-hidden />
      </span>
    </Link>
  );
}

export function DashboardStatusStrip({
  data,
  refreshing,
  onRefresh,
}: {
  data: WorkbenchSummary;
  refreshing: boolean;
  onRefresh: () => void;
}) {
  const t = useT();
  const fmt = useI18nFormat();
  const activeJobs = data.queue.active_download_count + data.queue.active_import_count;
  const failedJobs = data.queue.failed_download_count + data.queue.failed_import_count;

  return (
    <section className="overflow-hidden rounded-lg border border-border bg-surface" aria-label={t("dashboard.operational_status")}>
      <div className="grid grid-cols-2 md:grid-cols-3 xl:grid-cols-6">
        <DashboardStatusLink
          label={t("dashboard.auto_sync")}
          value={data.scheduler.enabled ? t("common.on") : t("common.off")}
          href="/admin/scheduler"
          tone={data.scheduler.enabled ? "ok" : "danger"}
          testId="dashboard-status-scheduler"
        />
        <DashboardStatusLink
          label={t("dashboard.queue")}
          value={t("dashboard.active_count", { count: activeJobs })}
          href="/admin/jobs"
          tone={activeJobs > 0 ? "info" : "muted"}
          testId="dashboard-status-jobs"
        />
        <DashboardStatusLink
          label={t("dashboard.failed")}
          value={String(failedJobs)}
          href="/admin/jobs?q=status%3Afailed"
          tone={failedJobs > 0 ? "danger" : "ok"}
          testId="dashboard-status-failed"
        />
        <DashboardStatusLink
          label={t("dashboard.stale")}
          value={String(data.queue.stale_count)}
          href="/admin/jobs?q=status%3Astale"
          tone={data.queue.stale_count > 0 ? "warning" : "ok"}
          testId="dashboard-status-stale"
        />
        <DashboardStatusLink
          label={t("dashboard.disk")}
          value={data.storage.disk_free_percent == null
            ? "—"
            : t("dashboard.disk_free_percent", { percent: data.storage.disk_free_percent })}
          href="/admin/data-mgmt"
          tone={data.storage.risk_level === "critical"
            ? "danger"
            : data.storage.risk_level === "warning" ? "warning" : "ok"}
          testId="dashboard-status-storage"
        />
        <div className="flex min-h-[108px] min-w-0 flex-col justify-between border-b border-border p-4 md:min-h-[112px] md:border-r xl:border-b-0 xl:border-r-0">
          <span className="text-xs font-medium uppercase tracking-wide text-muted">{t("dashboard.updated_label")}</span>
          <time className="mt-3 whitespace-nowrap text-xs tabular text-muted sm:text-sm" dateTime={data.updated_at} aria-live="polite">
            {fmt.dateTime(data.updated_at)}
          </time>
          <button
            type="button"
            className="mt-2 inline-flex min-h-8 w-fit items-center gap-2 rounded-md text-sm font-medium text-accent outline-none hover:underline focus-visible:ring-2 focus-visible:ring-accent/50 disabled:cursor-wait disabled:opacity-60"
            onClick={onRefresh}
            disabled={refreshing}
            aria-busy={refreshing}
          >
            <RefreshCw className={`h-4 w-4 ${refreshing ? "animate-spin" : ""}`} aria-hidden />
            {refreshing ? t("dashboard.refreshing") : t("common.refresh")}
          </button>
        </div>
      </div>
    </section>
  );
}

function WorkThumbnail({ assetId, title }: { assetId?: string | null; title: string }) {
  const [failed, setFailed] = useState(false);
  if (!assetId || failed) {
    return (
      <div className="flex h-full w-full items-center justify-center bg-subtle text-muted">
        <ImageOff className="h-7 w-7" aria-hidden />
      </div>
    );
  }
  return (
    <img
      src={api.mediaUrl(assetId, "thumb")}
      alt=""
      className="h-full w-full object-cover transition-transform duration-slow ease-expo group-hover:scale-[1.025]"
      loading="eager"
      decoding="async"
      onError={() => setFailed(true)}
      aria-describedby={`dashboard-work-${assetId}`}
      title={title}
    />
  );
}

export function RecentWorksPanel({ data }: { data: WorkbenchSummary }) {
  const t = useT();
  const fmt = useI18nFormat();
  const works = data.recent.works.slice(0, 3);
  const entrance = useStaggeredEntrance(works.map((work) => `dashboard-work:${work.id}`));

  return (
    <section className="min-w-0 sm:rounded-lg sm:border sm:border-border sm:bg-surface sm:p-5" aria-labelledby="dashboard-recent-works">
      <div className="mb-4 flex items-center justify-between gap-3">
        <h2 id="dashboard-recent-works" className="text-base font-semibold text-fg sm:text-lg">{t("dashboard.recently_added_works")}</h2>
        <Link href="/admin/works" className="shrink-0 text-sm font-medium text-accent hover:underline">
          {t("dashboard.open_works_library")}
        </Link>
      </div>
      {works.length ? (
        <div className="grid auto-cols-[minmax(230px,82%)] grid-flow-col gap-3 overflow-x-auto pb-2 [scrollbar-width:thin] snap-x snap-mandatory sm:auto-cols-auto sm:grid-flow-row sm:grid-cols-3 sm:overflow-visible sm:pb-0">
          {works.map((work, index) => {
            const title = work.title || t("dashboard.untitled_work");
            const motion = entrance(`dashboard-work:${work.id}`, index);
            return (
              <Link
                key={work.id}
                href={`/admin/works/${work.id}`}
                className={`${motion.className} group min-w-0 snap-start overflow-hidden rounded-lg border border-border bg-bg outline-none transition-colors hover:border-accent/50 focus-visible:ring-2 focus-visible:ring-accent/50`}
                style={motion.style}
                aria-label={t("common.open_item", { name: title })}
              >
                <div className="aspect-[4/3] overflow-hidden border-b border-border bg-subtle">
                  <WorkThumbnail assetId={work.thumbnail_asset_id} title={title} />
                </div>
                <div className="p-3" id={work.thumbnail_asset_id ? `dashboard-work-${work.thumbnail_asset_id}` : undefined}>
                  <h3 className="line-clamp-2 text-sm font-semibold leading-5 text-fg">{title}</h3>
                  <div className="mt-2 flex min-w-0 flex-wrap items-center gap-2">
                    {work.source && <SourceBadge source={work.source} />}
                    {work.creator_name && <span className="truncate text-xs text-muted">{work.creator_name}</span>}
                  </div>
                  <p className="mt-2 text-xs tabular text-muted">{fmt.relative(work.created_at)}</p>
                </div>
              </Link>
            );
          })}
        </div>
      ) : (
        <div className="flex min-h-56 flex-col items-center justify-center rounded-lg border border-dashed border-border px-6 text-center">
          <ImageOff className="h-8 w-8 text-muted" aria-hidden />
          <p className="mt-3 text-sm font-medium text-fg">{t("dashboard.no_recent_works")}</p>
          <p className="mt-1 max-w-sm text-xs leading-5 text-muted">{t("dashboard.no_recent_works_desc")}</p>
        </div>
      )}
    </section>
  );
}

function buildActivities(data: WorkbenchSummary, t: ReturnType<typeof useT>): DashboardActivity[] {
  const downloads = data.recent.download_jobs.map((job) => {
    const progress = progressValue(job.progress_data);
    return {
      key: `download:${job.id}`,
      id: job.id,
      kind: "download" as const,
      source: job.source,
      title: job.creator_name || job.subscription_name || sourceHandle(job.source_url) || job.id.slice(0, 8),
      detail: job.pipeline_stage || t("dashboard.activity_download"),
      status: job.status,
      timestamp: job.updated_at || job.created_at,
      href: `/admin/jobs?tab=downloads&job=${job.id}`,
      progress: progress.percent,
      progressLabel: progress.label,
      retryable: FAILED_STATUSES.has(job.status.toLowerCase()),
    };
  });
  const imports = data.recent.import_jobs.map((job) => {
    const progress = progressValue(job.progress_data, job.progress_works_done, job.progress_works_total);
    return {
      key: `import:${job.id}`,
      id: job.id,
      kind: "import" as const,
      source: job.source,
      title: job.creator_name || job.subscription_name || t("dashboard.import_for", { id: job.download_job_id.slice(0, 8) }),
      detail: job.progress_stage || t("dashboard.activity_import"),
      status: job.status,
      timestamp: job.updated_at || job.created_at,
      href: `/admin/jobs?tab=imports&import_job=${job.id}`,
      progress: progress.percent,
      progressLabel: progress.label,
      retryable: FAILED_STATUSES.has(job.status.toLowerCase()),
    };
  });
  const syncs = data.recent.successful_syncs.map((sync) => ({
    key: `sync:${sync.source_id}`,
    id: sync.source_id,
    kind: "sync" as const,
    source: sync.source,
    title: sync.creator_name,
    detail: t("dashboard.activity_sync"),
    status: "complete",
    timestamp: sync.last_synced_at,
    href: `/admin/creators/${sync.creator_id}`,
    progress: null,
    progressLabel: null,
    retryable: false,
  }));

  return [...downloads, ...imports, ...syncs]
    .sort((left, right) => {
      const rightTime = right.timestamp ? Date.parse(right.timestamp) : 0;
      const leftTime = left.timestamp ? Date.parse(left.timestamp) : 0;
      return rightTime - leftTime || left.key.localeCompare(right.key);
    })
    .slice(0, 8);
}

function ActivityRow({
  activity,
  canRetry,
  retrying,
  onRetry,
}: {
  activity: DashboardActivity;
  canRetry: boolean;
  retrying: boolean;
  onRetry: (activity: DashboardActivity) => void;
}) {
  const t = useT();
  const fmt = useI18nFormat();
  return (
    <div className="flex min-w-0 flex-nowrap items-center gap-2 border-t border-border px-3 py-3 first:border-t-0 sm:gap-3">
      <div className="w-20 shrink-0 sm:w-16">{activity.source ? <SourceBadge source={activity.source} /> : null}</div>
      <Link
        href={activity.href}
        className="flex min-h-11 min-w-0 flex-1 flex-col justify-center rounded outline-none focus-visible:ring-2 focus-visible:ring-accent/50"
      >
        <span className="block truncate text-sm font-semibold text-fg hover:text-accent">
          {activity.title}
        </span>
        <div className="mt-0.5 flex min-w-0 items-center gap-2 text-xs text-muted">
          <span className="truncate">{activity.detail}</span>
          {activity.progressLabel && <span className="shrink-0 tabular">{activity.progressLabel}</span>}
        </div>
        {activity.progress !== null && activity.progress !== undefined && (
          <div className="mt-2 h-1.5 max-w-52 overflow-hidden rounded-full bg-subtle" aria-hidden>
            <div
              className="h-full w-full rounded-full bg-accent transition-transform duration-slow ease-expo"
              style={{ transform: `scaleX(${activity.progress / 100})`, transformOrigin: "left" }}
            />
          </div>
        )}
      </Link>
      <div className="ml-auto flex min-h-11 shrink-0 items-center gap-1 sm:gap-2">
        <div className="text-right">
          <StatusBadge status={activity.status} className="py-0 text-[11px]" />
          <p className="mt-1 text-[11px] tabular text-muted">{fmt.relative(activity.timestamp)}</p>
        </div>
        {activity.retryable && canRetry && (
          <button
            type="button"
            className="btn-ghost min-h-11 px-2 text-xs sm:px-3"
            disabled={retrying}
            onClick={() => onRetry(activity)}
          >
            {retrying ? t("dashboard.retrying") : t("common.retry")}
          </button>
        )}
        <Link
          href={activity.href}
          className="btn-icon hidden min-h-11 min-w-11 border border-border sm:inline-flex"
          aria-label={t("common.open_item", { name: activity.title })}
        >
          <ChevronRight className="h-4 w-4" aria-hidden />
        </Link>
      </div>
    </div>
  );
}

export function ActivityPanel({
  data,
  canRetry,
  retryingKey,
  onRetry,
}: {
  data: WorkbenchSummary;
  canRetry: boolean;
  retryingKey?: string | null;
  onRetry: (activity: DashboardActivity) => void;
}) {
  const t = useT();
  const activities = useMemo(() => buildActivities(data, t), [data, t]);
  const grouped = {
    active: activities.filter((activity) => activityGroup(activity.status) === "active"),
    failed: activities.filter((activity) => activityGroup(activity.status) === "failed"),
    complete: activities.filter((activity) => activityGroup(activity.status) === "complete"),
    other: activities.filter((activity) => activityGroup(activity.status) === "other"),
  };
  const entrance = useStaggeredEntrance(activities.map((activity) => activity.key));
  let entranceIndex = 0;

  return (
    <section className="min-w-0 rounded-lg border border-border bg-surface p-4 sm:p-5" aria-labelledby="dashboard-live-activity">
      <div className="mb-4 flex items-center justify-between gap-3">
        <h2 id="dashboard-live-activity" className="text-lg font-semibold text-fg">{t("dashboard.live_activity")}</h2>
        <Link href="/admin/jobs" className="shrink-0 text-sm font-medium text-accent hover:underline">
          {t("dashboard.open_all_jobs")}
        </Link>
      </div>
      {activities.length ? (
        <div className="overflow-hidden rounded-lg border border-border">
          {(["active", "failed", "complete", "other"] as const).map((group) => {
            const rows = grouped[group];
            if (!rows.length) return null;
            return (
              <div key={group} className="border-t border-border first:border-t-0">
                <div className="flex items-center gap-2 bg-subtle px-3 py-2 text-xs font-semibold uppercase tracking-wide text-muted">
                  <span className={`h-2 w-2 rounded-full ${
                    group === "failed" ? "bg-danger" : group === "active" ? "bg-accent" : group === "complete" ? "bg-success" : "bg-muted"
                  }`} />
                  {t(`dashboard.activity_group_${group}`)}
                </div>
                {rows.map((activity) => {
                  const currentIndex = entranceIndex++;
                  const motion = entrance(activity.key, currentIndex);
                  return (
                    <div key={activity.key} className={motion.className} style={motion.style}>
                      <ActivityRow
                        activity={activity}
                        canRetry={canRetry}
                        retrying={retryingKey === activity.key}
                        onRetry={onRetry}
                      />
                    </div>
                  );
                })}
              </div>
            );
          })}
        </div>
      ) : (
        <div className="flex min-h-56 items-center justify-center rounded-lg border border-dashed border-border px-6 text-center text-sm text-muted">
          {t("dashboard.no_recent_activity")}
        </div>
      )}
    </section>
  );
}

export function AttentionBanner({
  data,
  canRetry,
  retrying,
  onRetryFailedDownloads,
}: {
  data: WorkbenchSummary;
  canRetry: boolean;
  retrying: boolean;
  onRetryFailedDownloads: () => void;
}) {
  const t = useT();
  const issueCount = data.attention.auth_unhealthy_count
    + data.attention.failed_download_count
    + data.attention.failed_import_count
    + data.attention.stale_job_count
    + Number(data.attention.low_disk_warning)
    + Number(data.attention.scheduler_disabled_warning);
  if (!issueCount) return null;

  const primary = data.attention.failed_download_count
    ? t("dashboard.attention_failed_downloads", { count: data.attention.failed_download_count })
    : data.attention.failed_import_count
      ? t("dashboard.attention_failed_imports", { count: data.attention.failed_import_count })
      : data.attention.stale_job_count
        ? t("dashboard.attention_stale", { count: data.attention.stale_job_count })
        : data.attention.auth_unhealthy_count
          ? t("dashboard.attention_auth", { count: data.attention.auth_unhealthy_count })
          : data.attention.low_disk_warning
            ? t("dashboard.attention_storage")
            : t("dashboard.attention_scheduler");
  const href = data.attention.failed_import_count && !data.attention.failed_download_count
    ? "/admin/jobs?tab=imports&q=kind%3Aimport%20status%3Afailed"
    : data.attention.auth_unhealthy_count && !data.attention.failed_download_count
      ? "/admin/settings/auth-status"
      : data.attention.low_disk_warning && !data.attention.failed_download_count
        ? "/admin/data-mgmt"
        : data.attention.scheduler_disabled_warning && !data.attention.failed_download_count
          ? "/admin/scheduler"
          : "/admin/jobs?q=status%3Afailed";

  return (
    <section className="flex flex-col gap-4 rounded-lg border border-danger/50 bg-danger-subtle p-4 sm:flex-row sm:items-center" aria-labelledby="dashboard-attention-heading">
      <AlertTriangle className="h-6 w-6 shrink-0 text-danger" aria-hidden />
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-2">
          <h2 id="dashboard-attention-heading" className="text-base font-semibold text-fg">{t("dashboard.attention_required")}</h2>
          <span className="badge border-danger/30 bg-danger-subtle text-danger">
            {t(issueCount === 1 ? "dashboard.issue_count_one" : "dashboard.issue_count", { count: issueCount })}
          </span>
        </div>
        <p className="mt-1 text-sm text-muted">{primary}</p>
      </div>
      <div className="grid shrink-0 gap-2 sm:flex">
        {data.attention.failed_download_count > 0 && canRetry && (
          <button
            type="button"
            className="btn-primary min-h-11"
            onClick={onRetryFailedDownloads}
            disabled={retrying}
          >
            {retrying ? t("dashboard.retrying") : t("dashboard.retry_failed_downloads")}
          </button>
        )}
        <Link href={href} className="btn-ghost min-h-11">{t("dashboard.view_attention")}</Link>
      </div>
    </section>
  );
}

const SERVICE_ICONS = {
  backend: Server,
  postgres: Database,
  redis: Layers3,
  meilisearch: Search,
  "gallery-dl": Download,
} as const;

export function ServicesPanel({ health }: { health: WorkbenchSummary["health"] }) {
  const t = useT();
  const services = Object.entries(health)
    .filter(([name]) => name !== "gallery-dl")
    .slice(0, 4);
  return (
    <section className="rounded-lg border border-border bg-surface p-4 sm:p-5" aria-labelledby="dashboard-services">
      <div className="mb-4 flex items-center justify-between gap-3">
        <h2 id="dashboard-services" className="text-lg font-semibold text-fg">{t("dashboard.services")}</h2>
        <Link href="/admin/system" className="text-sm font-medium text-accent hover:underline">{t("dashboard.open_system_status")}</Link>
      </div>
      <div className="grid grid-cols-2 gap-px overflow-hidden rounded-lg border border-border bg-border xl:grid-cols-4">
        {services.map(([name, status]) => {
          const Icon = SERVICE_ICONS[name as keyof typeof SERVICE_ICONS] || Box;
          return (
            <Link
              key={name}
              href="/admin/system"
              className="group flex min-h-24 min-w-0 items-center gap-3 bg-surface p-3 outline-none transition-colors hover:bg-subtle focus-visible:z-10 focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-accent/50"
            >
              <span className="flex h-10 w-10 shrink-0 items-center justify-center rounded-lg border border-border text-muted group-hover:text-fg">
                <Icon className="h-5 w-5" strokeWidth={1.8} aria-hidden />
              </span>
              <span className="min-w-0">
                <span className="block truncate text-sm font-semibold capitalize text-fg">{name}</span>
                <StatusBadge status={status} className="mt-1 py-0 text-[10px]" />
              </span>
            </Link>
          );
        })}
      </div>
    </section>
  );
}

export type { DashboardActivity };
export { fmtBytes };

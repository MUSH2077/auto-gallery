"use client";
import Link from "next/link";
import {WorkMediaThumbnail} from "@/components/MediaAssetRenderer";
import {SyncOutcomeBadge} from "@/components/SyncOutcomeBadge";
import StatusBadge from "@/components/StatusBadge";
import {type SyncOutcome,type WorkbenchSummary} from "@/lib/api";
import {useT} from "@/lib/i18n";
import {useI18nFormat} from "@/lib/i18n-format";
import {usePermissions} from "@/lib/usePermissions";
import {adminRoutes} from "@/lib/adminRoutes";
import {hasTaskAction} from "@/lib/task-actions";
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
  outcome?: SyncOutcome | null;
  retryable: boolean;
  repeatable: boolean;
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

function buildActivities(data: WorkbenchSummary, t: ReturnType<typeof useT>): DashboardActivity[] {
  const downloads = data.recent.download_jobs.filter((job) => {
    const status = job.status.toLowerCase();
    return ACTIVE_STATUSES.has(status) || FAILED_STATUSES.has(status) || Boolean(job.outcome) || hasTaskAction(job, "repeat_sync");
  }).map((job) => {
    const active = ACTIVE_STATUSES.has(job.status.toLowerCase());
    const progress = active ? progressValue(job.progress_data) : { percent: null, label: null };
    return {
      key: `download:${job.id}`,
      id: job.id,
      kind: "download" as const,
      source: job.source,
      title: job.creator_name || job.subscription_name || sourceHandle(job.source_url) || job.id.slice(0, 8),
      detail: job.outcome ? t(`sync_outcome.${job.outcome.code}_desc`) : job.pipeline_stage || t("dashboard.activity_download"),
      status: job.status,
      timestamp: job.updated_at || job.created_at,
      href: `/admin/jobs?records=download&job=${encodeURIComponent(job.id)}`,
      progress: progress.percent,
      progressLabel: progress.label,
      outcome: job.outcome,
      retryable: hasTaskAction(job, "retry"),
      repeatable: hasTaskAction(job, "repeat_sync"),
    };
  });
  const imports = data.recent.import_jobs.filter((job) => {
    const status = job.status.toLowerCase();
    return ACTIVE_STATUSES.has(status) || FAILED_STATUSES.has(status);
  }).map((job) => {
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
      href: `/admin/jobs?records=import&import_job=${encodeURIComponent(job.id)}`,
      progress: progress.percent,
      progressLabel: progress.label,
      retryable: hasTaskAction(job, "retry"),
      repeatable: false,
    };
  });
  return [...downloads, ...imports]
    .sort((left, right) => {
      const rightTime = right.timestamp ? Date.parse(right.timestamp) : 0;
      const leftTime = left.timestamp ? Date.parse(left.timestamp) : 0;
      return rightTime - leftTime || left.key.localeCompare(right.key);
    })
    .slice(0, 8);
}


export function DashboardStatusStrip({data,refreshing,onRefresh}:{data:WorkbenchSummary;refreshing:boolean;onRefresh:()=>void}){
 const t=useT(),fmt=useI18nFormat();
 const entries=[
  [t("dashboard.auto_sync"),data.scheduler.enabled?t("common.on"):t("common.off"),"/admin/scheduler"],
  [t("dashboard.queue"),t("dashboard.active_count",{count:data.queue.active_download_count+data.queue.active_import_count}),"/admin/jobs"],
  [t("dashboard.failed"),fmt.number(data.queue.failed_download_count+data.queue.failed_import_count),"/admin/jobs?q=status%3Afailed"],
  [t("dashboard.stale"),fmt.number(data.queue.stale_count),"/admin/jobs?q=status%3Astale"],
  [t("dashboard.disk"),data.storage.disk_free_percent==null?"—":t("dashboard.disk_free_percent",{percent:data.storage.disk_free_percent}),"/admin/data-mgmt"]
 ];
 return <section className="business-dashboard-status" aria-label={t("dashboard.operational_status")}><dl>{entries.map(([label,value,href])=><div key={label}><dt><Link href={href}>{label}</Link></dt><dd><Link href={href}>{value}</Link></dd></div>)}</dl><div className="business-dashboard-refresh"><time dateTime={data.updated_at}>{t("dashboard.updated_label")} {fmt.dateTime(data.updated_at)}</time><button className="button" type="button" disabled={refreshing} aria-busy={refreshing||undefined} onClick={onRefresh}>{t(refreshing?"dashboard.refreshing":"common.refresh")}</button></div></section>;
}
export function RecentWorksPanel({data}:{data:WorkbenchSummary}){
 const t=useT(),fmt=useI18nFormat(),{has}=usePermissions();
 if(!has("library"))return null;
 return <section className="business-section" aria-labelledby="dashboard-recent-works"><div className="business-section-heading"><h2 id="dashboard-recent-works">{t("dashboard.recently_added_works")}</h2><Link className="text-link" href="/admin/works">{t("dashboard.open_works_library")}</Link></div>{data.recent.works.length?<div className="business-dashboard-works">{data.recent.works.slice(0,3).map(work=><article key={work.id}><Link href={"/admin/works?work="+encodeURIComponent(work.id)}><div className="business-dashboard-image"><WorkMediaThumbnail assetId={work.thumbnail_asset_id} hasVideo={work.has_video} alt={work.title||t("dashboard.untitled_work")} className="h-full w-full object-cover" eager/></div><h3>{work.title||t("dashboard.untitled_work")}</h3></Link><p>{work.creator_name} {work.source}</p><time dateTime={work.created_at||undefined}>{fmt.relative(work.created_at)}</time></article>)}</div>:<p className="state-view">{t("dashboard.no_recent_works")}</p>}</section>;
}
export function ActivityPanel({data,canRetry,retryingKeys,onRetry,onRepeat}:{data:WorkbenchSummary;canRetry:boolean;retryingKeys?:ReadonlySet<string>;onRetry:(activity:DashboardActivity)=>void;onRepeat:(activity:DashboardActivity)=>void}){
 const t=useT(),fmt=useI18nFormat(),{has}=usePermissions(),activities=buildActivities(data,t);
 if(!has("tasks"))return null;
 return <section className="business-section" aria-labelledby="dashboard-live-activity"><div className="business-section-heading"><h2 id="dashboard-live-activity">{t("dashboard.live_activity")}</h2><Link className="text-link" href="/admin/jobs">{t("dashboard.open_all_jobs")}</Link></div>{activities.length?activities.map(activity=><article className="business-row business-dashboard-activity" key={activity.key} data-activity-id={activity.id}><div><Link href={activity.href}>{activity.title}</Link><p>{activity.detail}</p>{activity.progress!=null?<progress max={100} value={activity.progress} aria-label={activity.title}/>:null}{activity.progressLabel?<span>{activity.progressLabel}</span>:null}</div><div className="business-dashboard-outcome"><StatusBadge status={activity.status}/>{activity.outcome?<SyncOutcomeBadge outcome={activity.outcome}/>:null}<time dateTime={activity.timestamp||undefined}>{fmt.relative(activity.timestamp)}</time></div><div className="business-row-actions">{canRetry&&activity.retryable?<button type="button" className="text-button" disabled={retryingKeys?.has(activity.key)} onClick={()=>onRetry(activity)}>{t(retryingKeys?.has(activity.key)?"dashboard.retrying":"common.retry")}</button>:null}{canRetry&&activity.repeatable?<button type="button" className="text-button" onClick={()=>onRepeat(activity)}>{t("jobs.repeat_sync")}</button>:null}</div></article>):<p className="state-view">{t("dashboard.no_recent_activity")}</p>}</section>;
}
export function AttentionBanner({data,canRetry,retrying,onRetryFailedDownloads}:{data:WorkbenchSummary;canRetry:boolean;retrying:boolean;onRetryFailedDownloads:()=>void}){
 const t=useT();
  const authActionableCount = data.attention.auth_actionable_count ?? data.attention.auth_unhealthy_count;
  const issueCount = authActionableCount
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
        : authActionableCount
          ? t("dashboard.attention_auth", { count: authActionableCount })
          : data.attention.low_disk_warning
            ? t("dashboard.attention_storage")
            : t("dashboard.attention_scheduler");
  const href = data.attention.failed_import_count && !data.attention.failed_download_count
    ? "/admin/jobs?q=kind%3Aimport+status%3Afailed"
    : authActionableCount && !data.attention.failed_download_count
      ? adminRoutes.schedulerAuth
      : data.attention.low_disk_warning && !data.attention.failed_download_count
        ? "/admin/data-mgmt"
        : data.attention.scheduler_disabled_warning && !data.attention.failed_download_count
          ? adminRoutes.settingsSection("scheduler-defaults")
          : "/admin/jobs?scope=attention";


 return <section className="business-section business-dashboard-attention" aria-labelledby="dashboard-attention-heading"><div><h2 id="dashboard-attention-heading">{t("dashboard.attention_required")}</h2><p>{primary}</p><p>{t(issueCount===1?"dashboard.issue_count_one":"dashboard.issue_count",{count:issueCount})}</p></div><div className="business-row-actions">{canRetry&&data.attention.failed_download_count>0?<button type="button" className="button" disabled={retrying} onClick={onRetryFailedDownloads}>{t(retrying?"dashboard.retrying":"dashboard.retry_failed_downloads")}</button>:null}<Link className="text-link" href={href}>{t("dashboard.view_attention")}</Link></div></section>;
}
export function ServicesPanel({health}:{health:WorkbenchSummary["health"]}){
 const t=useT();
 return <details className="business-section business-dashboard-services"><summary>{t("dashboard.services")}</summary><div className="business-section-heading"><Link href="/admin/system" className="text-link">{t("dashboard.open_system_status")}</Link></div><dl>{Object.entries(health).map(([name,status])=><div key={name}><dt translate="no">{name}</dt><dd><StatusBadge status={status}/></dd></div>)}</dl></details>;
}
export type {DashboardActivity};
export {fmtBytes};

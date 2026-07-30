"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import type { KeyboardEvent, MouseEvent } from "react";
import type { CreatorRepository, RepositoryLatestJob, SchedulerDecisionItem } from "@/lib/api";
import { scheduleModeLabel, schedulerDecisionLabel, useI18nFormat } from "@/lib/i18n-format";
import { useT } from "@/lib/i18n";
import { adminRoutes } from "@/lib/adminRoutes";
import SourceBadge from "./SourceBadge";
import StatusBadge from "./StatusBadge";

type RepoLike = Pick<CreatorRepository,
  "id" | "subscription_id" | "source" | "source_display_name" | "source_creator_id" |
  "source_url" | "is_enabled" | "auth_healthy" | "last_synced_at" | "last_attempted_at" |
  "can_download" | "url_valid" | "is_repository" | "latest_job"
>;

function hostFromUrl(url?: string | null): string {
  if (!url) return "repo";
  try {
    return new URL(url).host.replace(/^www\./, "");
  } catch {
    return url.replace(/^https?:\/\//, "").split("/")[0] || url;
  }
}

function repoName(repo: RepoLike): string {
  const host = hostFromUrl(repo.source_url);
  const suffix = repo.source_creator_id || repo.source_url?.split("/").filter(Boolean).pop() || repo.id.slice(0, 8);
  return `${repo.source}/${suffix}`.replace(/\s+/g, "-") || host;
}

function isInteractiveTarget(target: EventTarget | null): boolean {
  return target instanceof Element && !!target.closest("a,button,input,select,textarea,[role='button']");
}

function DecisionPill({ decision }: { decision?: SchedulerDecisionItem }) {
  const t = useT();
  if (!decision) return null;
  const warning = ["auth_unhealthy", "url_invalid", "scheduler_disabled"].includes(decision.reason);
  const waiting = ["already_attempted_in_window", "manual_mode", "source_disabled"].includes(decision.reason);
  const cls = decision.due
    ? "border-accent/30 bg-accent-subtle text-accent dark:border-accent/30 dark:bg-accent-subtle dark:text-accent"
    : warning
      ? "border-danger/30 bg-danger-subtle text-danger dark:border-danger/30 dark:bg-danger-subtle dark:text-danger"
      : waiting
        ? "border-warning/30 bg-warning-subtle text-warning dark:bg-warning-subtle dark:text-warning"
        : "border-border bg-subtle text-muted dark:border-border dark:bg-subtle dark:text-muted";
  return (
    <span className={`inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5 text-xs font-medium ${cls}`}>
      <span className={`h-1.5 w-1.5 rounded-full ${decision.due ? "animate-pulse bg-current" : "bg-current"}`} />
      {schedulerDecisionLabel(t, decision.reason, decision.due)}
    </span>
  );
}

function JobPill({ job }: { job?: RepositoryLatestJob | null }) {
  const t = useT();
  if (!job) return <span className="text-xs text-muted">{t("repo.no_jobs")}</span>;
  return (
    <Link href={`/admin/jobs`} className="inline-flex">
      <StatusBadge status={job.status} />
    </Link>
  );
}

function RepoHealthLine({ repo }: { repo: RepoLike }) {
  const t = useT();
  const fmt = useI18nFormat();
  return (
    <>
      <span className="inline-flex items-center gap-1.5">
        <span className={`h-2 w-2 rounded-full ${repo.is_enabled ? "bg-success" : "bg-placeholder"}`} />
        {repo.is_enabled ? t("repo.enabled") : t("repo.disabled")}
      </span>
      <span className="inline-flex items-center gap-1.5">
        <span className={`h-2 w-2 rounded-full ${repo.auth_healthy ? "bg-success" : "bg-danger"}`} />
        {repo.auth_healthy ? t("repo.auth_healthy") : t("repo.auth_issue")}
      </span>
      <span>{t("repo.last_sync", { time: fmt.relative(repo.last_synced_at, "repo.never_synced") })}</span>
      <span>{t("repo.last_try", { time: fmt.relative(repo.last_attempted_at) })}</span>
    </>
  );
}

export default function RepositoryCard({
  repo,
  onSync,
  onToggle,
  onDelete,
  syncPending,
  togglePending,
  decision,
}: {
  repo: RepoLike;
  onSync?: (repo: RepoLike) => void;
  onToggle?: (repo: RepoLike) => void;
  onDelete?: (repo: RepoLike) => void;
  syncPending?: boolean;
  togglePending?: boolean;
  decision?: SchedulerDecisionItem;
}) {
  const t = useT();
  const fmt = useI18nFormat();
  const router = useRouter();
  const running = !!repo.latest_job && ["pending", "downloading", "downloaded", "importing"].includes(repo.latest_job.status);
  const legal = repo.is_repository;
  const disabledReason = !repo.can_download ? t("repo.provider_cannot_download") : !repo.url_valid ? t("repo.invalid_url") : null;
  const detailHref = adminRoutes.repository(repo.id);

  const openDetail = () => router.push(detailHref);
  const handleCardClick = (event: MouseEvent<HTMLElement>) => {
    if (isInteractiveTarget(event.target)) return;
    openDetail();
  };
  const handleCardKeyDown = (event: KeyboardEvent<HTMLElement>) => {
    if (event.key !== "Enter" || isInteractiveTarget(event.target)) return;
    openDetail();
  };

  return (
    <article
      role="link"
      tabIndex={0}
      onClick={handleCardClick}
      onKeyDown={handleCardKeyDown}
      aria-label={t("repo.open_details")}
      className="cursor-pointer rounded-md border border-border bg-white p-4 transition-colors hover:border-accent/50 focus:outline-none focus:ring-2 focus:ring-accent/40 dark:border-border dark:bg-surface dark:hover:border-accent/50 dark:focus:ring-accent/40"
    >
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <SourceBadge source={repo.source} />
            <h3 className="truncate text-base font-semibold text-accent">
              <button
                type="button"
                onClick={openDetail}
                className="min-w-0 truncate text-left hover:underline focus:outline-none focus:underline"
              >
                {repoName(repo)}
              </button>
            </h3>
            {!legal && (
              <span className="rounded-full border border-warning/30 bg-warning-subtle px-2 py-0.5 text-xs text-warning dark:bg-warning-subtle dark:text-warning">
                {t("repo.not_downloadable")}
              </span>
            )}
          </div>
          <p className="mt-2 truncate font-mono text-xs text-muted">
            {repo.source_url || t("repo.no_source_url")}
          </p>
          <div className="mt-3 flex flex-wrap items-center gap-x-4 gap-y-2 text-xs text-muted">
            <RepoHealthLine repo={repo} />
            <JobPill job={repo.latest_job} />
            <DecisionPill decision={decision} />
          </div>
          {decision && (
            <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted">
              <span>{t("repo.mode", { mode: scheduleModeLabel(t, decision.effective_mode) })}</span>
              <span>{t("repo.next", { time: fmt.dateTime(decision.next_due_at) })}</span>
              {decision.window_start && <span>{t("repo.window", { start: fmt.dateTime(decision.window_start), end: fmt.dateTime(decision.window_end) })}</span>}
            </div>
          )}
          {disabledReason && <p className="mt-2 text-xs text-warning dark:text-warning">{disabledReason}</p>}
          {repo.latest_job?.error_log_excerpt && (
            <p className="mt-2 line-clamp-2 rounded-md bg-danger-subtle px-2 py-1 text-xs text-danger dark:bg-danger-subtle dark:text-danger">
              {repo.latest_job.error_log_excerpt}
            </p>
          )}
        </div>
        <div className="flex shrink-0 flex-wrap gap-2">
          <Link href={detailHref} className="btn-ghost">
            {t("repo.open_details")}
          </Link>
          {repo.source_url && (
            <a href={repo.source_url} target="_blank" rel="noopener noreferrer" className="btn-ghost">
              {t("repo_detail.open_source")}
            </a>
          )}
          {onToggle && (
            <button onClick={() => onToggle(repo)} disabled={togglePending}
              className="btn-ghost disabled:opacity-50">
              {repo.is_enabled ? t("repo.disable") : t("repo.enable")}
            </button>
          )}
          {onSync && (
            <button onClick={() => onSync(repo)} disabled={!legal || !repo.is_enabled || running || syncPending}
              className="btn-ghost disabled:opacity-50">
              {running || syncPending ? t("repo.syncing") : t("repo.sync_now")}
            </button>
          )}
          {onDelete && (
            <button onClick={() => onDelete(repo)}
              className="rounded-md border border-border px-3 py-1.5 text-sm font-medium text-danger hover:bg-danger-subtle dark:border-border dark:text-danger dark:hover:bg-danger-subtle">
              {t("repo.remove")}
            </button>
          )}
        </div>
      </div>
    </article>
  );
}

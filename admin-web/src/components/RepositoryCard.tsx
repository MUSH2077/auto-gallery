"use client";

import Link from "next/link";
import { CreatorRepository, RepositoryLatestJob } from "@/lib/api";
import SourceBadge from "./SourceBadge";

type RepoLike = Pick<CreatorRepository,
  "id" | "subscription_id" | "source" | "source_display_name" | "source_creator_id" |
  "source_url" | "is_enabled" | "auth_healthy" | "last_synced_at" |
  "can_download" | "url_valid" | "is_repository" | "latest_job"
>;

function hostFromUrl(url?: string | null): string {
  if (!url) return "no-url";
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

function relativeTime(value?: string | null): string {
  if (!value) return "Never synced";
  const time = new Date(value).getTime();
  if (Number.isNaN(time)) return "Unknown";
  const diff = Date.now() - time;
  const minutes = Math.floor(diff / 60000);
  if (minutes < 1) return "Just now";
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  if (days < 30) return `${days}d ago`;
  return new Date(value).toLocaleDateString();
}

function JobPill({ job }: { job?: RepositoryLatestJob | null }) {
  if (!job) return <span className="text-xs text-[#57606a] dark:text-[#8b949e]">No jobs yet</span>;
  const running = ["pending", "downloading", "downloaded", "importing"].includes(job.status);
  const failed = ["failed", "stale"].includes(job.status);
  const cls = running
    ? "border-[#0969da]/30 bg-[#ddf4ff] text-[#0969da] dark:border-[#58a6ff]/30 dark:bg-[#1f6feb26] dark:text-[#58a6ff]"
    : failed
      ? "border-[#cf222e]/30 bg-[#ffebe9] text-[#cf222e] dark:border-[#f85149]/30 dark:bg-[#f8514926] dark:text-[#f85149]"
      : "border-[#1a7f37]/30 bg-[#dafbe1] text-[#1a7f37] dark:border-[#3fb950]/30 dark:bg-[#2ea04326] dark:text-[#3fb950]";
  return (
    <Link href={`/admin/downloads?job=${job.id}`} className={`inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5 text-xs font-medium ${cls}`}>
      <span className={`h-1.5 w-1.5 rounded-full ${running ? "animate-pulse bg-current" : "bg-current"}`} />
      {job.status}
    </Link>
  );
}

export default function RepositoryCard({
  repo,
  onSync,
  onToggle,
  onDelete,
  syncPending,
  togglePending,
}: {
  repo: RepoLike;
  onSync?: (repo: RepoLike) => void;
  onToggle?: (repo: RepoLike) => void;
  onDelete?: (repo: RepoLike) => void;
  syncPending?: boolean;
  togglePending?: boolean;
}) {
  const running = !!repo.latest_job && ["pending", "downloading", "downloaded", "importing"].includes(repo.latest_job.status);
  const legal = repo.is_repository;
  const disabledReason = !repo.can_download ? "Provider cannot download" : !repo.url_valid ? "Invalid gallery-dl URL" : null;

  return (
    <article className="rounded-md border border-[#d8dee4] bg-white p-4 transition-colors hover:border-[#0969da]/50 dark:border-[#30363d] dark:bg-[#161b22] dark:hover:border-[#58a6ff]/50">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <SourceBadge source={repo.source} />
            <h3 className="truncate text-base font-semibold text-[#0969da] dark:text-[#58a6ff]">
              {repo.source_url ? (
                <a href={repo.source_url} target="_blank" rel="noopener noreferrer" className="hover:underline">
                  {repoName(repo)}
                </a>
              ) : repoName(repo)}
            </h3>
            {!legal && (
              <span className="rounded-full border border-[#bf8700]/30 bg-[#fff8c5] px-2 py-0.5 text-xs text-[#9a6700] dark:bg-[#bb800926] dark:text-[#d29922]">
                Not downloadable
              </span>
            )}
          </div>
          <p className="mt-2 truncate font-mono text-xs text-[#57606a] dark:text-[#8b949e]">
            {repo.source_url || "No source URL configured"}
          </p>
          <div className="mt-3 flex flex-wrap items-center gap-x-4 gap-y-2 text-xs text-[#57606a] dark:text-[#8b949e]">
            <span className="inline-flex items-center gap-1.5">
              <span className={`h-2 w-2 rounded-full ${repo.is_enabled ? "bg-[#1a7f37]" : "bg-[#8c959f]"}`} />
              {repo.is_enabled ? "Enabled" : "Disabled"}
            </span>
            <span className="inline-flex items-center gap-1.5">
              <span className={`h-2 w-2 rounded-full ${repo.auth_healthy ? "bg-[#1a7f37]" : "bg-[#cf222e]"}`} />
              {repo.auth_healthy ? "Auth healthy" : "Auth issue"}
            </span>
            <span>Last sync {relativeTime(repo.last_synced_at)}</span>
            <JobPill job={repo.latest_job} />
          </div>
          {disabledReason && <p className="mt-2 text-xs text-[#9a6700] dark:text-[#d29922]">{disabledReason}</p>}
          {repo.latest_job?.error_log_excerpt && (
            <p className="mt-2 line-clamp-2 rounded-md bg-[#ffebe9] px-2 py-1 text-xs text-[#cf222e] dark:bg-[#f8514926] dark:text-[#f85149]">
              {repo.latest_job.error_log_excerpt}
            </p>
          )}
        </div>
        <div className="flex shrink-0 flex-wrap gap-2">
          {onToggle && (
            <button onClick={() => onToggle(repo)} disabled={togglePending}
              className="btn-ghost disabled:opacity-50">
              {repo.is_enabled ? "Disable" : "Enable"}
            </button>
          )}
          {onSync && (
            <button onClick={() => onSync(repo)} disabled={!legal || !repo.is_enabled || running || syncPending}
              className="btn-ghost disabled:opacity-50">
              {running || syncPending ? "Syncing" : "Sync now"}
            </button>
          )}
          {onDelete && (
            <button onClick={() => onDelete(repo)}
              className="rounded-md border border-[#d8dee4] px-3 py-1.5 text-sm font-medium text-[#cf222e] hover:bg-[#ffebe9] dark:border-[#30363d] dark:text-[#f85149] dark:hover:bg-[#f8514926]">
              Remove
            </button>
          )}
        </div>
      </div>
    </article>
  );
}

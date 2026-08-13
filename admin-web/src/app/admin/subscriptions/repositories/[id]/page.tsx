"use client";

import Link from "next/link";
import { useMemo, useState } from "react";
import { useParams } from "next/navigation";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, queryKeys } from "@/lib/api";
import type { CreatorRepository, RepositoryDetailResponse, RepositoryGraphNode, RepositoryRecentJob, RepositoryRecentWork, SchedulerDecisionItem } from "@/lib/api";
import { Breadcrumb, EmptyState, ErrorState, PageShell, SourceBadge, SyncOutcomeBadge, SyncOutcomeNotice, TagBubbleChart, WorkMediaThumbnail } from "@/components";
import { useToast } from "@/components/Toast";
import { useT } from "@/lib/i18n";
import { scheduleModeLabel, schedulerDecisionLabel, statusLabel, useI18nFormat } from "@/lib/i18n-format";
import { adminRoutes } from "@/lib/adminRoutes";

type TabKey = "overview" | "content" | "history" | "settings";

function hostFromUrl(url?: string | null): string {
  if (!url) return "repo";
  try {
    return new URL(url).host.replace(/^www\./, "");
  } catch {
    return url.replace(/^https?:\/\//, "").split("/")[0] || url;
  }
}

function repoName(repo: CreatorRepository): string {
  const suffix = repo.source_creator_id || repo.source_url?.split("/").filter(Boolean).pop() || repo.id.slice(0, 8);
  return `${repo.source}/${suffix || hostFromUrl(repo.source_url)}`.replace(/\s+/g, "-");
}

function statusClass(status?: string | null) {
  const running = ["pending", "downloading", "downloaded", "importing"].includes(status || "");
  const failed = ["failed", "stale"].includes(status || "");
  if (running) return "border-accent/30 bg-accent-subtle text-accent dark:border-accent/30 dark:bg-accent-subtle dark:text-accent";
  if (failed) return "border-danger/30 bg-danger-subtle text-danger dark:border-danger/30 dark:bg-danger-subtle dark:text-danger";
  return "border-success/30 bg-success-subtle text-success dark:border-success/30 dark:bg-success-subtle dark:text-success";
}

function Pill({ children, tone = "neutral" }: { children: React.ReactNode; tone?: "neutral" | "good" | "warn" | "bad" }) {
  const cls = tone === "good"
    ? "border-success/30 bg-success-subtle text-success dark:border-success/30 dark:bg-success-subtle dark:text-success"
    : tone === "warn"
      ? "border-warning/30 bg-warning-subtle text-warning dark:bg-warning-subtle dark:text-warning"
      : tone === "bad"
        ? "border-danger/30 bg-danger-subtle text-danger dark:border-danger/30 dark:bg-danger-subtle dark:text-danger"
        : "border-border bg-subtle text-muted dark:border-border dark:bg-subtle dark:text-muted";
  return <span className={`inline-flex items-center rounded-full border px-2 py-0.5 text-xs font-medium ${cls}`}>{children}</span>;
}

function StatCard({ label, value, hint }: { label: string; value: React.ReactNode; hint?: string }) {
  return (
    <div className="rounded-md border border-border bg-white p-3 dark:border-border dark:bg-surface">
      <div className="truncate text-sm font-semibold text-fg">{value}</div>
      <div className="mt-1 text-[11px] font-medium uppercase text-muted">{label}</div>
      {hint && <div className="mt-0.5 text-xs text-placeholder dark:text-muted">{hint}</div>}
    </div>
  );
}

function JobsList({ jobs }: { jobs: RepositoryRecentJob[] }) {
  const t = useT();
  const fmt = useI18nFormat();
  if (!jobs.length) {
    return <EmptyState title={t("repo_detail.no_jobs_title")} description={t("repo_detail.no_jobs_desc")} />;
  }
  return (
    <div className="divide-y divide-border rounded-md border border-border bg-white dark:divide-border dark:border-border dark:bg-surface">
      {jobs.map((job) => (
        <div key={job.id} className="p-4">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div className="min-w-0">
              <div className="flex flex-wrap items-center gap-2">
                <span className={`inline-flex items-center rounded-full border px-2 py-0.5 text-xs font-medium ${statusClass(job.status)}`}>
                  {statusLabel(t, job.status)}
                </span>
                {job.outcome && <SyncOutcomeBadge outcome={job.outcome} />}
                {job.retry_count > 0 && <Pill tone="warn">{t("repo_detail.retry_count", { count: job.retry_count })}</Pill>}
                {job.recovered && <Pill tone="good">{t("repo_detail.recovered")}</Pill>}
              </div>
              {job.error_log_excerpt && ["failed", "stale"].includes(job.status) && <p className="mt-2 line-clamp-2 text-xs text-danger dark:text-danger">{job.error_log_excerpt}</p>}
            </div>
            <div className="text-right text-xs text-muted">
              <div>{fmt.relative(job.created_at)}</div>
              <div>{fmt.dateTime(job.updated_at)}</div>
            </div>
          </div>
        </div>
      ))}
    </div>
  );
}

function WorksGrid({ works }: { works: RepositoryRecentWork[] }) {
  const t = useT();
  const fmt = useI18nFormat();
  if (!works.length) {
    return <EmptyState title={t("repo_detail.no_works_title")} description={t("repo_detail.no_works_desc")} />;
  }
  return (
    <div className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-4">
      {works.map((work) => (
        <Link key={work.id} href={`/admin/works/${work.id}`}
          className="group overflow-hidden rounded-md border border-border bg-white transition-colors hover:border-accent/50 dark:border-border dark:bg-surface dark:hover:border-accent/50">
          <div className="aspect-[4/3] bg-subtle">
            {work.thumbnail_asset_id ? (
              <WorkMediaThumbnail assetId={work.thumbnail_asset_id} hasVideo={work.has_video} alt={work.title || ""} className="h-full w-full object-cover" />
            ) : (
              <div className="flex h-full items-center justify-center text-xs text-muted">{t("works.na")}</div>
            )}
          </div>
          <div className="p-3">
            <div className="truncate text-sm font-medium group-hover:text-accent dark:group-hover:text-accent">{work.title || t("creator_detail.untitled")}</div>
            <div className="mt-1 flex flex-wrap items-center gap-2 text-xs text-muted">
              <span>{work.posted_at ? fmt.date(work.posted_at) : t("works.no_date")}</span>
              {work.asset_count > 1 && <span>{work.asset_count}p</span>}
              {work.is_nsfw && <Pill tone="bad">NSFW</Pill>}
              {work.is_ai_generated && <Pill tone="warn">AI</Pill>}
            </div>
          </div>
        </Link>
      ))}
    </div>
  );
}

function graphTone(node: RepositoryGraphNode) {
  if (node.is_baseline) return "border-placeholder bg-subtle";
  if (node.trigger.includes("purge") || node.trigger.includes("trash")) return "border-danger bg-danger-subtle dark:bg-danger-subtle";
  if (node.trigger.includes("restore") || node.trigger.includes("revert")) return "border-success bg-success-subtle dark:bg-success-subtle";
  return "border-accent bg-accent-subtle dark:bg-accent-subtle";
}

function RepositoryGraph({ repositoryId }: { repositoryId: string }) {
  const t = useT();
  const fmt = useI18nFormat();
  const [offset, setOffset] = useState(0);
  const [trigger, setTrigger] = useState("");
  const [includeBaseline, setIncludeBaseline] = useState(true);
  const [expanded, setExpanded] = useState<string | null>(null);
  const limit = 100;
  const graph = useQuery({
    queryKey: queryKeys.repositories.graph(repositoryId, offset, { trigger, includeBaseline }),
    queryFn: () => api.getRepositoryCurationGraph(repositoryId, offset, limit, { trigger: trigger || undefined, include_baseline: includeBaseline }),
  });
  const graphFilters = [
    ["", t("repo_detail.graph_filter_all")],
    ["baseline_backfill", t("repo_detail.graph_filter_baseline")],
    ["source_synced", t("repo_detail.graph_filter_sync")],
    ["work_trash", t("repo_detail.graph_filter_trash")],
    ["work_restore", t("repo_detail.graph_filter_restore")],
    ["work_purge", t("repo_detail.graph_filter_purge")],
    ["commit_revert", t("repo_detail.graph_filter_revert")],
  ];
  if (graph.isLoading) return <div className="h-24 animate-pulse rounded-md border border-border bg-white dark:border-border dark:bg-surface" />;
  if (!graph.data?.nodes.length) {
    return (
      <EmptyState
        title={t("repo_detail.graph_empty_title")}
        description={t("repo_detail.graph_empty_desc")}
        action={(
          <Link href={adminRoutes.curation} className="mt-3 inline-flex rounded-md border border-border px-3 py-1.5 text-sm font-medium text-accent hover:bg-subtle dark:border-border dark:text-accent dark:hover:bg-subtle">
            {t("repo_detail.open_curation")}
          </Link>
        )}
      />
    );
  }
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="text-sm text-muted">
          {t("repo_detail.graph_showing", { shown: graph.data.nodes.length, total: graph.data.total })}
        </div>
        <div className="flex flex-wrap gap-2">
          <button onClick={() => setIncludeBaseline((value) => !value)} className={`rounded-md border px-2.5 py-1.5 text-xs ${includeBaseline ? "border-border" : "border-accent bg-accent-subtle text-accent dark:border-accent dark:bg-accent-subtle dark:text-accent"}`}>
            {includeBaseline ? t("repo_detail.hide_baseline") : t("repo_detail.show_baseline")}
          </button>
          <Link href={`${adminRoutes.curation}?subject_type=repository&subject_id=${repositoryId}`} className="text-sm text-accent hover:underline dark:text-accent">{t("repo_detail.open_full_history")}</Link>
        </div>
      </div>
      <div className="flex flex-wrap gap-1">
        {graphFilters.map(([key, label]) => (
          <button key={key} onClick={() => { setTrigger(key); setOffset(0); }} className={`rounded-md border px-2.5 py-1.5 text-xs font-medium ${trigger === key ? "border-accent bg-accent-subtle text-accent dark:border-accent dark:bg-accent-subtle dark:text-accent" : "border-border hover:bg-subtle dark:border-border dark:hover:bg-subtle"}`}>
            {label}
          </button>
        ))}
      </div>
      <div className="rounded-md border border-border bg-white p-4 dark:border-border dark:bg-surface">
        {graph.data.nodes.map((node, index) => (
          <div key={node.id} className="relative grid grid-cols-[32px_minmax(0,1fr)] gap-3 pb-5 last:pb-0">
            {index < graph.data.nodes.length - 1 && <div className="absolute left-[15px] top-8 h-[calc(100%-1.5rem)] w-px bg-border dark:bg-border" />}
            <div className={`relative z-10 mt-1 h-8 w-8 rounded-full border-2 ${graphTone(node)}`} />
            <div className="min-w-0 rounded-md border border-border hover:border-accent/50 dark:border-border dark:hover:border-accent/50">
              <button
                type="button"
                onClick={() => setExpanded(expanded === node.id ? null : node.id)}
                aria-expanded={expanded === node.id}
                aria-controls={`repository-history-${node.id}`}
                className="block min-h-11 w-full rounded-md p-3 text-left focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent"
              >
              <span className="flex flex-wrap items-start justify-between gap-3">
                <div className="min-w-0">
                  <div className="truncate text-sm font-semibold">{node.message}</div>
                  <div className="mt-1 text-xs text-muted">
                    <span className="font-mono">{node.id.slice(0, 8)}</span>
                    <span className="mx-1.5">·</span>
                    <span>{node.trigger}</span>
                    {node.is_baseline && <span className="ml-1.5 rounded-full border border-border px-1.5 py-0.5 text-[10px] dark:border-border">{t("repo_detail.graph_baseline")}</span>}
                    <span className="mx-1.5">·</span>
                    <span>{fmt.dateTime(node.occurred_at)}</span>
                  </div>
                </div>
                {typeof node.stats?.work_count === "number" && <Pill>{t("repo_detail.graph_works_count", { count: node.stats.work_count })}</Pill>}
              </span>
              {node.changes_summary.length > 0 && (
                <div className="mt-3 flex flex-wrap gap-2">
                  {node.changes_summary.map((item) => (
                    <span key={item.action} className="rounded-full border border-border px-2 py-0.5 text-xs text-muted dark:border-border dark:text-muted">
                      {item.action.replaceAll("_", " ")} · {item.count}
                    </span>
                  ))}
                </div>
              )}
              {node.thumbnails.length > 0 && (
                <div className="mt-3 flex gap-2 overflow-x-auto">
                  {node.thumbnails.map((assetId) => (
                    <div key={assetId} className="h-14 w-14 shrink-0 overflow-hidden rounded-md border border-border bg-subtle dark:border-border dark:bg-subtle">
                      <WorkMediaThumbnail assetId={assetId} alt="" className="h-full w-full object-cover" />
                    </div>
                  ))}
                </div>
              )}
              </button>
              {expanded === node.id && (
                <div id={`repository-history-${node.id}`} className="border-t border-border p-3 text-xs text-muted dark:border-border dark:text-muted">
                  <div className="mb-2 font-medium text-fg">{t("repo_detail.graph_node_details")}</div>
                  <div className="flex flex-wrap gap-2">
                    <Link href={`${adminRoutes.curation}?commit=${node.id}`} className="inline-flex min-h-6 items-center text-accent hover:underline dark:text-accent">{t("repo_detail.open_commit")}</Link>
                    <Link href={`/admin/jobs?tab=downloads&q=${encodeURIComponent(`kind:download repo:${repositoryId}`)}`} className="inline-flex min-h-6 items-center text-accent hover:underline dark:text-accent">{t("repo_detail.open_jobs")}</Link>
                  </div>
                </div>
              )}
            </div>
          </div>
        ))}
      </div>
      {offset + limit < graph.data.total && (
        <button onClick={() => setOffset(offset + limit)} className="rounded-md border border-border px-3 py-1.5 text-sm hover:bg-subtle dark:border-border dark:hover:bg-subtle">
          {t("repo_detail.load_older")}
        </button>
      )}
      {offset > 0 && (
        <button onClick={() => setOffset(Math.max(0, offset - limit))} className="ml-2 rounded-md border border-border px-3 py-1.5 text-sm hover:bg-subtle dark:border-border dark:hover:bg-subtle">
          {t("repo_detail.newer")}
        </button>
      )}
    </div>
  );
}

function ConfigRows({ detail, decision }: { detail: RepositoryDetailResponse; decision?: SchedulerDecisionItem }) {
  const t = useT();
  const fmt = useI18nFormat();
  const { repository: repo, provider, subscription } = detail;
  const rows = [
    [t("repo_detail.source_url"), repo.source_url || "—"],
    [t("repo_detail.normalized_url"), provider.normalized_url || "—"],
    [t("repo_detail.source_creator_id"), repo.source_creator_id || "—"],
    [t("repo_detail.subscription"), subscription.name || subscription.id],
    [t("repo_detail.schedule_mode"), scheduleModeLabel(t, subscription.schedule_mode || "inherit")],
    [t("repo_detail.scheduled_times"), subscription.scheduled_times || "—"],
    [t("repo_detail.interval"), `${subscription.sync_interval_hours}h`],
    [t("repo_detail.next_due"), fmt.dateTime(decision?.next_due_at)],
  ];
  return (
    <div className="rounded-md border border-border bg-white dark:border-border dark:bg-surface">
      {rows.map(([label, value]) => (
        <div key={label} className="grid grid-cols-1 gap-1 border-b border-border px-4 py-3 text-sm last:border-b-0 dark:border-border md:grid-cols-[180px_1fr]">
          <dt className="text-muted">{label}</dt>
          <dd className="min-w-0 break-all font-mono text-xs text-fg">{value}</dd>
        </div>
      ))}
    </div>
  );
}

function nextActionHint(detail: RepositoryDetailResponse, decision?: SchedulerDecisionItem): { tone: "neutral" | "good" | "warn" | "bad"; text: string } {
  const repo = detail.repository;
  if (!repo.is_enabled) return { tone: "warn", text: "repo_detail.hint_enable_repo" };
  if (!repo.auth_healthy) return { tone: "bad", text: "repo_detail.hint_fix_auth" };
  if (!repo.url_valid) return { tone: "bad", text: "repo_detail.hint_fix_url" };
  if (repo.latest_job && ["failed", "stale"].includes(repo.latest_job.status)) return { tone: "bad", text: "repo_detail.hint_open_failed_jobs" };
  if (repo.latest_job && ["pending", "downloading", "downloaded", "importing"].includes(repo.latest_job.status)) return { tone: "neutral", text: "repo_detail.hint_job_running" };
  if (decision?.due) return { tone: "good", text: "repo_detail.hint_due_now" };
  return { tone: "neutral", text: "repo_detail.hint_wait_window" };
}

export default function RepositoryDetailPage() {
  const t = useT();
  const fmt = useI18nFormat();
  const toast = useToast();
  const params = useParams();
  const qc = useQueryClient();
  const id = params.id as string;
  const [tab, setTab] = useState<TabKey>("overview");
  const [tagPage, setTagPage] = useState(0);
  const tagLimit = 50;

  const detail = useQuery({ queryKey: queryKeys.repositories.detail(id), queryFn: () => api.getRepository(id), refetchInterval: 12000 });
  const decisions = useQuery({ queryKey: [...queryKeys.schedulerDecisions, "repository", id], queryFn: api.schedulerDecisions, refetchInterval: 15000 });
  const repositoryTags = useQuery({
    queryKey: queryKeys.repositories.tags(id, tagPage),
    queryFn: () => api.getRepositoryTags(id, tagPage * tagLimit, tagLimit),
    enabled: tab === "content",
  });

  const repo = detail.data?.repository;
  const decision = useMemo(() => decisions.data?.items.find((item) => item.source_id === id), [decisions.data?.items, id]);

  const sync = useMutation({
    mutationFn: () => api.syncRepository(id),
    onSuccess: (result) => {
      qc.invalidateQueries({ queryKey: queryKeys.repositories.detail(id) });
      qc.invalidateQueries({ queryKey: queryKeys.downloadJobs.all });
      qc.invalidateQueries({ queryKey: queryKeys.schedulerDecisions });
      const reason = typeof result.reason === "object" ? result.reason?.message : result.reason;
      toast.success(result.message || reason || t("repo_detail.sync_queued"));
    },
    onError: (e: Error) => toast.error(e.message),
  });

  const toggle = useMutation({
    mutationFn: () => {
      if (!repo) throw new Error(t("repo_detail.not_found"));
      return api.updateSubscriptionSource(repo.subscription_id, repo.id, { is_enabled: !repo.is_enabled });
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: queryKeys.repositories.detail(id) });
      qc.invalidateQueries({ queryKey: queryKeys.schedulerDecisions });
    },
    onError: (e: Error) => toast.error(e.message),
  });

  if (detail.isLoading) {
    return <PageShell><div className="space-y-4">{Array.from({ length: 4 }).map((_, i) => <div key={i} className="h-28 animate-pulse rounded-md bg-subtle dark:bg-subtle" />)}</div></PageShell>;
  }
  if (detail.error) {
    return <PageShell><ErrorState message={(detail.error as Error).message} onRetry={() => detail.refetch()} /></PageShell>;
  }
  if (!detail.data || !repo) return null;

  const { creator, subscription, provider, recent_works } = detail.data;
  const syncHistory = detail.data.sync_history || detail.data.recent_jobs || [];
  const running = !!repo.latest_job && ["pending", "downloading", "downloaded", "importing"].includes(repo.latest_job.status);
  const canSync = repo.is_repository && repo.is_enabled && !running;
  const hint = nextActionHint(detail.data, decision);
  const tabs: { key: TabKey; label: string; count?: number }[] = [
    { key: "overview", label: t("repo_detail.tab_overview") },
    { key: "content", label: t("repo_detail.tab_content"), count: recent_works.length },
    { key: "history", label: t("repo_detail.tab_sync_history"), count: syncHistory.length },
    { key: "settings", label: t("repo_detail.tab_settings") },
  ];

  return (
    <PageShell>
      <Breadcrumb items={[
        { label: t("subscriptions.title"), href: adminRoutes.subscriptions },
        {
          label: subscription.name || creator.display_name || creator.name,
          href: adminRoutes.subscription(subscription.id),
        },
        { label: repoName(repo) },
      ]} />

      <header className="border-b border-border pb-5 dark:border-border">
        <div className="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <SourceBadge source={repo.source} />
              <h1 className="min-w-0 truncate text-2xl font-semibold tracking-normal text-fg">{repoName(repo)}</h1>
              <Pill tone={repo.is_enabled ? "good" : "neutral"}>{repo.is_enabled ? t("repo.enabled") : t("repo.disabled")}</Pill>
              <Pill tone={repo.auth_healthy ? "good" : "bad"}>{repo.auth_healthy ? t("repo.auth_healthy") : t("repo.auth_issue")}</Pill>
              <Pill tone={repo.url_valid ? "good" : "warn"}>{repo.url_valid ? t("repo_detail.valid_url") : t("repo.invalid_url")}</Pill>
            </div>
            <p className="mt-2 max-w-4xl truncate font-mono text-xs text-muted">{repo.source_url || t("repo.no_source_url")}</p>
            <div className="mt-3 flex flex-wrap items-center gap-3 text-sm text-muted">
              <Link href={`/admin/creators/${creator.id}`} className="text-accent hover:underline dark:text-accent">{creator.display_name || creator.name}</Link>
              <span>{provider.display_name}</span>
              <span>{t("repo.last_sync", { time: fmt.relative(repo.last_synced_at, "repo.never_synced") })}</span>
              {decision && <span>{schedulerDecisionLabel(t, decision.reason, decision.due)}</span>}
            </div>
          </div>
          <div className="flex flex-wrap gap-2">
            <button onClick={() => sync.mutate()} disabled={!canSync || sync.isPending} className="btn-primary disabled:opacity-50">
              {running || sync.isPending ? t("repo.syncing") : t("repo.sync_now")}
            </button>
            <button onClick={() => toggle.mutate()} disabled={toggle.isPending} className="btn-ghost disabled:opacity-50">
              {repo.is_enabled ? t("repo.disable") : t("repo.enable")}
            </button>
            <Link href={`/admin/subscriptions/${subscription.id}`} className="btn-ghost">{t("repo_detail.open_subscription")}</Link>
            {repo.source_url && <a href={repo.source_url} target="_blank" rel="noopener noreferrer" className="btn-ghost">{t("repo_detail.open_source")}</a>}
          </div>
        </div>
      </header>

      <nav className="mt-4 flex gap-1 overflow-x-auto border-b border-border" aria-label={t("repo_detail.sections")}>
        {tabs.map((item) => (
          <button key={item.key} onClick={() => setTab(item.key)}
            className={`whitespace-nowrap border-b-2 px-3 py-2 text-sm ${tab === item.key ? "border-danger font-semibold text-fg" : "border-transparent text-muted hover:text-fg dark:text-muted dark:hover:text-fg"}`}>
            {item.label}{item.count !== undefined && <span className="ml-2 rounded-full bg-subtle px-2 py-0.5 text-xs font-medium text-muted dark:bg-border dark:text-muted">{item.count}</span>}
          </button>
        ))}
      </nav>

      <section className="mt-5">
        {tab === "overview" && (
          <div className="space-y-5">
            <div className={`rounded-md border p-4 text-sm ${hint.tone === "bad" ? "border-danger/30 bg-danger-subtle text-danger dark:border-danger/30 dark:bg-danger-subtle dark:text-danger" : hint.tone === "warn" ? "border-warning/30 bg-warning-subtle text-warning dark:bg-warning-subtle dark:text-warning" : "border-border bg-white text-fg dark:border-border dark:bg-surface dark:text-fg"}`}>
              <div className="font-semibold">{t("repo_detail.next_action")}</div>
              <div className="mt-1">{t(hint.text)}</div>
              <div className="mt-3 flex flex-wrap gap-2">
                <Link href={`${adminRoutes.jobs}?view=attention&q=${encodeURIComponent(id)}`} className="btn-ghost text-xs">{t("repo_detail.open_jobs")}</Link>
                <Link href={adminRoutes.settingsSection("scheduler-defaults")} className="btn-ghost text-xs">{t("repo_detail.open_scheduler")}</Link>
                <button onClick={() => sync.mutate()} disabled={!canSync || sync.isPending} className="btn-primary text-xs disabled:opacity-50">{t("repo.sync_now")}</button>
              </div>
            </div>
            <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
              <StatCard label={t("repo_detail.latest_job")} value={repo.latest_job ? statusLabel(t, repo.latest_job.status) : t("repo.no_jobs")} hint={repo.latest_job ? fmt.relative(repo.latest_job.created_at) : undefined} />
              <StatCard label={t("repo_detail.last_attempt")} value={fmt.relative(repo.last_attempted_at)} hint={fmt.dateTime(repo.last_attempted_at)} />
              <StatCard label={t("repo_detail.auth_status")} value={repo.auth_status || (repo.auth_healthy ? t("repo.auth_healthy") : t("repo.auth_issue"))} hint={fmt.dateTime(repo.last_auth_checked_at)} />
              <StatCard label={t("repo_detail.schedule")} value={decision ? schedulerDecisionLabel(t, decision.reason, decision.due) : scheduleModeLabel(t, subscription.schedule_mode)} hint={decision?.next_due_at ? fmt.dateTime(decision.next_due_at) : undefined} />
            </div>
            {repo.latest_job?.outcome && <SyncOutcomeNotice outcome={repo.latest_job.outcome} />}
            {repo.latest_job?.error_log_excerpt && ["failed", "stale"].includes(repo.latest_job.status) && (
              <div className="rounded-md border border-danger/30 bg-danger-subtle p-4 text-sm text-danger dark:border-danger/30 dark:bg-danger-subtle dark:text-danger">
                {repo.latest_job.error_log_excerpt}
              </div>
            )}
            <div className="grid grid-cols-1 gap-5 lg:grid-cols-2">
              <section>
                <div className="mb-3 flex items-center justify-between">
                  <h2 className="text-base font-semibold">{t("repo_detail.recent_jobs")}</h2>
                  <button onClick={() => setTab("history")} className="text-sm text-accent hover:underline dark:text-accent">{t("common.view_all")}</button>
                </div>
                <JobsList jobs={syncHistory.slice(0, 4)} />
              </section>
              <section>
                <div className="mb-3 flex items-center justify-between">
                  <h2 className="text-base font-semibold">{t("repo_detail.recent_works")}</h2>
                  <button onClick={() => setTab("content")} className="text-sm text-accent hover:underline dark:text-accent">{t("common.view_all")}</button>
                </div>
                <WorksGrid works={recent_works.slice(0, 4)} />
              </section>
            </div>
          </div>
        )}
        {tab === "history" && (
          <div className="space-y-3">
            <div className="flex items-center justify-between gap-3">
              <p className="text-sm text-muted">{t("repo_detail.jobs_filtered_desc")}</p>
              <Link href={`${adminRoutes.jobs}?view=attention&q=${encodeURIComponent(id)}`} className="btn-ghost text-sm">{t("repo_detail.open_in_jobs")}</Link>
            </div>
            <JobsList jobs={syncHistory} />
            <RepositoryGraph repositoryId={id} />
          </div>
        )}
        {tab === "content" && (
          <div className="space-y-4">
            <WorksGrid works={recent_works} />
            <h2 className="pt-3 text-base font-semibold">{t("repo_detail.tab_tags")}</h2>
            {repositoryTags.isLoading && (
              <div className="flex min-h-72 flex-wrap items-center justify-center gap-3">
                {Array.from({ length: 16 }).map((_, index) => {
                  const size = 44 + (index % 4) * 12;
                  return <div key={index} className="animate-pulse rounded-full bg-subtle" style={{ width: size, height: size }} />;
                })}
              </div>
            )}
            {repositoryTags.error && (
              <ErrorState message={(repositoryTags.error as Error).message} onRetry={() => repositoryTags.refetch()} />
            )}
            {repositoryTags.data?.items.length === 0 && (
              <EmptyState title={t("repo_detail.no_tags_title")} description={t("repo_detail.no_tags_desc")} />
            )}
            {!!repositoryTags.data?.items.length && (
              <>
                <div className="rounded-md border border-border bg-white p-3 dark:border-border dark:bg-surface sm:p-5">
                  <TagBubbleChart tags={repositoryTags.data.items} ariaLabel={t("repo_detail.tab_tags")} />
                </div>
                {repositoryTags.data.total > tagLimit && (
                  <div className="flex items-center justify-center gap-2">
                    <button
                      type="button"
                      className="btn-ghost text-sm"
                      disabled={tagPage === 0}
                      onClick={() => setTagPage((current) => Math.max(0, current - 1))}
                    >
                      {t("common.prev")}
                    </button>
                    <span className="px-2 text-sm text-muted">{t("common.page").replace("{page}", String(tagPage + 1))}</span>
                    <button
                      type="button"
                      className="btn-ghost text-sm"
                      disabled={(tagPage + 1) * tagLimit >= repositoryTags.data.total}
                      onClick={() => setTagPage((current) => current + 1)}
                    >
                      {t("common.next")}
                    </button>
                  </div>
                )}
              </>
            )}
          </div>
        )}
        {tab === "settings" && <ConfigRows detail={detail.data} decision={decision} />}
      </section>
    </PageShell>
  );
}

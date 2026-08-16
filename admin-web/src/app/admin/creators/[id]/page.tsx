"use client";

import { useEffect, useMemo, useState, type CSSProperties } from "react";
import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, CreatorLink as CreatorLinkType, CreatorRepository, queryKeys, SchedulerDecisionItem, WorkListItem } from "@/lib/api";
import { GitlleryPanel, HierarchyDeletionDialog, Modal, MotionNumber, PageShell, RepositoryCard, SourceBadge, StatusBadge, SmartSearchInput, WorkMediaThumbnail, type SlideItem } from "@/components";
import ActivityDotMatrix, { type ActivityDay } from "@/components/charts/ActivityDotMatrix";
import BallotTally from "@/components/charts/BallotTally";
import ChartFrame from "@/components/charts/ChartFrame";
import HairlineSeries from "@/components/charts/HairlineSeries";
import TickRows from "@/components/charts/TickRows";
import { niceUnit } from "@/components/charts/chartMath";
import type { ChartDatum, ChartSeriesPoint } from "@/components/charts/types";
import { useSlideshow } from "@/lib/useSlideshow";
import { POLL_IDLE_MS } from "@/lib/polling";
import { motionConfig } from "@/lib/motion";
import { Breadcrumb } from "@/components/Breadcrumb";
import { useToast } from "@/components/Toast";
import { useT } from "@/lib/i18n";
import { useI18nFormat } from "@/lib/i18n-format";
import { quoteSearchValue, searchUrl } from "@/lib/search-query";
import { adminRoutes } from "@/lib/adminRoutes";
import { usePermissions } from "@/lib/usePermissions";
import { useNotifications } from "@/components/NotificationCenter";

type TabKey = "overview" | "repositories" | "works" | "links";

function initials(name: string) {
  return name.trim().slice(0, 2).toUpperCase();
}

function runningRepoCount(repos: CreatorRepository[]) {
  return repos.filter((r) => r.latest_job && ["pending", "downloading", "downloaded", "importing"].includes(r.latest_job.status)).length;
}

function WorkPreviewCard({ work }: { work: WorkListItem }) {
  const t = useT();
  const fmt = useI18nFormat();
  const assetId = work.preview_asset_ids?.[0] || work.thumbnail_asset_id;
  return (
    <Link href={`/admin/works/${work.id}`} className="group overflow-hidden rounded-md border border-border bg-white transition-colors hover:border-accent/50 dark:border-border dark:bg-surface dark:hover:border-accent/50">
      <div className="aspect-[4/3] bg-subtle">
        {assetId ? (
          <WorkMediaThumbnail assetId={assetId} hasVideo={work.has_video} alt={work.title || t("creator_detail.untitled")} className="h-full w-full object-cover" />
        ) : (
          <div className="flex h-full items-center justify-center text-xs text-muted">{t("works.na")}</div>
        )}
      </div>
      <div className="p-3">
        <div className="truncate text-sm font-medium group-hover:text-accent dark:group-hover:text-accent">{work.title || t("creator_detail.untitled")}</div>
        <div className="mt-1 flex items-center gap-2 text-xs text-muted">
          {work.source && <SourceBadge source={work.source} />}
          <span>{work.posted_at ? fmt.date(work.posted_at) : t("works.no_date")}</span>
        </div>
      </div>
    </Link>
  );
}

function CreatorWorksExplorer({ creatorId, selectedTag, onTagChange }: {
  creatorId: string;
  selectedTag: string;
  onTagChange: (tag: string) => void;
}) {
  const t = useT();
  const fmt = useI18nFormat();
  const [page, setPage] = useState(0);
  const [search, setSearch] = useState(
    `creator:${creatorId}${selectedTag ? ` tag:${quoteSearchValue(selectedTag)}` : ""} sort:posted-desc`,
  );
  const limit = 12;

  useEffect(() => {
    setPage(0);
  }, [search]);

  useEffect(() => {
    let cancelled = false;
    api.assistSearch({
      before_cursor: search,
      scope: "works",
      composes: [
        { key: "creator", value: creatorId, operation: "set" },
        { key: "tag", value: selectedTag || null, operation: "set" },
        { key: "sort", value: "posted-desc", operation: "set" },
      ],
    }).then((result) => {
      if (!cancelled) setSearch(result.canonical_query || result.query);
    });
    return () => {
      cancelled = true;
    };
    // The existing free-text query is intentionally retained while contextual
    // creator/tag tokens are updated by the server composer.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [creatorId, selectedTag]);

  const filteredWorks = useQuery({
    queryKey: ["search", "works", "creator-detail", search, page],
    queryFn: async () => {
      const result = await api.search(search, page * limit, limit, "works");
      return result.groups.works || { total: 0, items: [] };
    },
  });

  const slideshow = useSlideshow();
  const slideItems: SlideItem[] = (filteredWorks.data?.items || [])
    .filter((w): w is WorkListItem & { thumbnail_asset_id: string } => !!w.thumbnail_asset_id)
    .map((w) => ({ assetId: w.thumbnail_asset_id, workId: w.id, title: w.title, creatorName: w.creator_name }));

  return (
    <div className="space-y-4">
      <div className="rounded-md border border-border bg-white p-4 dark:border-border dark:bg-surface">
        <div className="flex flex-col gap-3 md:flex-row md:items-center md:justify-between">
          <div>
            <h2 className="text-base font-semibold">{t("creator_detail.works_title")}</h2>
            <p className="mt-1 text-sm text-muted">{t("creator_detail.works_filter_hint")}</p>
          </div>
          <div className="flex flex-wrap gap-2">
            {slideItems.length > 0 && (
              <button type="button" onClick={() => slideshow.open(slideItems)} className="btn-ghost">
                {t("slideshow.open")}
              </button>
            )}
            <Link href={searchUrl("/admin/works", search)} className="btn-ghost">
              {t("creator_detail.open_full_gallery")}
            </Link>
          </div>
        </div>
        <div className="mt-4 flex flex-wrap items-center gap-2">
          <SmartSearchInput
            value={search}
            onChange={setSearch}
            scope="works"
            placeholder={t("works.search_title")}
            className="min-w-0 flex-1 md:max-w-xl"
          />
          {selectedTag && (
            <button
              type="button"
              onClick={() => onTagChange("")}
              className="inline-flex items-center gap-2 rounded-full border border-accent/30 bg-accent-subtle px-3 py-1.5 text-sm font-medium text-accent hover:border-accent/60 hover:bg-subtle dark:border-accent/40 dark:bg-accent-subtle dark:text-accent"
              title={t("creator_detail.clear_tag_filter", { tag: selectedTag })}
            >
              <span className="max-w-[14rem] truncate">#{selectedTag}</span>
              <span aria-hidden="true">&times;</span>
            </button>
          )}
        </div>
      </div>

      {filteredWorks.isLoading && (
        <div className="grid grid-cols-2 gap-3 md:grid-cols-3">
          {Array.from({ length: 6 }).map((_, i) => <div key={i} className="h-48 animate-pulse rounded-md bg-subtle dark:bg-subtle" />)}
        </div>
      )}
      {filteredWorks.error && <div className="card p-5 text-sm text-danger">{(filteredWorks.error as Error).message}</div>}
      {filteredWorks.data && filteredWorks.data.items.length === 0 && (
        <div className="card p-8 text-center text-sm text-muted">
          {selectedTag ? t("creator_detail.no_tagged_works", { tag: selectedTag }) : t("creator_detail.no_creator_works")}
        </div>
      )}
      {filteredWorks.data && filteredWorks.data.items.length > 0 && (
        <>
          <div className="grid grid-cols-2 gap-3 md:grid-cols-3">
            {filteredWorks.data.items.map((w) => <WorkPreviewCard key={w.id} work={w} />)}
          </div>
          <div className="flex items-center justify-center gap-2">
            <button disabled={page === 0} onClick={() => setPage((p) => Math.max(0, p - 1))} className="btn-ghost disabled:opacity-40">
              {t("works.prev")}
            </button>
            <span className="px-3 py-1 text-sm text-muted">
              {t("works.page", { page: page + 1 })}
            </span>
            <button disabled={(page + 1) * limit >= filteredWorks.data.total} onClick={() => setPage((p) => p + 1)} className="btn-ghost disabled:opacity-40">
              {t("works.next")}
            </button>
          </div>
        </>
      )}
      {slideshow.node}
    </div>
  );
}

export default function CreatorDetailPage() {
  const t = useT();
  const fmt = useI18nFormat();
  const toast = useToast();
  const params = useParams();
  const router = useRouter();
  const qc = useQueryClient();
  const { isAdmin, has } = usePermissions();
  const notify = useNotifications();
  const id = params.id as string;
  const [activeTab, setActiveTab] = useState<TabKey>("overview");
  const [showAddLink, setShowAddLink] = useState(false);
  const [editing, setEditing] = useState(false);
  const [worksTag, setWorksTag] = useState("");
  const [activityYear, setActivityYear] = useState<number | null>(null);
  const [editName, setEditName] = useState("");
  const [editDisplay, setEditDisplay] = useState("");
  const [editDesc, setEditDesc] = useState("");
  const [narrativeMotionEnabled, setNarrativeMotionEnabled] = useState(false);
  const [showDelete, setShowDelete] = useState(false);
  const [deleteFiles, setDeleteFiles] = useState(false);

  useEffect(() => {
    setNarrativeMotionEnabled(motionConfig.shouldAnimate());
  }, []);

  const creator = useQuery({ queryKey: queryKeys.creators.detail(id), queryFn: () => api.getCreator(id) });
  const links = useQuery({ queryKey: queryKeys.creators.links(id), queryFn: () => api.listCreatorLinks(id) });
  const stats = useQuery({ queryKey: ["creator-stats", id], queryFn: () => api.getCreatorStats(id), refetchInterval: POLL_IDLE_MS, staleTime: POLL_IDLE_MS });
  const availableActivityYears = useMemo(() => {
    if (!stats.data) return [];
    const years = new Set(
      stats.data.monthly_frequency
        .map((point) => Number(point.month.slice(0, 4)))
        .filter(Number.isFinite),
    );
    if (!years.size) years.add(new Date().getUTCFullYear());
    return [...years].sort((left, right) => left - right);
  }, [stats.data]);
  useEffect(() => {
    if (!availableActivityYears.length) return;
    setActivityYear((current) => (
      current !== null && availableActivityYears.includes(current)
        ? current
        : availableActivityYears[availableActivityYears.length - 1]
    ));
  }, [availableActivityYears]);
  const timeline = useQuery({
    queryKey: ["creator-timeline", id, activityYear],
    queryFn: () => api.getCreatorTimeline(
      id,
      `${activityYear}-01-01`,
      `${Number(activityYear) + 1}-01-01`,
    ),
    enabled: activityYear !== null,
    refetchInterval: POLL_IDLE_MS,
    staleTime: POLL_IDLE_MS,
  });
  const works = useQuery({
    queryKey: ["creator-latest-works", id],
    queryFn: async () => {
      const result = await api.search(`creator:${id} sort:posted-desc`, 0, 6, "works");
      return result.groups.works || { total: 0, items: [] };
    },
    refetchInterval: 15000,
  });
  const overview = useQuery({
    queryKey: ["creator-subscription-overview", id],
    queryFn: () => api.getCreatorSubscriptionOverview(id),
    refetchInterval: (query) => {
      const data = query.state.data;
      const running = data ? Math.max(data.summary.running_job_count, runningRepoCount(data.repositories)) : 0;
      return running > 0 ? 4000 : 12000;
    },
  });
  const schedulerDecisions = useQuery({
    queryKey: [...queryKeys.schedulerDecisions, id],
    queryFn: api.schedulerDecisions,
    refetchInterval: (query) => {
      const hasRunning = (overview.data?.summary.running_job_count || 0) > 0;
      const hasDue = query.state.data?.items.some((item) => item.creator_id === id && item.due);
      return hasRunning || hasDue ? 5000 : 15000;
    },
  });
  const curationHistory = useQuery({
    queryKey: queryKeys.curation.subject("creator", id),
    queryFn: () => api.listCurationCommits({ subject_type: "creator", subject_id: id, limit: 6 }),
  });
  const deletionPreview = useQuery({
    queryKey: ["deletion-preview", "creator", id],
    queryFn: () => api.getCreatorDeletionPreview(id),
    enabled: showDelete && has("curation"),
  });

  const openEdit = () => {
    if (!creator.data) return;
    setEditName(creator.data.name);
    setEditDisplay(creator.data.display_name || "");
    setEditDesc(creator.data.description || "");
    setEditing(true);
  };

  const update = useMutation({
    mutationFn: () => api.updateCreator(id, { name: editName, display_name: editDisplay || undefined, description: editDesc || undefined }),
    onSuccess: () => { qc.invalidateQueries({ queryKey: queryKeys.creators.detail(id) }); setEditing(false); toast.success(t("common.saved")); },
    onError: (e: Error) => toast.error(e.message),
  });

  const toggleFavorite = useMutation({
    mutationFn: () => api.toggleCreatorFavorite(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: queryKeys.creators.detail(id) }),
  });

  const curateCreator = useMutation({
    mutationFn: (action: "archive" | "restore") => api.curateCreator(id, action),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: queryKeys.creators.detail(id) });
      qc.invalidateQueries({ queryKey: queryKeys.works.all });
      qc.invalidateQueries({ queryKey: queryKeys.curation.all });
      toast.success(t("common.saved"));
    },
    onError: (e: Error) => toast.error(e.message),
  });

  const deleteCreator = useMutation({
    mutationFn: () => api.deleteCreator(id, deleteFiles),
    onSuccess: (result) => {
      if (result.task_id) {
        notify.startOperationJob(result.task_id, "hierarchy-delete", t("deletion.permanent_title"), {
          entity: "hierarchy-delete",
          entity_type: "creator",
          entity_ids: [id],
        });
        toast.success(t("deletion.queued"));
      } else {
        toast.success(t("deletion.soft_deleted"));
      }
      qc.invalidateQueries({ queryKey: queryKeys.creators.all });
      qc.invalidateQueries({ queryKey: queryKeys.subscriptions.all });
      qc.invalidateQueries({ queryKey: queryKeys.works.all });
      router.push(adminRoutes.creators);
    },
    onError: (error: Error) => toast.error(error.message),
  });

  const syncRepo = useMutation({
    mutationFn: (repo: CreatorRepository) => api.createDownloadJob({
      subscription_id: repo.subscription_id,
      subscription_source_id: repo.id,
      source: repo.source,
      source_url: repo.source_url || "",
    }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["creator-subscription-overview", id] });
      qc.invalidateQueries({ queryKey: queryKeys.downloadJobs.all });
      qc.invalidateQueries({ queryKey: queryKeys.schedulerDecisions });
      toast.success(t("creator_detail.sync_queued"));
    },
    onError: (e: Error) => toast.error(e.message),
  });

  const toggleRepo = useMutation({
    mutationFn: (repo: CreatorRepository) => api.updateSubscriptionSource(repo.subscription_id, repo.id, { is_enabled: !repo.is_enabled }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["creator-subscription-overview", id] });
      qc.invalidateQueries({ queryKey: queryKeys.schedulerDecisions });
    },
    onError: (e: Error) => toast.error(e.message),
  });

  const c = creator.data;
  const st = stats.data;
  const sourceChartData = useMemo<ChartDatum[]>(() => (
    [...(st?.source_breakdown || [])]
      .sort((left, right) => right.count - left.count || left.source.localeCompare(right.source))
      .map((item) => ({
        id: item.source,
        label: item.source,
        value: item.count,
        colorRole: `source:${item.source}`,
        description: t("charts.source_row_label", { source: item.source, count: item.count }),
      }))
  ), [st?.source_breakdown, t]);
  const sourceUnit = niceUnit(
    sourceChartData.reduce((maximum, item) => Math.max(maximum, item.value), 0),
  );
  const tagChartData = useMemo<ChartDatum[]>(() => (
    [...(st?.tag_distribution || [])]
      .sort((left, right) => right.count - left.count || left.tag.localeCompare(right.tag))
      .slice(0, 6)
      .map((item) => ({
        id: item.tag,
        label: item.tag,
        value: item.count,
        colorRole: "accent",
        href: searchUrl("/admin/works", `type:work creator:${id} tag:${quoteSearchValue(item.tag)} sort:posted-desc`),
        description: t("charts.tag_row_label", { tag: item.tag, count: item.count }),
      }))
  ), [id, st?.tag_distribution, t]);
  const monthlyChartData = useMemo<ChartSeriesPoint[]>(() => (
    [...(st?.monthly_frequency || [])]
      .sort((left, right) => left.month.localeCompare(right.month))
      .slice(-48)
      .map((item) => ({
        id: item.month,
        label: item.month,
        value: item.count,
        description: t("charts.month_row_label", { month: item.month, count: item.count }),
      }))
  ), [st?.monthly_frequency, t]);
  const sourceLeader = sourceChartData[0];
  const tagLeader = tagChartData[0];
  const monthlyPeak = monthlyChartData.reduce<ChartSeriesPoint | null>(
    (current, point) => current === null || point.value > current.value ? point : current,
    null,
  );
  const activityPeak = (timeline.data?.days || []).reduce<ActivityDay | null>(
    (current, day) => current === null || day.total > current.total ? day : current,
    null,
  );
  const repos = overview.data?.repositories || [];
  const legalRepos = repos.filter((r) => r.is_repository);
  const decisionBySource = useMemo(() => {
    const map = new Map<string, SchedulerDecisionItem>();
    for (const item of schedulerDecisions.data?.items || []) {
      if (item.creator_id === id) map.set(item.source_id, item);
    }
    return map;
  }, [schedulerDecisions.data?.items, id]);
  const repoSummary = useMemo(() => {
    const enabled = legalRepos.filter((r) => r.is_enabled).length;
    const running = runningRepoCount(repos);
    const failed = repos.filter((r) => r.latest_job && ["failed", "stale"].includes(r.latest_job.status)).length;
    const authIssues = repos.filter((r) => !r.auth_healthy).length;
    const lastSuccess = legalRepos
      .map((r) => r.last_synced_at)
      .filter(Boolean)
      .sort((a, b) => new Date(String(b)).getTime() - new Date(String(a)).getTime())[0] as string | undefined;
    return { enabled, running, failed, authIssues, lastSuccess };
  }, [legalRepos, repos]);
  const subscriptionHref = overview.data?.subscriptions[0]?.id
    ? `/admin/subscriptions/${overview.data.subscriptions[0].id}`
    : `/admin/subscriptions?q=${encodeURIComponent(c?.display_name || c?.name || "")}`;

  const creatorVisibility = c?.curation_state?.visibility || "visible";

  const tabs = useMemo(() => [
    { key: "overview" as const, label: t("creator_detail.tabs_overview"), count: undefined },
    { key: "repositories" as const, label: t("creator_detail.tabs_repositories"), count: overview.data?.summary.repository_count ?? legalRepos.length },
    { key: "works" as const, label: t("creator_detail.tabs_works"), count: st?.total_works },
    { key: "links" as const, label: t("creator_detail.tabs_links"), count: links.data?.length || 0 },
  ], [t, overview.data?.summary.repository_count, legalRepos.length, st?.total_works, links.data?.length]);

  if (creator.isLoading) {
    return <PageShell><div className="space-y-4">{Array.from({ length: 4 }).map((_, i) => <div key={i} className="h-28 animate-pulse rounded-md bg-subtle dark:bg-subtle" />)}</div></PageShell>;
  }
  if (creator.error) {
    return <PageShell><div className="card p-5 text-danger">{(creator.error as Error).message}</div></PageShell>;
  }
  if (!c) return null;

  return (
    <PageShell>
      <Breadcrumb items={[
        { label: t("creators.title"), href: "/admin/creators" },
        { label: c.display_name || c.name },
      ]} />
      <div className="mb-6 flex flex-col gap-4 border-b border-border pb-5 dark:border-border md:flex-row md:items-end md:justify-between">
        <div
          className={`${narrativeMotionEnabled ? "creator-narrative-item" : ""} flex min-w-0 items-center gap-4`}
          style={{ "--chart-delay": "0ms" } as CSSProperties}
        >
          <div className="flex h-20 w-20 shrink-0 items-center justify-center rounded-full border border-accent/30 bg-accent-subtle text-2xl font-semibold text-accent">
            {initials(c.display_name || c.name)}
          </div>
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <h1 className="truncate text-2xl font-semibold tracking-normal text-fg">{c.display_name || c.name}</h1>
              {c.is_favorite && <span className="rounded-full border border-warning/30 bg-warning-subtle px-2 py-0.5 text-xs text-warning dark:bg-warning-subtle dark:text-warning">{t("creator_detail.favorite")}</span>}
              {creatorVisibility !== "visible" && <span className="rounded-full border border-danger/25 bg-danger-subtle px-2 py-0.5 text-xs text-danger dark:bg-danger-subtle">{creatorVisibility}</span>}
            </div>
            {c.display_name && c.display_name !== c.name && <p className="mt-0.5 font-mono text-sm text-muted">{c.name}</p>}
            <div className="mt-2 flex flex-wrap items-center gap-3 text-sm text-muted">
              <StatusBadge status={c.is_active ? "up" : "down"} />
              {c.danbooru_artist_id && (
                <a href={`https://danbooru.donmai.us/artists/${c.danbooru_artist_id}`} target="_blank" rel="noopener noreferrer"
                  className="font-mono text-accent hover:underline dark:text-accent">
                  danbooru #{c.danbooru_artist_id}
                </a>
              )}
              <span>{t("creator_detail.repositories_count", { count: overview.data?.summary.repository_count ?? 0 })}</span>
              {overview.data?.summary.running_job_count ? <span className="text-accent">{t("creator_detail.running_count", { count: overview.data.summary.running_job_count })}</span> : null}
            </div>
          </div>
        </div>
        <div
          className={`${narrativeMotionEnabled ? "creator-narrative-item" : ""} flex flex-wrap gap-2`}
          style={{ "--chart-delay": "90ms" } as CSSProperties}
        >
          <button onClick={() => toggleFavorite.mutate()} className="btn-ghost">
            {c.is_favorite ? t("creator_detail.unstar") : t("creator_detail.star")}
          </button>
          {(isAdmin || creatorVisibility === "visible") && has("curation") ? (
            <button onClick={() => { setDeleteFiles(false); setShowDelete(true); }} className={isAdmin ? "btn-danger" : "btn-ghost"}>
              {isAdmin ? t("deletion.permanent_title") : t("creator_detail.archive")}
            </button>
          ) : (
            has("curation") ? <button onClick={() => curateCreator.mutate("restore")} disabled={curateCreator.isPending} className="btn-ghost">{t("creator_detail.restore")}</button> : null
          )}
          <Link href={subscriptionHref} className="btn-ghost">
            {t("creator_detail.subscription")}
          </Link>
          <button onClick={openEdit} className="btn-primary">{t("creator_detail.edit_profile")}</button>
        </div>
      </div>

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-[280px_1fr]">
        <aside className="space-y-4">
          <section
            className={`${narrativeMotionEnabled ? "creator-narrative-item" : ""} card p-4`}
            style={{ "--chart-delay": "140ms" } as CSSProperties}
          >
            <h2 className="mb-2 text-sm font-semibold">{t("creator_detail.profile")}</h2>
            {c.description ? (
              <p className="whitespace-pre-wrap text-sm leading-6 text-fg">{c.description}</p>
            ) : (
              <p className="text-sm text-muted">{t("creator_detail.no_description")}</p>
            )}
            <dl className="mt-4 space-y-2 border-t border-border pt-4 text-sm dark:border-border">
              <div className="flex justify-between gap-3">
                <dt className="text-muted">{t("creator_detail.stat_works")}</dt>
                <dd className="font-semibold">
                  {st?.total_works != null ? (
                    <MotionNumber value={st.total_works} animateInitial format={(value) => fmt.number(value)} />
                  ) : "-"}
                </dd>
              </div>
              <div className="flex justify-between gap-3">
                <dt className="text-muted">{t("creator_detail.stat_assets")}</dt>
                <dd className="font-semibold">
                  {st?.total_assets != null ? (
                    <MotionNumber value={st.total_assets} animateInitial format={(value) => fmt.number(value)} />
                  ) : "-"}
                </dd>
              </div>
              <div className="flex justify-between gap-3">
                <dt className="text-muted">{t("creator_detail.stat_sources")}</dt>
                <dd className="font-semibold">
                  {st?.source_breakdown ? (
                    <MotionNumber value={st.source_breakdown.length} animateInitial format={(value) => fmt.number(value)} />
                  ) : "-"}
                </dd>
              </div>
              <div className="flex justify-between gap-3"><dt className="text-muted">{t("creator_detail.created")}</dt><dd>{fmt.date(c.created_at)}</dd></div>
            </dl>
          </section>

          <section className="card p-4">
            <div className="mb-3 flex items-center justify-between">
              <h2 className="text-sm font-semibold">{t("curation.title")}</h2>
              <Link href={`${adminRoutes.curation}?subject_type=creator&subject_id=${id}`} className="text-sm text-accent hover:underline dark:text-accent">{t("common.open")}</Link>
            </div>
            {curationHistory.data?.items.length ? (
              <div className="space-y-3">
                {curationHistory.data.items.map((commit) => (
                  <div key={commit.id} className="border-l-2 border-accent pl-3 text-xs dark:border-accent">
                    <div className="font-medium text-fg">{commit.message}</div>
                    <div className="mt-0.5 text-muted">{commit.trigger} · {fmt.date(commit.occurred_at)}</div>
                  </div>
                ))}
              </div>
            ) : <p className="text-sm text-muted">{t("curation.empty_title")}</p>}
          </section>

          <GitlleryPanel creatorId={id} />

          <section className="card p-4">
            <div className="mb-3 flex items-center justify-between">
              <h2 className="text-sm font-semibold">{t("creator_detail.external_links")}</h2>
              <button onClick={() => setShowAddLink(true)} className="text-sm text-accent hover:underline dark:text-accent">{t("creator_detail.add")}</button>
            </div>
            {links.data?.length ? (
              <div className="space-y-2">
                {links.data.slice(0, 8).map((l: CreatorLinkType) => (
                  <a key={l.id} href={l.url} target="_blank" rel="noopener noreferrer"
                    className="flex min-w-0 items-center gap-2 rounded-md px-2 py-1 text-sm hover:bg-subtle dark:hover:bg-subtle">
                    <SourceBadge source={l.link_type} />
                    <span className="truncate text-accent">{l.url}</span>
                  </a>
                ))}
              </div>
            ) : <p className="text-sm text-muted">{t("creator_detail.no_links")}</p>}
          </section>

          {c.danbooru_artist_id && (
            <DanbooruAliases artistId={c.danbooru_artist_id} currentDisplay={c.display_name}
              onSelectAlias={(alias) => { setEditName(c.name); setEditDisplay(alias); setEditDesc(c.description || ""); setEditing(true); }} />
          )}
        </aside>

        <section className="min-w-0">
          <nav className="mb-4 grid grid-cols-2 gap-1 border-b border-border sm:flex sm:flex-wrap" aria-label={t("creator_detail.sections")}>
            {tabs.map((tab) => (
              <button key={tab.key} onClick={() => setActiveTab(tab.key)}
                className={`whitespace-nowrap border-b-2 px-3 py-2 text-sm ${activeTab === tab.key ? "border-danger font-semibold text-fg" : "border-transparent text-muted hover:text-fg dark:text-muted dark:hover:text-fg"}`}>
                {tab.label}{tab.count !== undefined && <span className="ml-2 rounded-full bg-subtle px-2 py-0.5 text-xs font-semibold text-fg">{tab.count}</span>}
              </button>
            ))}
          </nav>

          {activeTab === "overview" && (
            <div className="space-y-5">
              <ChartFrame
                title={t("creator_detail.works_timeline")}
                insight={activityYear && timeline.data
                  ? activityPeak
                    ? t("charts.activity_insight", {
                      year: activityYear,
                      date: fmt.date(`${activityPeak.date}T00:00:00Z`),
                      count: activityPeak.total,
                    })
                    : t("charts.activity_insight_empty", { year: activityYear })
                  : undefined}
                description={t("charts.activity_encoding")}
                actions={(
                  <Link href={searchUrl("/admin/works", `creator:${id} sort:posted-desc`)} className="btn-ghost">
                    {t("creator_detail.view_all_works")}
                  </Link>
                )}
                footer={t("charts.creator_activity_footer")}
                testId="creator-activity-chart"
              >
                {timeline.isPending || activityYear === null ? (
                  <div className="h-44 animate-pulse rounded-md bg-subtle" aria-label={t("common.loading")} />
                ) : timeline.error && !timeline.data ? (
                  <div className="rounded-md border border-danger/30 bg-danger-subtle p-4" role="alert">
                    <p className="font-semibold text-danger">{t("charts.activity_error")}</p>
                    <p className="mt-1 break-words text-xs text-muted">{(timeline.error as Error).message}</p>
                    <button type="button" className="btn-ghost mt-3" onClick={() => timeline.refetch()}>
                      {t("common.retry")}
                    </button>
                  </div>
                ) : timeline.data ? (
                  <>
                    {timeline.isRefetchError ? (
                      <div className="mb-3 rounded-md border border-warning/30 bg-warning-subtle px-3 py-2 text-xs text-warning" role="status">
                        {t("charts.activity_refresh_error")}
                      </div>
                    ) : null}
                    <ActivityDotMatrix
                      data={timeline.data}
                      year={activityYear}
                      availableYears={availableActivityYears}
                      onYearChange={setActivityYear}
                    />
                  </>
                ) : (
                  <div className="rounded-md border border-border bg-subtle p-4 text-sm text-muted">
                    {t("charts.activity_error")}
                  </div>
                )}
              </ChartFrame>

              <div className="grid grid-cols-1 items-start gap-5 xl:grid-cols-2">
                {sourceChartData.length ? (
                  <ChartFrame
                    title={t("creator_detail.source_breakdown")}
                    insight={sourceLeader
                      ? t("charts.source_insight", {
                        source: sourceLeader.label,
                        count: sourceLeader.value,
                      })
                      : undefined}
                    description={t("charts.source_encoding", { unit: sourceUnit })}
                    footer={t("charts.creator_stats_footer")}
                    testId="creator-source-chart"
                  >
                    <TickRows data={sourceChartData} />
                  </ChartFrame>
                ) : null}
                {tagChartData.length ? (
                  <ChartFrame
                    title={t("creator_detail.tag_distribution")}
                    insight={tagLeader
                      ? t("charts.tag_insight", {
                        tag: tagLeader.label,
                        count: tagLeader.value,
                        share: fmt.number(
                          st?.total_works ? (tagLeader.value / st.total_works) * 100 : 0,
                          { maximumFractionDigits: 1 },
                        ),
                      })
                      : undefined}
                    description={t("charts.tag_encoding")}
                    footer={t("charts.creator_stats_footer")}
                    testId="creator-tag-chart"
                  >
                    <BallotTally data={tagChartData} total={st?.total_works || 0} />
                  </ChartFrame>
                ) : null}
              </div>

              {monthlyChartData.length ? (
                <ChartFrame
                  title={t("creator_detail.posting_frequency")}
                  insight={monthlyPeak
                    ? t("charts.monthly_insight", {
                      month: monthlyPeak.label,
                      count: monthlyPeak.value,
                    })
                    : undefined}
                  description={t("charts.monthly_encoding")}
                  footer={t("charts.creator_stats_footer")}
                  testId="creator-monthly-chart"
                >
                  <HairlineSeries data={monthlyChartData} />
                </ChartFrame>
              ) : null}

              <section className="card p-4">
                <div className="mb-4 flex items-center justify-between">
                  <h2 className="text-base font-semibold">{t("creator_detail.latest_works")}</h2>
                  <Link href={searchUrl("/admin/works", `creator:${id} sort:posted-desc`)} className="text-sm text-accent hover:underline dark:text-accent">{t("creator_detail.open_works")}</Link>
                </div>
                {works.data?.items?.length ? (
                  <div className="grid grid-cols-2 gap-3 md:grid-cols-3">
                    {works.data.items.map((w) => <WorkPreviewCard key={w.id} work={w} />)}
                  </div>
                ) : <p className="text-sm text-muted">{t("creator_detail.no_imported_works")}</p>}
              </section>
            </div>
          )}

          {activeTab === "repositories" && (
            <div className="space-y-3">
              <div className="flex flex-wrap items-center justify-between gap-3 rounded-md border border-border bg-white p-3 dark:border-border dark:bg-surface">
                <div>
                  <h2 className="text-base font-semibold">{t("creator_detail.repositories_title")}</h2>
                  <p className="text-sm text-muted">{t("creator_detail.repositories_desc")}</p>
                </div>
                <Link href={subscriptionHref} className="btn-primary">{t("creator_detail.manage_subscription")}</Link>
              </div>
              <div className="grid grid-cols-2 gap-3 md:grid-cols-5">
                {[
                  [t("creator_detail.enabled_repos"), repoSummary.enabled, t("creator_detail.legal_repos", { count: legalRepos.length })],
                  [t("creator_detail.running_jobs"), repoSummary.running, t("creator_detail.active_queue")],
                  [t("creator_detail.failed_jobs"), repoSummary.failed, t("creator_detail.needs_review")],
                  [t("creator_detail.auth_issues"), repoSummary.authIssues, t("creator_detail.source_auth")],
                  [t("creator_detail.last_success"), repoSummary.lastSuccess ? fmt.dateTime(repoSummary.lastSuccess) : t("creator_detail.never"), t("creator_detail.source_sync")],
                ].map(([label, value, sub]) => (
                  <div key={label} className="card p-3">
                    <div className="truncate text-sm font-semibold text-fg">{value}</div>
                    <div className="mt-1 text-[11px] font-medium uppercase text-muted">{label}</div>
                    <div className="mt-0.5 text-xs text-placeholder dark:text-muted">{sub}</div>
                  </div>
                ))}
              </div>
              {repos.length ? repos.map((repo) => (
                <RepositoryCard key={repo.id} repo={repo} decision={decisionBySource.get(repo.id)} onSync={(r) => syncRepo.mutate(r as CreatorRepository)} onToggle={(r) => toggleRepo.mutate(r as CreatorRepository)}
                  syncPending={syncRepo.isPending} togglePending={toggleRepo.isPending} />
              )) : (
                <div className="card p-8 text-center text-sm text-muted">
                  {t("creator_detail.no_subscription_urls")}
                </div>
              )}
            </div>
          )}

          {activeTab === "works" && (
            <CreatorWorksExplorer creatorId={id} selectedTag={worksTag} onTagChange={setWorksTag} />
          )}

          {activeTab === "links" && (
            <div className="space-y-3">
              <div className="flex items-center justify-between rounded-md border border-border bg-white p-3 dark:border-border dark:bg-surface">
                <div>
                  <h2 className="text-base font-semibold">{t("creator_detail.external_links")}</h2>
                  <p className="text-sm text-muted">{t("creator_detail.links_desc")}</p>
                </div>
                <button onClick={() => setShowAddLink(true)} className="btn-primary">{t("creator_detail.add_link_short")}</button>
              </div>
              {links.data?.length ? links.data.map((l: CreatorLinkType) => (
                <div key={l.id} className="rounded-md border border-border bg-white p-4 dark:border-border dark:bg-surface">
                  <div className="flex items-center gap-2">
                    <SourceBadge source={l.link_type} />
                    <a href={l.url} target="_blank" rel="noopener noreferrer" className="truncate text-sm font-medium text-accent hover:underline dark:text-accent">{l.url}</a>
                  </div>
                  <div className="mt-2 text-xs text-muted">{t("creator_detail.confidence_state", { value: l.confidence.toFixed(1), state: l.is_verified ? t("creator_detail.verified") : t("creator_detail.unverified") })}</div>
                </div>
              )) : <div className="card p-8 text-center text-sm text-muted">{t("creator_detail.no_links")}</div>}
            </div>
          )}
        </section>
      </div>

      <Modal open={showAddLink} onClose={() => setShowAddLink(false)} title={t("creator_detail.add_link_modal")}>
        <AddLinkForm creatorId={id} onClose={() => setShowAddLink(false)} />
      </Modal>
      <Modal open={editing} onClose={() => setEditing(false)} title={t("creator_detail.edit_title")}>
        <div className="space-y-4">
          <div><label className="mb-1 block text-sm font-medium">{t("creator_detail.name_field")}</label><input value={editName} onChange={(e) => setEditName(e.target.value)} className="input w-full" /></div>
          <div><label className="mb-1 block text-sm font-medium">{t("creator_detail.display_name_field")}</label><input value={editDisplay} onChange={(e) => setEditDisplay(e.target.value)} className="input w-full" /></div>
          <div><label className="mb-1 block text-sm font-medium">{t("creator_detail.description_field")}</label><textarea value={editDesc} onChange={(e) => setEditDesc(e.target.value)} className="textarea w-full" rows={4} /></div>
          <div className="flex justify-end gap-3 pt-2">
            <button onClick={() => setEditing(false)} className="btn-ghost">{t("creator_detail.cancel")}</button>
            <button onClick={() => update.mutate()} disabled={update.isPending} className="btn-primary">{t("creator_detail.save")}</button>
          </div>
        </div>
      </Modal>
      <HierarchyDeletionDialog
        open={showDelete}
        title={isAdmin ? t("deletion.permanent_title") : t("deletion.soft_title")}
        confirmationPhrase={c.display_name || c.name}
        preview={deletionPreview.data}
        previewLoading={deletionPreview.isLoading}
        deleteFiles={deleteFiles}
        onDeleteFilesChange={setDeleteFiles}
        onConfirm={() => deleteCreator.mutate()}
        onCancel={() => { setShowDelete(false); setDeleteFiles(false); }}
        isPending={deleteCreator.isPending}
        error={(deleteCreator.error as Error)?.message || (deletionPreview.error as Error)?.message}
      />
    </PageShell>
  );
}

function DanbooruAliases({ artistId, currentDisplay, onSelectAlias }: {
  artistId: number; currentDisplay?: string; onSelectAlias: (alias: string) => void;
}) {
  const t = useT();
  const aliases = useQuery({
    queryKey: ["danbooru-artist", artistId],
    queryFn: () => api.getDanbooruArtist(artistId),
    staleTime: 10 * 60 * 1000,
  });

  if (aliases.isLoading) {
    return <div className="card p-4"><div className="h-12 animate-pulse rounded-md bg-subtle dark:bg-subtle" /></div>;
  }
  if (!aliases.data?.artist) {
    return (
      <div className="card p-4">
        <h3 className="mb-2 text-sm font-semibold">{t("creator_detail.danbooru_ref")}</h3>
        <p className="text-xs text-muted">Danbooru #{artistId}</p>
      </div>
    );
  }

  const artist = aliases.data.artist;
  const names = [
    ...(artist.pixiv_display_name ? [{ label: artist.pixiv_display_name, type: "pixiv" as const }] : []),
    ...(artist.other_names || []).map((n: string) => ({ label: n, type: "danbooru" as const })),
  ];
  if (!names.length) return null;

  return (
    <div className="card p-4">
      <h3 className="mb-2 text-sm font-semibold">{t("creator_detail.danbooru_ref")}</h3>
      <p className="mb-3 text-xs text-muted">{t("creator_detail.danbooru_aliases_hint")}</p>
      <div className="flex flex-wrap gap-1.5">
        {names.map(({ label, type }) => {
          const isActive = currentDisplay === label;
          return (
            <button key={label} type="button" onClick={() => onSelectAlias(label)}
              title={t("creator_detail.set_display_name_as", { name: label })}
              className={`rounded-full border px-2.5 py-1 text-xs transition-colors ${
                isActive
                  ? "border-accent bg-accent-subtle text-accent dark:border-accent dark:bg-accent-subtle dark:text-accent"
                  : type === "pixiv"
                    ? "border-blue-200 bg-blue-50 text-blue-700 hover:bg-blue-100 dark:border-blue-900 dark:bg-blue-950/30 dark:text-blue-300"
                    : "border-border bg-subtle text-muted hover:bg-subtle dark:border-border dark:bg-subtle dark:text-muted"
              }`}>
              {label}
            </button>
          );
        })}
      </div>
    </div>
  );
}

function AddLinkForm({ creatorId, onClose }: { creatorId: string; onClose: () => void }) {
  const t = useT();
  const [url, setUrl] = useState("");
  const [linkType, setLinkType] = useState("website");
  const toast = useToast();
  const qc = useQueryClient();
  const create = useMutation({
    mutationFn: () => api.createCreatorLink(creatorId, { url, link_type: linkType }),
    onSuccess: () => { qc.invalidateQueries({ queryKey: queryKeys.creators.links(creatorId) }); onClose(); toast.success(t("creator_detail.link_added")); },
    onError: (e: Error) => toast.error(e.message),
  });
  return (
    <div className="space-y-4">
      <div><label className="mb-1 block text-sm font-medium">{t("creator_detail.url_label")}</label><input value={url} onChange={(e) => setUrl(e.target.value)} className="input w-full" placeholder={t("creator_detail.url_placeholder")} /></div>
      <div><label className="mb-1 block text-sm font-medium">{t("creator_detail.type_label")}</label>
        <select value={linkType} onChange={(e) => setLinkType(e.target.value)} className="select w-full">
          {["website", "pixiv", "x", "iwara", "danbooru", "other"].map((o) => <option key={o} value={o}>{o}</option>)}
        </select>
      </div>
      <div className="flex justify-end gap-3 pt-2">
        <button onClick={onClose} className="btn-ghost">{t("common.cancel")}</button>
        <button onClick={() => create.mutate()} disabled={!url || create.isPending} className="btn-primary">{t("creator_detail.add")}</button>
      </div>
    </div>
  );
}

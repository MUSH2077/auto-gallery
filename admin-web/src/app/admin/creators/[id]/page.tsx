"use client";

import { useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { useParams } from "next/navigation";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, CreatorLink as CreatorLinkType, CreatorRepository, queryKeys, SchedulerDecisionItem, WorkListItem } from "@/lib/api";
import { GitlleryPanel, Modal, RepositoryCard, SourceBadge, StatusBadge, WorkGrid } from "@/components";
import { POLL_IDLE_MS } from "@/lib/polling";
import { Breadcrumb } from "@/components/Breadcrumb";
import { useToast } from "@/components/Toast";
import { useT } from "@/lib/i18n";
import { useI18nFormat } from "@/lib/i18n-format";
import { CHART_COLORS, SOURCE_COLORS } from "@/lib/sourceColors";

type TabKey = "overview" | "repositories" | "works" | "links";

function initials(name: string) {
  return name.trim().slice(0, 2).toUpperCase();
}

function runningRepoCount(repos: CreatorRepository[]) {
  return repos.filter((r) => r.latest_job && ["pending", "downloading", "downloaded", "importing"].includes(r.latest_job.status)).length;
}

function HorizontalBarChart({ data, maxKey, labelKey, colorFn, onItemClick }: {
  data: { [k: string]: any }[];
  maxKey: string;
  labelKey: string;
  colorFn: (item: any, i: number) => string;
  onItemClick?: (item: any) => void;
}) {
  const fmt = useI18nFormat();
  const max = data.length > 0 ? Math.max(...data.map((d) => d[maxKey] as number)) : 1;
  return (
    <div className="space-y-2">
      {data.map((item, i) => (
        <button
          key={`${item[labelKey]}-${i}`}
          type="button"
          onClick={() => onItemClick?.(item)}
          disabled={!onItemClick}
          className={`flex w-full items-center gap-2 text-xs ${onItemClick ? "rounded-md px-1 py-0.5 text-left hover:bg-subtle dark:hover:bg-subtle" : ""}`}
        >
          <span className="w-24 truncate text-right text-muted">{String(item[labelKey])}</span>
          <div className="h-2 flex-1 overflow-hidden rounded-full bg-subtle dark:bg-border">
            <div className="h-full rounded-full" style={{ width: `${((item[maxKey] as number) / max) * 100}%`, backgroundColor: colorFn(item, i) }} />
          </div>
          <span className="w-10 shrink-0 text-right font-mono text-muted">{fmt.number(item[maxKey] as number)}</span>
        </button>
      ))}
    </div>
  );
}

function MonthStrip({ data }: { data: { month: string; count: number }[] }) {
  if (!data.length) return null;
  const max = Math.max(...data.map((d) => d.count), 1);
  return (
    <div className="flex h-12 items-end gap-[2px]">
      {data.slice(-48).map((d) => (
        <div key={d.month}
          className="min-w-[3px] flex-1 rounded-t-sm bg-accent transition-opacity hover:opacity-75"
          style={{ height: `${Math.max(2, (d.count / max) * 100)}%`, opacity: 0.3 + (d.count / max) * 0.7 }}
          title={`${d.month}: ${d.count} works`} />
      ))}
    </div>
  );
}

function WorkPreviewCard({ work }: { work: WorkListItem }) {
  const t = useT();
  const fmt = useI18nFormat();
  const assetId = work.preview_asset_ids?.[0] || work.thumbnail_asset_id;
  return (
    <Link href={`/admin/works/${work.id}`} className="group overflow-hidden rounded-md border border-border bg-white transition-colors hover:border-accent/50 dark:border-border dark:bg-surface dark:hover:border-accent/50">
      <div className="aspect-[4/3] bg-subtle">
        {assetId ? (
          <img src={api.mediaUrl(assetId, "thumb")} alt={work.title || t("creator_detail.untitled")} className="h-full w-full object-cover" loading="lazy" decoding="async" />
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
  const [search, setSearch] = useState("");
  const limit = 12;

  useEffect(() => {
    setPage(0);
  }, [creatorId, selectedTag, search]);

  const filteredWorks = useQuery({
    queryKey: ["creator-works-tab", creatorId, selectedTag, search, page],
    queryFn: () => api.listWorks(page * limit, limit, {
      creator_id: creatorId,
      tag: selectedTag || undefined,
      search: search || undefined,
      sort_by: "posted_at",
      sort_order: "desc",
    }),
  });

  return (
    <div className="space-y-4">
      <div className="rounded-md border border-border bg-white p-4 dark:border-border dark:bg-surface">
        <div className="flex flex-col gap-3 md:flex-row md:items-center md:justify-between">
          <div>
            <h2 className="text-base font-semibold">{t("creator_detail.works_title")}</h2>
            <p className="mt-1 text-sm text-muted">{t("creator_detail.works_filter_hint")}</p>
          </div>
          <Link href={`/admin/works?creator=${creatorId}${selectedTag ? `&tag=${encodeURIComponent(selectedTag)}` : ""}`} className="btn-ghost">
            {t("creator_detail.open_full_gallery")}
          </Link>
        </div>
        <div className="mt-4 flex flex-wrap items-center gap-2">
          <input
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder={t("works.search_title")}
            className="input min-w-0 flex-1 md:max-w-xs"
          />
          {selectedTag && (
            <button
              type="button"
              onClick={() => onTagChange("")}
              className="inline-flex items-center gap-2 rounded-full border border-accent/30 bg-accent-subtle px-3 py-1.5 text-sm font-medium text-accent hover:bg-[#b6e3ff] dark:border-accent/40 dark:bg-accent-subtle dark:text-accent"
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
    </div>
  );
}

export default function CreatorDetailPage() {
  const t = useT();
  const fmt = useI18nFormat();
  const toast = useToast();
  const params = useParams();
  const qc = useQueryClient();
  const id = params.id as string;
  const [activeTab, setActiveTab] = useState<TabKey>("overview");
  const [showAddLink, setShowAddLink] = useState(false);
  const [editing, setEditing] = useState(false);
  const [worksTag, setWorksTag] = useState("");
  const [editName, setEditName] = useState("");
  const [editDisplay, setEditDisplay] = useState("");
  const [editDesc, setEditDesc] = useState("");

  const creator = useQuery({ queryKey: queryKeys.creators.detail(id), queryFn: () => api.getCreator(id) });
  const links = useQuery({ queryKey: queryKeys.creators.links(id), queryFn: () => api.listCreatorLinks(id) });
  const timeline = useQuery({ queryKey: ["creator-timeline", id], queryFn: () => api.getCreatorTimeline(id), refetchInterval: POLL_IDLE_MS, staleTime: POLL_IDLE_MS });
  const stats = useQuery({ queryKey: ["creator-stats", id], queryFn: () => api.getCreatorStats(id), refetchInterval: POLL_IDLE_MS, staleTime: POLL_IDLE_MS });
  const works = useQuery({
    queryKey: ["creator-latest-works", id],
    queryFn: () => api.listWorks(0, 6, { creator_id: id, sort_by: "posted_at", sort_order: "desc" }),
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

  const openWorksTag = (tag: string) => {
    setWorksTag(tag);
    setActiveTab("works");
  };
  const creatorVisibility = c?.curation_state?.visibility || "visible";

  const tabs = useMemo(() => [
    { key: "overview" as const, label: t("creator_detail.tabs_overview"), count: undefined },
    { key: "repositories" as const, label: t("creator_detail.tabs_repositories"), count: overview.data?.summary.repository_count ?? legalRepos.length },
    { key: "works" as const, label: t("creator_detail.tabs_works"), count: st?.total_works },
    { key: "links" as const, label: t("creator_detail.tabs_links"), count: links.data?.length || 0 },
  ], [t, overview.data?.summary.repository_count, legalRepos.length, st?.total_works, links.data?.length]);

  if (creator.isLoading) {
    return <main className="mx-auto max-w-7xl p-6"><div className="space-y-4">{Array.from({ length: 4 }).map((_, i) => <div key={i} className="h-28 animate-pulse rounded-md bg-subtle dark:bg-subtle" />)}</div></main>;
  }
  if (creator.error) {
    return <main className="mx-auto max-w-7xl p-6"><div className="card p-5 text-danger">{(creator.error as Error).message}</div></main>;
  }
  if (!c) return null;

  return (
    <main className="page-transition mx-auto max-w-7xl px-4 py-6 sm:px-6 lg:px-8">
      <Breadcrumb items={[
        { label: t("creators.title"), href: "/admin/creators" },
        { label: c.display_name || c.name },
      ]} />
      <div className="mb-6 flex flex-col gap-4 border-b border-border pb-5 dark:border-border md:flex-row md:items-end md:justify-between">
        <div className="flex min-w-0 items-center gap-4">
          <div className="flex h-20 w-20 shrink-0 items-center justify-center rounded-full border border-border bg-gradient-to-br from-accent to-[#8250df] text-2xl font-semibold text-white dark:border-border">
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
        <div className="flex flex-wrap gap-2">
          <button onClick={() => toggleFavorite.mutate()} className="btn-ghost">
            {c.is_favorite ? t("creator_detail.unstar") : t("creator_detail.star")}
          </button>
          {creatorVisibility === "visible" ? (
            <button onClick={() => curateCreator.mutate("archive")} disabled={curateCreator.isPending} className="btn-ghost">Archive</button>
          ) : (
            <button onClick={() => curateCreator.mutate("restore")} disabled={curateCreator.isPending} className="btn-ghost">Restore</button>
          )}
          <Link href={subscriptionHref} className="btn-ghost">
            {t("creator_detail.subscription")}
          </Link>
          <button onClick={openEdit} className="btn-primary">{t("creator_detail.edit_profile")}</button>
        </div>
      </div>

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-[280px_1fr]">
        <aside className="space-y-4">
          <section className="card p-4">
            <h2 className="mb-2 text-sm font-semibold">{t("creator_detail.profile")}</h2>
            {c.description ? (
              <p className="whitespace-pre-wrap text-sm leading-6 text-fg">{c.description}</p>
            ) : (
              <p className="text-sm text-muted">{t("creator_detail.no_description")}</p>
            )}
            <dl className="mt-4 space-y-2 border-t border-border pt-4 text-sm dark:border-border">
              <div className="flex justify-between gap-3"><dt className="text-muted">{t("creator_detail.stat_works")}</dt><dd className="font-semibold">{st?.total_works != null ? fmt.number(st.total_works) : "-"}</dd></div>
              <div className="flex justify-between gap-3"><dt className="text-muted">{t("creator_detail.stat_assets")}</dt><dd className="font-semibold">{st?.total_assets != null ? fmt.number(st.total_assets) : "-"}</dd></div>
              <div className="flex justify-between gap-3"><dt className="text-muted">{t("creator_detail.stat_sources")}</dt><dd className="font-semibold">{st?.source_breakdown?.length ?? "-"}</dd></div>
              <div className="flex justify-between gap-3"><dt className="text-muted">{t("creator_detail.created")}</dt><dd>{fmt.date(c.created_at)}</dd></div>
            </dl>
          </section>

          <section className="card p-4">
            <div className="mb-3 flex items-center justify-between">
              <h2 className="text-sm font-semibold">Curation</h2>
              <Link href={`/admin/curation?subject_type=creator&subject_id=${id}`} className="text-sm text-accent hover:underline dark:text-accent">Open</Link>
            </div>
            {curationHistory.data?.items.length ? (
              <div className="space-y-3">
                {curationHistory.data.items.map((commit) => (
                  <div key={commit.id} className="border-l-2 border-accent pl-3 text-xs dark:border-accent">
                    <div className="font-medium text-fg">{commit.message}</div>
                    <div className="mt-0.5 text-muted">{commit.trigger} · {new Date(commit.occurred_at).toLocaleDateString()}</div>
                  </div>
                ))}
              </div>
            ) : <p className="text-sm text-muted">No curation commits yet.</p>}
          </section>

          <GitlleryPanel />

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
          <nav className="mb-4 flex gap-1 overflow-x-auto border-b border-border" aria-label="Creator sections">
            {tabs.map((tab) => (
              <button key={tab.key} onClick={() => setActiveTab(tab.key)}
                className={`whitespace-nowrap border-b-2 px-3 py-2 text-sm ${activeTab === tab.key ? "border-danger font-semibold text-fg" : "border-transparent text-muted hover:text-fg dark:text-muted dark:hover:text-fg"}`}>
                {tab.label}{tab.count !== undefined && <span className="ml-2 rounded-full bg-subtle px-2 py-0.5 text-xs font-medium text-muted dark:bg-border dark:text-muted">{tab.count}</span>}
              </button>
            ))}
          </nav>

          {activeTab === "overview" && (
            <div className="space-y-5">
              <section className="card p-4">
                <div className="mb-4 flex items-center justify-between gap-3">
                  <h2 className="text-base font-semibold">{t("creator_detail.works_timeline")}</h2>
                  <Link href={`/admin/works?creator=${id}`} className="text-sm text-accent hover:underline dark:text-accent">{t("creator_detail.view_all_works")}</Link>
                </div>
                <WorkGrid data={timeline.data} loading={timeline.isLoading} />
              </section>

              <div className="grid grid-cols-1 gap-5 xl:grid-cols-2">
                {st?.source_breakdown?.length ? (
                  <section className="card p-4">
                    <h2 className="mb-4 text-base font-semibold">{t("creator_detail.source_breakdown")}</h2>
                    <HorizontalBarChart data={st.source_breakdown} maxKey="count" labelKey="source" colorFn={(d) => SOURCE_COLORS[d.source as string] || CHART_COLORS[0]} />
                  </section>
                ) : null}
                {st?.tag_distribution?.length ? (
                  <section className="card p-4">
                    <h2 className="mb-4 text-base font-semibold">{t("creator_detail.tag_distribution")}</h2>
                    <HorizontalBarChart
                      data={st.tag_distribution.slice(0, 10)}
                      maxKey="count"
                      labelKey="tag"
                      colorFn={(_, i) => CHART_COLORS[i % CHART_COLORS.length]}
                      onItemClick={(item) => openWorksTag(String(item.tag))}
                    />
                    <div className="mt-4 flex flex-wrap gap-2">
                      {st.tag_distribution.slice(0, 12).map((item) => (
                        <button
                          key={item.tag}
                          type="button"
                          onClick={() => openWorksTag(item.tag)}
                          className="rounded-full border border-border bg-subtle px-2.5 py-1 text-xs font-medium text-accent hover:border-accent/40 hover:bg-accent-subtle dark:border-border dark:bg-subtle dark:text-accent dark:hover:bg-accent-subtle"
                          title={t("creator_detail.search_tag_title", { tag: item.tag })}
                        >
                          #{item.tag}
                        </button>
                      ))}
                    </div>
                  </section>
                ) : null}
              </div>

              {st?.monthly_frequency?.length ? (
                <section className="card p-4">
                  <div className="mb-3 flex items-center justify-between">
                    <h2 className="text-base font-semibold">{t("creator_detail.posting_frequency")}</h2>
                    <span className="text-xs text-muted">{t("creator_detail.months_count", { count: st.monthly_frequency.length })}</span>
                  </div>
                  <MonthStrip data={st.monthly_frequency} />
                </section>
              ) : null}

              <section className="card p-4">
                <div className="mb-4 flex items-center justify-between">
                  <h2 className="text-base font-semibold">{t("creator_detail.latest_works")}</h2>
                  <Link href={`/admin/works?creator=${id}`} className="text-sm text-accent hover:underline dark:text-accent">{t("creator_detail.open_works")}</Link>
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
    </main>
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
      <div><label className="mb-1 block text-sm font-medium">{t("creator_detail.url_label")}</label><input value={url} onChange={(e) => setUrl(e.target.value)} className="input w-full" placeholder="https://..." /></div>
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

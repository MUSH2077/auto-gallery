"use client";

import { Suspense, useDeferredValue, useEffect, useMemo, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";

import { api, type SearchQualifierToken, type SearchTarget } from "@/lib/api";
import {
  EmptyState,
  ErrorState,
  MatchedIdentityBadge,
  SmartSearchInput,
  WorkMediaThumbnail,
  useSearchComposer,
} from "@/components";
import { useT } from "@/lib/i18n";
import { useI18nFormat } from "@/lib/i18n-format";
import { adminRoutes } from "@/lib/adminRoutes";
import { useToast } from "@/components/Toast";
import { quoteSearchValue, searchUrl } from "@/lib/search-query";
import { usePermissions } from "@/lib/usePermissions";
import { authHealthPresentation } from "@/lib/auth-health";
import { BusinessConfirm, BusinessPage, BusinessPermissionGuard } from "@/features/gallery-ui/BusinessPage";
import styles from "./search.module.css";

type SearchTab = "all" | "work" | "creator" | "tag" | "repo" | "subscription";

const TAB_TARGET: Record<Exclude<SearchTab, "all">, SearchTarget> = {
  work: "works",
  creator: "creators",
  tag: "tags",
  repo: "repositories",
  subscription: "subscriptions",
};

function SearchContent() {
  const t = useT();
  const fmt = useI18nFormat();
  const toast = useToast();
  const { has } = usePermissions();
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const initialQuery = searchParams.get("q") || "";
  const [query, setQuery] = useState(initialQuery);
  const [page, setPage] = useState(Math.max(0, Number(searchParams.get("page") || 1) - 1));
  const pushNextComposedQuery = useRef(false);
  const urlSyncTimer = useRef<number | null>(null);
  const latestQuery = useRef(initialQuery);
  const deferredQuery = useDeferredValue(query);
  const [reindexOpen, setReindexOpen] = useState(false);
  const [reindexTask,setReindexTask] = useState<string|null>(null);
  const [reindexPending, setReindexPending] = useState(false);
  const [reindexError, setReindexError] = useState<string | null>(null);
  const reindexPendingRef = useRef(false);
  const reindexRequestRef = useRef(0);

  useEffect(() => {
    const nextQuery = searchParams.get("q") || "";
    latestQuery.current = nextQuery;
    setQuery(nextQuery);
    setPage(Math.max(0, Number(searchParams.get("page") || 1) - 1));
  }, [searchParams]);

  useEffect(() => {
    const timer = window.setTimeout(() => {
      if (deferredQuery.trim() !== latestQuery.current.trim()) return;
      const next = new URLSearchParams();
      if (deferredQuery.trim()) next.set("q", deferredQuery.trim());
      if (page > 0) next.set("page", String(page + 1));
      router.replace(next.size ? `${pathname}?${next.toString()}` : pathname, { scroll: false });
    }, 250);
    urlSyncTimer.current = timer;
    return () => {
      window.clearTimeout(timer);
      if (urlSyncTimer.current === timer) urlSyncTimer.current = null;
    };
  }, [deferredQuery, page, pathname, router]);

  const cancelPendingUrlSync = () => {
    if (urlSyncTimer.current !== null) {
      window.clearTimeout(urlSyncTimer.current);
      urlSyncTimer.current = null;
    }
  };

  const results = useQuery({
    queryKey: ["compound-search", deferredQuery, page],
    queryFn: () => api.search(deferredQuery, page * 50, 50, "global"),
    enabled: deferredQuery.trim().length > 0,
    placeholderData: (previous) => previous,
  });

  const selectedType = results.data?.parsed.tokens.find(
    (token): token is SearchQualifierToken => token.kind === "qualifier" && token.key === "type",
  )?.value as SearchTab | undefined;
  const activeTab: SearchTab = selectedType || "all";
  const composer = useSearchComposer({
    value: query,
    scope: "global",
    onChange: (value) => {
      cancelPendingUrlSync();
      latestQuery.current = value;
      setQuery(value);
      setPage(0);
      if (pushNextComposedQuery.current) {
        pushNextComposedQuery.current = false;
        router.push(searchUrl(pathname, value), { scroll: false });
      }
    },
  });

  const groups = results.data?.groups;
  const works = groups?.works?.items || [];
  const creators = groups?.creators?.items || [];
  const tags = groups?.tags?.items || [];
  const repositories = groups?.repositories?.items || [];
  const subscriptions = groups?.subscriptions?.items || [];
  const total = Object.values(groups || {}).reduce((sum, group) => sum + (group?.total || 0), 0);
  const activeTotal = activeTab === "all" ? total : groups?.[TAB_TARGET[activeTab]]?.total || 0;


  const tabs = useMemo(() => ([
    { key: "all" as const, label: t("search.tab_all"), count: total },
    { key: "work" as const, label: t("search.tab_works"), count: groups?.works?.total || 0 },
    { key: "creator" as const, label: t("search.tab_creators"), count: groups?.creators?.total || 0 },
    { key: "tag" as const, label: t("search.tab_tags"), count: groups?.tags?.total || 0 },
    { key: "repo" as const, label: t("search.tab_repositories"), count: groups?.repositories?.total || 0 },
    { key: "subscription" as const, label: t("search.tab_subscriptions"), count: groups?.subscriptions?.total || 0 },
  ]), [groups, t, total]);

  const setTab = (tab: SearchTab) => {
    pushNextComposedQuery.current = true;
    composer.mutate(
      {
        key: "type",
        value: tab === "all" ? null : tab,
        operation: tab === "all" ? "remove" : "set",
      },
      { onError: () => { pushNextComposedQuery.current = false; } },
    );
  };

  const startReindex = async () => {
    if (reindexPendingRef.current) return;
    const requestId = ++reindexRequestRef.current;
    reindexPendingRef.current = true;
    setReindexPending(true);
    setReindexError(null);
    try {
      const result = await api.reindexSearch();
      if (reindexRequestRef.current !== requestId) return;
      setReindexTask(result.task_id);
      toast.info(result.message || result.status);
      setReindexOpen(false);
    } catch (error) {
      if (reindexRequestRef.current !== requestId) return;
      const message = error instanceof Error ? error.message : String(error);
      setReindexError(message);
      toast.error(message);
    } finally {
      if (reindexRequestRef.current === requestId) {
        reindexPendingRef.current = false;
        setReindexPending(false);
      }
    }
  };

  return (
      <BusinessPage
        title={t("search.title")}
        description={t("search.desc")}
        className={styles.page}

      >
        <BusinessConfirm
          open={reindexOpen}
          title={t("settings.reindex_confirm_title")}
          message={t("settings.reindex_confirm_msg")}
          onConfirm={startReindex}
          onCancel={() => {
            reindexRequestRef.current += 1;
            reindexPendingRef.current = false;
            setReindexOpen(false);
            setReindexPending(false);
            setReindexError(null);
          }}
          isPending={reindexPending}
          error={reindexError}
        />

        <SmartSearchInput
          value={query}
          onEditStart={composer.discardPendingResult}
          onChange={(value) => {
            cancelPendingUrlSync();
            latestQuery.current = value;
            setQuery(value);
            setPage(0);
          }}
          scope="global"
          parseWhenUnfocused
          showHelp
          className={styles.searchInput}
        />

        {!deferredQuery.trim() && (
          <EmptyState title={t("search.empty")} description={t("search.empty_desc")} />
        )}
        {results.isLoading && deferredQuery.trim() && (
          <div className={styles.resultList}>
            {Array.from({ length: 5 }).map((_, index) => (
              <div key={index} className={styles.loadingRow} />
            ))}
          </div>
        )}
        {results.isError && deferredQuery.trim() && (
          <ErrorState message={(results.error as Error).message} onRetry={() => results.refetch()} />
        )}

        {/* TanStack Query keeps the last complete index result visible when a
            later full-text request fails; the error above becomes a sync
            warning instead of replacing the user's working result set. */}
        {results.data && deferredQuery.trim() && (
          <>
            <div className={styles.tabsWrap} aria-label={t("search.title")}>
              <div className={styles.tabs} role="tablist">
                {tabs.map((tab) => (
                  <button
                    key={tab.key}
                    type="button"
                    role="tab"
                    aria-selected={activeTab === tab.key}
                    onClick={() => setTab(tab.key)}
                    disabled={composer.isPending}
                    className={styles.tab + (activeTab === tab.key ? " " + styles.tabActive : "")}
                  >
                    {tab.label}
                    <span className={styles.tabCount}>{tab.count}</span>
                  </button>
                ))}
              </div>
            </div>

            {creators.length > 0 && (
              <section className={styles.section} aria-labelledby="search-creators-heading">
                <h2 id="search-creators-heading" className={styles.sectionHeading}>{t("search.creators_section")}</h2>
                <div className={styles.resultList}>
                  {creators.map((creator) => {
                    return (
                      <Link
                        key={creator.id}
                        href={`/admin/creators/${creator.id}`}
                        className="search-result-row"
                      >
                        <span className={styles.creatorAvatar}>
                          {(creator.display_name || creator.name).trim().slice(0, 2).toUpperCase()}
                        </span>
                        <span className={styles.resultMain}>
                          <span className={styles.resultTitle}>{creator.display_name || creator.name}</span>
                          {creator.description && <span className={styles.resultDescription}>{creator.description}</span>}
                          <span className={styles.resultIdentity}><MatchedIdentityBadge identity={creator.matched_identity} /></span>
                        </span>
                      </Link>
                    );
                  })}
                </div>
              </section>
            )}

            {works.length > 0 && (
              <section className={styles.section} aria-labelledby="search-works-heading">
                <h2 id="search-works-heading" className={styles.sectionHeading}>{t("search.works_section")}</h2>
                <div className={styles.resultList}>
                  {works.map((work) => {
                    return (
                      <article key={work.id} className="search-result-row search-work-row">
                        <Link
                          href={`/admin/works/${work.id}`}
                          className="search-row-link"
                          aria-label={t("search.open_work", { title: work.title || t("search.untitled") })}
                        />
                        {work.thumbnail_asset_id ? (
                          <span className="search-work-thumb media-motion-visual">
                            <WorkMediaThumbnail
                              assetId={work.thumbnail_asset_id}
                              hasVideo={work.has_video}
                              alt=""
                              className="h-full w-full object-cover"
                            />
                          </span>
                        ) : (
                          <span className="search-work-thumb search-work-thumb-empty">
                            {t("search.na")}
                          </span>
                        )}
                        <span className={styles.resultMain}>
                          <span className={styles.resultTitleLine}>
                            <span className={styles.resultTitle}>{work.title || t("search.untitled")}</span>
                            {work.is_nsfw && <span className="search-nsfw-badge">{t("search.nsfw")}</span>}
                          </span>
                          <span className={styles.resultMeta}>
                            {work.source && (
                              <Link className={styles.source} href={searchUrl("/admin/search", `type:work source:${work.source}`)} onClick={event=>event.stopPropagation()}>{work.source}</Link>
                            )}
                            {work.creator_name && work.creator_id && (
                              <Link href={`/admin/creators/${work.creator_id}`} className="text-accent hover:underline">
                                {work.creator_name}
                              </Link>
                            )}
                            {work.posted_at && <span>{fmt.date(work.posted_at)}</span>}
                            <MatchedIdentityBadge identity={work.matched_identity} />
                          </span>
                          {work.tags && work.tags.length > 0 && (
                            <span className="search-tag-list search-tag-list-inline">
                              {work.tags.slice(0, 8).map((tag) => (
                                <button
                                  key={tag}
                                  type="button"
                                  onClick={() => router.push(searchUrl("/admin/search", `type:work tag:${quoteSearchValue(tag)}`))}
                                  className="search-tag-button"
                                >
                                  {tag}
                                </button>
                              ))}
                            </span>
                          )}
                        </span>
                      </article>
                    );
                  })}
                </div>
              </section>
            )}

            {tags.length > 0 && (
              <section className={styles.section} aria-labelledby="search-tags-heading">
                <h2 id="search-tags-heading" className={styles.sectionHeading}>{t("search.tags_section")}</h2>
                <div className="search-tag-list">
                  {tags.map((tag) => {
                    return (
                      <Link
                        key={tag.id}
                        href={`/admin/tags/${tag.id}`}
                        className="search-tag-link"
                      >
                        #{tag.normalized_name}
                        {typeof tag.usage_count === "number" && <span className="ml-1.5 text-xs text-muted">{tag.usage_count}</span>}
                      </Link>
                    );
                  })}
                </div>
              </section>
            )}

            {repositories.length > 0 && (
              <section className={styles.section} aria-labelledby="search-repositories-heading">
                <h2 id="search-repositories-heading" className={styles.sectionHeading}>{t("search.repositories_section")}</h2>
                <div className={styles.resultList}>
                  {repositories.map((repository) => {
                    return (
                      <Link
                        key={repository.id}
                        href={adminRoutes.repository(repository.id)}
                        aria-label={t("search.repository_open", { name: repository.name })}
                        className="search-result-row"
                      >
                        <span className={styles.source}>{repository.source}</span>
                        <span className={styles.resultMain}>
                          <span className={styles.resultTitle}>{repository.name}</span>
                          <span className={styles.resultDescription}>{repository.creator_name} · {repository.source_url || repository.source_creator_id}</span>
                          <span className={styles.resultIdentity}><MatchedIdentityBadge identity={repository.matched_identity} /></span>
                        </span>
                        <span className={`h-2 w-2 shrink-0 rounded-full ${authHealthPresentation(repository).dotClass}`} aria-hidden />
                      </Link>
                    );
                  })}
                </div>
              </section>
            )}

            {subscriptions.length > 0 && (
              <section className={styles.section} aria-labelledby="search-subscriptions-heading">
                <h2 id="search-subscriptions-heading" className={styles.sectionHeading}>{t("search.subscriptions_section")}</h2>
                <div className={styles.resultList}>
                  {subscriptions.map((subscription) => {
                    return (
                      <Link
                        key={subscription.id}
                        href={`/admin/subscriptions/${subscription.id}`}
                        aria-label={t("search.subscription_open", { name: subscription.name })}
                        className="search-result-row"
                      >
                        <span className={styles.creatorAvatar}>
                          {subscription.creator_name.slice(0, 2).toUpperCase()}
                        </span>
                        <span className={styles.resultMain}>
                          <span className={styles.resultTitle}>{subscription.name}</span>
                          <span className={styles.resultDescription}>
                            {subscription.creator_name} · {subscription.source_count}
                          </span>
                          <span className={styles.resultIdentity}><MatchedIdentityBadge identity={subscription.matched_identity} /></span>
                        </span>
                        <span className={`h-2 w-2 shrink-0 rounded-full ${subscription.is_active && subscription.sync_enabled ? "bg-success" : "bg-border"}`} aria-hidden />
                      </Link>
                    );
                  })}
                </div>
              </section>
            )}

            {total === 0 && (
              <EmptyState
                title={t("search.no_results")}
                description={t("search.no_results_for").replace("{query}", deferredQuery)}
              />
            )}
            {activeTab !== "all" && activeTotal > 0 && (
              <nav className={styles.pagination} aria-label={t("common.pagination")}>
                <button
                  type="button"
                  onClick={() => setPage((current) => Math.max(0, current - 1))}
                  disabled={page === 0}
                  className={styles.paginationButton}
                >
                  {t("works.prev")}
                </button>
                <span className={styles.pageIndicator}>{t("works.page", { page: page + 1 })}</span>
                <button
                  type="button"
                  onClick={() => setPage((current) => current + 1)}
                  disabled={(page + 1) * 50 >= activeTotal}
                  className={styles.paginationButton}
                >
                  {t("works.next")}
                </button>
              </nav>
            )}
          </>
        )}
        {has("system")?<details className="business-section"><summary>{t("jobs.maintenance_actions")}</summary>          <button
            type="button"
            className={styles.reindexButton}
            disabled={reindexPending}
            onClick={() => {
              setReindexError(null);
              setReindexOpen(true);
            }}
          >
            {reindexPending ? t("settings.reindexing") : t("settings.reindex")}
          </button>{reindexTask?<p className="operation-feedback" role="status">{t("jobs.action_accepted")} <Link className="text-link" href={"/admin/jobs?task="+encodeURIComponent(reindexTask)}>{t("jobs.task_detail")}</Link></p>:null}</details>:null}
      </BusinessPage>
  );
}

export default function SearchPage() {
  return (
    <BusinessPermissionGuard anyOf={["library", "subscriptions"]}>
      <Suspense>
        <SearchContent />
      </Suspense>
    </BusinessPermissionGuard>
  );
}

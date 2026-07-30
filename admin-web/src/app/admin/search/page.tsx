"use client";

import { Suspense, useDeferredValue, useEffect, useMemo, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";

import { api, type SearchQualifierToken, type SearchTarget } from "@/lib/api";
import {
  Breadcrumb,
  EmptyState,
  ErrorState,
  PageHeader,
  PageShell,
  PermissionGuard,
  SmartSearchInput,
  SourceBadge,
  useSearchComposer,
} from "@/components";
import { useT } from "@/lib/i18n";
import { useI18nFormat } from "@/lib/i18n-format";
import { useStaggeredEntrance } from "@/lib/motion";
import { useToast } from "@/components/Toast";
import { quoteSearchValue, searchUrl } from "@/lib/search-query";
import { usePermissions } from "@/lib/usePermissions";

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
  const deferredQuery = useDeferredValue(query);

  useEffect(() => {
    setQuery(searchParams.get("q") || "");
    setPage(Math.max(0, Number(searchParams.get("page") || 1) - 1));
  }, [searchParams]);

  useEffect(() => {
    const timer = window.setTimeout(() => {
      const next = new URLSearchParams();
      if (deferredQuery.trim()) next.set("q", deferredQuery.trim());
      if (page > 0) next.set("page", String(page + 1));
      router.replace(next.size ? `${pathname}?${next.toString()}` : pathname, { scroll: false });
    }, 250);
    return () => window.clearTimeout(timer);
  }, [deferredQuery, page, pathname, router]);

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

  const workEntrance = useStaggeredEntrance(works.map((item) => `work:${item.id}`));
  const creatorEntrance = useStaggeredEntrance(creators.map((item) => `creator:${item.id}`));
  const tagEntrance = useStaggeredEntrance(tags.map((item) => `tag:${item.id}`));
  const repositoryEntrance = useStaggeredEntrance(repositories.map((item) => `repo:${item.id}`));
  const subscriptionEntrance = useStaggeredEntrance(subscriptions.map((item) => `subscription:${item.id}`));

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

  return (
    <PermissionGuard anyOf={["library", "subscriptions"]}>
      <PageShell>
        <Breadcrumb items={[{ label: t("search.title") }, { label: deferredQuery || "…" }]} />
        <PageHeader
          title={t("search.title")}
          description={t("search.desc")}
          secondaryActions={has("system") ? (
            <button
              type="button"
              className="btn-ghost"
              onClick={() => {
                if (!confirm(t("settings.reindex_confirm_msg"))) return;
                api.reindexSearch()
                  .then((result) => toast.info(result.message || result.status))
                  .catch((error: Error) => toast.error(error.message));
              }}
            >
              {t("settings.reindex")}
            </button>
          ) : undefined}
        />

        <SmartSearchInput
          value={query}
          onChange={(value) => {
            setQuery(value);
            setPage(0);
          }}
          scope="global"
          autoFocus
          showHelp
          className="mb-6"
        />

        {!deferredQuery.trim() && (
          <EmptyState title={t("search.empty")} description={t("search.empty_desc")} />
        )}
        {results.isLoading && deferredQuery.trim() && (
          <div className="space-y-2">
            {Array.from({ length: 5 }).map((_, index) => (
              <div key={index} className="h-20 animate-pulse rounded-md bg-subtle" />
            ))}
          </div>
        )}
        {results.isError && deferredQuery.trim() && (
          <ErrorState message={(results.error as Error).message} onRetry={() => results.refetch()} />
        )}

        {results.data && deferredQuery.trim() && !results.isError && (
          <>
            <div className="mb-5 overflow-x-auto border-b border-border" aria-label={t("search.title")}>
              <div className="flex min-w-max gap-1" role="tablist">
                {tabs.map((tab) => (
                  <button
                    key={tab.key}
                    type="button"
                    role="tab"
                    aria-selected={activeTab === tab.key}
                    onClick={() => setTab(tab.key)}
                    disabled={composer.isPending}
                    className={`min-h-11 border-b-2 px-4 text-sm transition-colors ${
                      activeTab === tab.key
                        ? "border-accent font-medium text-accent"
                        : "border-transparent text-muted hover:text-fg"
                    }`}
                  >
                    {tab.label}
                    <span className="ml-1.5 rounded-full bg-subtle px-1.5 py-0.5 text-xs">{tab.count}</span>
                  </button>
                ))}
              </div>
            </div>

            {creators.length > 0 && (
              <section className="mb-7" aria-labelledby="search-creators-heading">
                <h2 id="search-creators-heading" className="mb-2 text-sm font-medium text-muted">{t("search.creators_section")}</h2>
                <div className="space-y-2">
                  {creators.map((creator, index) => {
                    const entrance = creatorEntrance(`creator:${creator.id}`, index);
                    return (
                      <Link
                        key={creator.id}
                        href={`/admin/creators/${creator.id}`}
                        className={`card-interactive ${entrance.className} flex min-h-16 items-center gap-4 p-4`}
                        style={entrance.style}
                      >
                        <span className="flex h-11 w-11 shrink-0 items-center justify-center rounded-full bg-accent text-sm font-semibold text-white">
                          {(creator.display_name || creator.name).trim().slice(0, 2).toUpperCase()}
                        </span>
                        <span className="min-w-0 flex-1">
                          <span className="block truncate font-medium">{creator.display_name || creator.name}</span>
                          {creator.description && <span className="mt-1 block truncate text-xs text-muted">{creator.description}</span>}
                        </span>
                      </Link>
                    );
                  })}
                </div>
              </section>
            )}

            {works.length > 0 && (
              <section className="mb-7" aria-labelledby="search-works-heading">
                <h2 id="search-works-heading" className="mb-2 text-sm font-medium text-muted">{t("search.works_section")}</h2>
                <div className="space-y-2">
                  {works.map((work, index) => {
                    const entrance = workEntrance(`work:${work.id}`, index);
                    return (
                      <article key={work.id} className={`card-interactive ${entrance.className} relative flex min-h-20 gap-4 p-4`} style={entrance.style}>
                        <Link
                          href={`/admin/works/${work.id}`}
                          className="absolute inset-0 rounded-md focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent"
                          aria-label={t("search.open_work", { title: work.title || t("search.untitled") })}
                        />
                        {work.thumbnail_asset_id ? (
                          <img
                            src={api.mediaUrl(work.thumbnail_asset_id, "thumb")}
                            alt=""
                            className="h-16 w-16 shrink-0 rounded object-cover"
                            loading="lazy"
                            decoding="async"
                          />
                        ) : (
                          <span className="flex h-16 w-16 shrink-0 items-center justify-center rounded border border-border bg-subtle text-xs text-muted">
                            {t("search.na")}
                          </span>
                        )}
                        <span className="min-w-0 flex-1">
                          <span className="flex items-center gap-2">
                            <span className="truncate text-sm font-medium">{work.title || t("search.untitled")}</span>
                            {work.is_nsfw && <span className="badge bg-danger-subtle text-xs text-danger">{t("search.nsfw")}</span>}
                          </span>
                          <span className="relative z-10 mt-1 flex flex-wrap items-center gap-2 text-xs text-muted">
                            {work.source && (
                              <SourceBadge
                                source={work.source}
                                href={searchUrl("/admin/search", `type:work source:${work.source}`)}
                              />
                            )}
                            {work.creator_name && work.creator_id && (
                              <Link href={`/admin/creators/${work.creator_id}`} className="text-accent hover:underline">
                                {work.creator_name}
                              </Link>
                            )}
                            {work.posted_at && <span>{fmt.date(work.posted_at)}</span>}
                          </span>
                          {work.tags && work.tags.length > 0 && (
                            <span className="relative z-10 mt-2 flex flex-wrap gap-1">
                              {work.tags.slice(0, 8).map((tag) => (
                                <button
                                  key={tag}
                                  type="button"
                                  onClick={() => router.push(searchUrl("/admin/search", `type:work tag:${quoteSearchValue(tag)}`))}
                                  className="badge min-h-6 text-[10px] hover:text-accent"
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
              <section className="mb-7" aria-labelledby="search-tags-heading">
                <h2 id="search-tags-heading" className="mb-2 text-sm font-medium text-muted">{t("search.tags_section")}</h2>
                <div className="flex flex-wrap gap-2">
                  {tags.map((tag, index) => {
                    const entrance = tagEntrance(`tag:${tag.id}`, index);
                    return (
                      <Link
                        key={tag.id}
                        href={`/admin/tags/${tag.id}`}
                        className={`${entrance.className} inline-flex min-h-11 items-center rounded-full border border-border bg-subtle px-4 text-sm hover:border-accent/40 hover:text-accent`}
                        style={entrance.style}
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
              <section className="mb-7" aria-labelledby="search-repositories-heading">
                <h2 id="search-repositories-heading" className="mb-2 text-sm font-medium text-muted">{t("search.repositories_section")}</h2>
                <div className="space-y-2">
                  {repositories.map((repository, index) => {
                    const entrance = repositoryEntrance(`repo:${repository.id}`, index);
                    return (
                      <Link
                        key={repository.id}
                        href={`/admin/repositories/${repository.id}`}
                        aria-label={t("search.repository_open", { name: repository.name })}
                        className={`card-interactive ${entrance.className} flex min-h-16 items-center gap-3 p-4`}
                        style={entrance.style}
                      >
                        <SourceBadge source={repository.source} />
                        <span className="min-w-0 flex-1">
                          <span className="block truncate font-medium">{repository.name}</span>
                          <span className="mt-1 block truncate text-xs text-muted">{repository.creator_name} · {repository.source_url || repository.source_creator_id}</span>
                        </span>
                        <span className={`h-2 w-2 shrink-0 rounded-full ${repository.auth_healthy ? "bg-success" : "bg-danger"}`} aria-hidden />
                      </Link>
                    );
                  })}
                </div>
              </section>
            )}

            {subscriptions.length > 0 && (
              <section className="mb-7" aria-labelledby="search-subscriptions-heading">
                <h2 id="search-subscriptions-heading" className="mb-2 text-sm font-medium text-muted">{t("search.subscriptions_section")}</h2>
                <div className="space-y-2">
                  {subscriptions.map((subscription, index) => {
                    const entrance = subscriptionEntrance(`subscription:${subscription.id}`, index);
                    return (
                      <Link
                        key={subscription.id}
                        href={`/admin/subscriptions/${subscription.id}`}
                        aria-label={t("search.subscription_open", { name: subscription.name })}
                        className={`card-interactive ${entrance.className} flex min-h-16 items-center gap-3 p-4`}
                        style={entrance.style}
                      >
                        <span className="flex h-10 w-10 shrink-0 items-center justify-center rounded-full bg-accent-subtle font-semibold text-accent">
                          {subscription.creator_name.slice(0, 2).toUpperCase()}
                        </span>
                        <span className="min-w-0 flex-1">
                          <span className="block truncate font-medium">{subscription.name}</span>
                          <span className="mt-1 block text-xs text-muted">
                            {subscription.creator_name} · {subscription.source_count}
                          </span>
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
              <nav className="mt-6 flex items-center justify-center gap-3" aria-label={t("common.pagination")}>
                <button
                  type="button"
                  onClick={() => setPage((current) => Math.max(0, current - 1))}
                  disabled={page === 0}
                  className="btn-ghost disabled:opacity-40"
                >
                  {t("works.prev")}
                </button>
                <span className="text-sm text-muted">{t("works.page", { page: page + 1 })}</span>
                <button
                  type="button"
                  onClick={() => setPage((current) => current + 1)}
                  disabled={(page + 1) * 50 >= activeTotal}
                  className="btn-ghost disabled:opacity-40"
                >
                  {t("works.next")}
                </button>
              </nav>
            )}
          </>
        )}
      </PageShell>
    </PermissionGuard>
  );
}

export default function SearchPage() {
  return (
    <Suspense>
      <SearchContent />
    </Suspense>
  );
}

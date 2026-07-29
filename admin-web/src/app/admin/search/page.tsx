"use client";
import { useState, useEffect, useRef, Suspense } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useRouter, useSearchParams } from "next/navigation";
import Link from "next/link";
import { api } from "@/lib/api";
import { useToast } from "@/components/Toast";
import { useT } from "@/lib/i18n";
import { useStaggeredEntrance } from "@/lib/motion";
import { PageHeader, PageShell, EmptyState, ErrorState, SourceBadge, PermissionGuard } from "@/components";
import { Breadcrumb } from "@/components/Breadcrumb";
import { useI18nFormat } from "@/lib/i18n-format";

type Kind = "all" | "works" | "creators" | "tags";

// Sliding tab indicator: transform-only (translateX + scaleX on a fixed
// 100px base) so the accent line glides between tabs without layout work.
const INDICATOR_BASE_PX = 100;

function SearchContent() {
  const t = useT();
  const fmt = useI18nFormat();
  const toast = useToast();
  const router = useRouter();
  const qc = useQueryClient();
  const searchParams = useSearchParams();
  const [query, setQuery] = useState("");
  const [debounced, setDebounced] = useState("");
  const [kind, setKind] = useState<Kind>("all");
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const tabRefs = useRef<Partial<Record<Kind, HTMLButtonElement | null>>>({});
  const [indicator, setIndicator] = useState<{ left: number; width: number } | null>(null);

  useEffect(() => {
    const q = searchParams.get("q");
    if (q) { setQuery(q); setDebounced(q); }
  }, [searchParams]);

  useEffect(() => {
    if (timerRef.current) clearTimeout(timerRef.current);
    timerRef.current = setTimeout(() => setDebounced(query), 300);
    return () => { if (timerRef.current) clearTimeout(timerRef.current); };
  }, [query]);

  const results = useQuery({
    queryKey: ["search", debounced, kind],
    queryFn: () => api.search(debounced, 0, kind === "all" ? 20 : 50, kind),
    enabled: debounced.length > 0,
  });

  const data = results.data;

  // Re-measure whenever the active tab or the counts inside labels change.
  useEffect(() => {
    const el = tabRefs.current[kind];
    if (!el) return;
    setIndicator({ left: el.offsetLeft, width: el.offsetWidth });
  }, [kind, data]);
  const worksHits = data?.results || [];
  const creatorsHits = (data?.creators || []) as any[];
  const tagsHits = (data?.tags || []) as any[];
  const creatorEntrance = useStaggeredEntrance(creatorsHits.map((creator) => `creator:${creator.id}`));
  const workEntrance = useStaggeredEntrance(worksHits.map((work) => `work:${work.id}`));
  const tagEntrance = useStaggeredEntrance(tagsHits.map((tag) => `tag:${tag.id}`));
  const total = data?.total || 0;

  const tabs: { key: Kind; label: string; count: number }[] = [
    { key: "all", label: t("search.tab_all"), count: total || (worksHits.length + creatorsHits.length + tagsHits.length) },
    { key: "works", label: t("search.tab_works"), count: kind === "works" ? total : 0 },
    { key: "creators", label: t("search.tab_creators"), count: creatorsHits.length },
    { key: "tags", label: t("search.tab_tags"), count: tagsHits.length },
  ];

  return (
    <PageShell>
      <Breadcrumb items={[{ label: t("search.title") }, { label: debounced || "..." }]} />
      <PageHeader title={t("search.title")} description={t("search.desc")}>
        <button className="btn-ghost px-3 py-1.5 text-xs" onClick={() => {
          if (confirm(t("settings.reindex_confirm_msg"))) {
            api.reindexSearch().then((d) => toast.info(d.message || d.status)).catch((e: Error) => toast.info(e.message));
          }
        }}>{t("settings.reindex")}</button>
      </PageHeader>
      <div className="flex gap-2 mb-6">
        <input value={query} onChange={(e) => setQuery(e.target.value)} placeholder={t("search.placeholder")}
          className="input flex-1 px-4" autoFocus />
      </div>

      {!debounced && <EmptyState title={t("search.empty")} description={t("search.empty_desc")} />}
      {results.isLoading && debounced && <div className="space-y-2">{Array.from({ length: 5 }).map((_, i) => <div key={i} className="h-20 rounded-md bg-subtle animate-pulse dark:bg-subtle" />)}</div>}
      {results.isError && debounced && <ErrorState message={(results.error as Error).message} onRetry={() => results.refetch()} />}

      {data && debounced && !results.isError && (
        <>
          <div className="relative flex gap-1 mb-4 border-b border-border">
            {tabs.map(tab => (
              <button key={tab.key} onClick={() => setKind(tab.key)}
                ref={(el) => { tabRefs.current[tab.key] = el; }}
                className={`px-4 py-2 text-sm transition-colors ${kind === tab.key ? "text-accent font-medium" : "text-muted hover:text-fg"}`}>
                {tab.label} <span className="ml-1.5 text-xs bg-subtle rounded-full px-1.5 py-0.5">{tab.count}</span>
              </button>
            ))}
            {indicator && (
              <span
                aria-hidden
                className="absolute bottom-0 left-0 h-0.5 bg-accent transition-transform duration-slow ease-expo"
                style={{
                  width: INDICATOR_BASE_PX,
                  transform: `translateX(${indicator.left}px) scaleX(${indicator.width / INDICATOR_BASE_PX})`,
                  transformOrigin: "left",
                }}
              />
            )}
          </div>

          {(kind === "all" || kind === "creators") && creatorsHits.length > 0 && (
            <div className="mb-6">
              {kind === "all" && <h3 className="text-sm font-medium text-muted mb-2">{t("search.creators_section")}</h3>}
              <div className="space-y-2">
                {creatorsHits.map((c: any, index: number) => {
                  const entrance = creatorEntrance(`creator:${c.id}`, index);
                  return (
                  <Link key={c.id} href={`/admin/creators/${c.id}`}
                    className={`card-interactive ${entrance.className} flex items-center gap-4 p-4 cursor-pointer`}
                    style={entrance.style}>
                    <div className="flex h-12 w-12 shrink-0 items-center justify-center rounded-full bg-gradient-to-br from-accent to-purple-600 text-white font-semibold text-lg">
                      {(c.display_name || c.name).trim().slice(0, 2).toUpperCase()}
                    </div>
                    <div className="flex-1 min-w-0">
                      <div className="font-medium text-fg dark:text-white">{c.display_name || c.name}</div>
                      {c.display_name && c.display_name !== c.name && <div className="text-xs text-muted font-mono">{c.name}</div>}
                      {c.description && <p className="text-xs text-muted mt-1 line-clamp-1">{c.description}</p>}
                    </div>
                  </Link>
                  );
                })}
              </div>
            </div>
          )}

          {(kind === "all" || kind === "works") && worksHits.length > 0 && (
            <div className="mb-6">
              {kind === "all" && <h3 className="text-sm font-medium text-muted mb-2">{t("search.works_section")}</h3>}
              <div className="space-y-2">
                {worksHits.map((w: any, index: number) => {
                  const entrance = workEntrance(`work:${w.id}`, index);
                  return (
                  <article key={w.id} className={`card-interactive ${entrance.className} relative flex gap-4 p-4`} style={entrance.style}>
                    <Link
                      href={`/admin/works/${w.id}`}
                      className="absolute inset-0 rounded-md focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent"
                      aria-label={t("search.open_work", { title: w.title || t("search.untitled") })}
                    />
                    {w.thumbnail_asset_id ? (
                      <img src={api.mediaUrl(w.thumbnail_asset_id, "thumb")} alt={w.title || ""} className="w-16 h-16 object-cover rounded shrink-0" loading="lazy" decoding="async" />
                    ) : (
                      <div className="flex h-16 w-16 shrink-0 items-center justify-center rounded-md border border-border bg-subtle text-xs text-muted">{t("search.na")}</div>
                    )}
                    <div className="flex-1 min-w-0">
                      <div className="flex items-center gap-2">
                        <span className="truncate text-sm font-medium">{w.title || t("search.untitled")}</span>
                        {w.is_nsfw && <span className="badge bg-danger-subtle text-danger text-xs">{t("search.nsfw")}</span>}
                      </div>
                      <div className="mt-1 flex items-center gap-2 text-xs text-muted">
                        {w.source && <span className="relative z-10"><SourceBadge source={w.source} href={`/admin/works?source=${w.source}`} /></span>}
                        {w.creator_name && (
                          <Link href={`/admin/creators/${w.creator_id || w.id}`}
                            className="relative z-10 text-accent hover:underline">{w.creator_name}</Link>
                        )}
                        {w.posted_at && <span>{fmt.date(w.posted_at)}</span>}
                      </div>
                      {w.tags && w.tags.length > 0 && (
                        <div className="flex gap-1 mt-1.5 flex-wrap">
                          {w.tags.slice(0, 8).map((tag: string) => (
                            <button
                              key={tag}
                              type="button"
                              onClick={() => router.push(`/admin/search?q=${encodeURIComponent(tag)}`)}
                              aria-label={t("search.search_tag", { tag })}
                              className="badge relative z-10 min-h-6 cursor-pointer text-[10px] hover:border-accent/30 hover:text-accent"
                            >
                              {tag}
                            </button>
                          ))}
                        </div>
                      )}
                    </div>
                  </article>
                  );
                })}
              </div>
            </div>
          )}

          {(kind === "all" || kind === "tags") && tagsHits.length > 0 && (
            <div>
              {kind === "all" && <h3 className="text-sm font-medium text-muted mb-2">{t("search.tags_section")}</h3>}
              <div className="flex flex-wrap gap-2">
                {tagsHits.map((tag: any, index: number) => {
                  const entrance = tagEntrance(`tag:${tag.id}`, index);
                  return (
                  <Link key={tag.id} href={`/admin/tags/${tag.id}`}
                    className={`${entrance.className} px-3 py-1.5 bg-subtle rounded-full text-sm hover:bg-accent/90 hover:text-accent dark:hover:bg-accent/90 dark:hover:text-accent transition-colors`}
                    style={entrance.style}>
                    #{tag.normalized_name}
                    {tag.category && <span className="text-xs text-muted ml-1">({tag.category})</span>}
                  </Link>
                  );
                })}
              </div>
            </div>
          )}

          {total === 0 && worksHits.length === 0 && creatorsHits.length === 0 && tagsHits.length === 0 && debounced && (
            <EmptyState title={t("search.no_results")} description={t("search.no_results_for").replace("{query}", debounced)} />
          )}
        </>
      )}
    </PageShell>
  );
}

export default function SearchPage() {
  return (
    <PermissionGuard module="library">
      <Suspense><SearchContent /></Suspense>
    </PermissionGuard>
  );
}

"use client";
import { useState, useMemo, useEffect, Suspense, memo } from "react";
import { useRouter, useSearchParams, usePathname } from "next/navigation";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useT } from "@/lib/i18n";
import { api, queryKeys, WorkListItem } from "@/lib/api";
import { PageHeader, EmptyState, ErrorState, SourceBadge } from "@/components";

function Img({ assetId, alt, className }: { assetId: string | undefined; alt: string; className?: string }) {
  const t = useT();
  const [mode, setMode] = useState<"thumb" | "preview" | "error">("thumb");
  useEffect(() => setMode("thumb"), [assetId]);
  if (!assetId || mode === "error") {
    return <div className={`${className || ""} flex items-center justify-center bg-[#f6f8fa] text-xs text-[#8c959f] dark:bg-[#21262d]`}>{t("works.na")}</div>;
  }
  return (
    <img
      src={api.mediaUrl(assetId, mode)}
      alt={alt}
      className={className}
      loading="lazy"
      onError={() => setMode(mode === "thumb" ? "preview" : "error")}
    />
  );
}

const GridCard = memo(function GridCard({ w, onToggleFavorite, selected, onToggleSelect }: {
  w: WorkListItem; onToggleFavorite: (id: string) => void;
  selected?: boolean; onToggleSelect?: (id: string) => void;
}) {
  const t = useT();
  const router = useRouter();
  const [pageIdx, setPageIdx] = useState(0);
  const assetIds = w.preview_asset_ids?.length ? w.preview_asset_ids : (w.thumbnail_asset_id ? [w.thumbnail_asset_id] : []);
  const hasMultiple = assetIds.length > 1;
  const currentId = assetIds[pageIdx] || assetIds[0];

  const prevPage = (e: React.MouseEvent) => { e.stopPropagation(); setPageIdx((pageIdx - 1 + assetIds.length) % assetIds.length); };
  const nextPage = (e: React.MouseEvent) => { e.stopPropagation(); setPageIdx((pageIdx + 1) % assetIds.length); };

  return (
    <div className="card overflow-hidden hover:shadow-md transition-shadow group relative">
        {onToggleSelect && (
          <input type="checkbox" checked={selected || false}
            onChange={(e) => { e.stopPropagation(); onToggleSelect(w.id); }}
            className="absolute top-2 left-2 z-10 w-4 h-4 rounded opacity-0 group-hover:opacity-100 checked:opacity-100 transition-opacity"
            aria-label={`Select ${w.title || "work"}`} />
        )}
        <div className="cursor-pointer" onClick={() => router.push(`/admin/works/${w.id}`)}>
      <div className="relative flex h-32 items-center justify-center overflow-hidden bg-[#f6f8fa] text-xs text-[#8c959f] dark:bg-[#21262d]">
        <Img assetId={currentId} alt={w.title || ""} className="w-full h-full object-cover" />
        <button onClick={(e) => { e.stopPropagation(); onToggleFavorite(w.id); }}
          className={`absolute top-1 right-1 text-base z-10 ${w.is_favorite ? "text-yellow-400" : "text-white/60 hover:text-yellow-300"} drop-shadow`}
          title={w.is_favorite ? "Unfavorite" : "Favorite"}>
          {w.is_favorite ? "★" : "☆"}
        </button>
        {w.asset_count > 1 && (
          <span className="absolute bottom-1 left-1 bg-black/70 text-white text-xs px-1.5 py-0.5 rounded font-medium">{w.asset_count}p</span>
        )}
        {w.is_ai_generated && (
          <span className="absolute top-1 left-1 rounded bg-[#bf8700] px-1.5 py-0.5 text-xs text-white">{t("works.ai_badge")}</span>
        )}
        {w.has_ugoira && (
          <span className="absolute bottom-1 right-1 bg-purple-600/90 text-white text-[10px] px-1.5 py-0.5 rounded font-medium">{t("works.gif_badge")}</span>
        )}
        {hasMultiple && (
          <div className="absolute inset-0 opacity-0 group-hover:opacity-100 transition-opacity flex items-center justify-between px-0.5">
            <button onClick={prevPage} className="bg-black/60 hover:bg-black/80 text-white rounded-full w-5 h-5 flex items-center justify-center text-xs leading-none transition-colors">&#9664;</button>
            <button onClick={nextPage} className="bg-black/60 hover:bg-black/80 text-white rounded-full w-5 h-5 flex items-center justify-center text-xs leading-none transition-colors">&#9654;</button>
          </div>
        )}
        {hasMultiple && (
          <div className="absolute bottom-1 left-1 flex gap-0.5">
            {assetIds.slice(0, 10).map((_, i) => (
              <span key={i} className={`w-1 h-1 rounded-full ${i === pageIdx ? "bg-white" : "bg-white/40"}`} />
            ))}
          </div>
        )}
      </div>
      <div className="p-3">
        <div className="truncate text-sm font-medium text-[#24292f] dark:text-[#e6edf3]">{w.title || t("works.untitled")}</div>
        <div className="flex items-center gap-1.5 mt-1">
          {w.source && <SourceBadge source={w.source} />}
          {w.has_ugoira && <span className="text-[10px] bg-purple-100 dark:bg-purple-900/30 text-purple-700 dark:text-purple-400 px-1 rounded">{t("works.gif_badge")}</span>}
          {w.creator_name && <span className="truncate text-xs text-[#57606a] dark:text-[#8b949e]">{w.creator_name}</span>}
        </div>
        <div className="mt-0.5 text-xs text-[#57606a] dark:text-[#8b949e]">
          {w.posted_at ? new Date(w.posted_at).toLocaleDateString() : t("works.no_date")}
        </div>      </div>
      </div>
    </div>
  );
});

type SortKey = "created_at" | "posted_at" | "title";
type ViewMode = "grid" | "list";

function WorksContent() {
  const t = useT();
  const router = useRouter();
  const qc = useQueryClient();
  const sp = useSearchParams();
  const pathname = usePathname();

  const SORT_OPTIONS: { key: SortKey; label: string }[] = [
    { key: "created_at", label: t("works.sort_imported") },
    { key: "posted_at", label: t("works.sort_posted") },
    { key: "title", label: t("works.sort_title") },
  ];

  const NSFW_FILTERS = [
    { key: "all", label: t("works.filter_all") },
    { key: "sfw", label: t("works.filter_sfw") },
    { key: "nsfw", label: t("works.filter_nsfw") },
  ];

  const SOURCE_FILTERS = [
    { key: "", label: t("works.filter_all_sources") },
    { key: "pixiv", label: "Pixiv" },
    { key: "x", label: "X" },
    { key: "iwara", label: "Iwara" },
    { key: "danbooru", label: "Danbooru" },
    { key: "pinterest", label: "Pinterest" },
    { key: "lofter", label: "Lofter" },
    { key: "weibo", label: "微博 (Weibo)" },
  ];
  // All filter state derived from URL — preserves filters on back/forward and supports sharing links
  const search = sp.get("q") ?? "";
  const page = Number(sp.get("p") ?? "0");
  const sortBy = (sp.get("sort") as SortKey) ?? "created_at";
  const sortOrder = (sp.get("order") as "desc" | "asc") ?? "desc";
  const sourceFilter = sp.get("source") ?? "";
  const creatorFilter = sp.get("creator") ?? "";
  const tagFilter = sp.get("tag") ?? "";
  const nsfwFilter = sp.get("nsfw") ?? "all";
  const isFavoriteFilter = sp.get("fav") === "1";
  const aiFilter = (sp.get("ai") as "all" | "human" | "ai") ?? "all";
  const viewMode = (sp.get("view") as ViewMode) ?? "grid";
  const limit = 25;

  // Local input for search field — debounced 300ms before writing to URL
  const [inputVal, setInputVal] = useState(search);
  useEffect(() => { setInputVal(search); }, [search]);
  useEffect(() => {
    if (inputVal === search) return;
    const timer = setTimeout(() => {
      const p = new URLSearchParams(sp.toString());
      if (inputVal) p.set("q", inputVal); else p.delete("q");
      p.delete("p");
      router.replace(`${pathname}?${p.toString()}`, { scroll: false });
    }, 300);
    return () => clearTimeout(timer);
  }, [inputVal]); // eslint-disable-line react-hooks/exhaustive-deps

  function updateParams(updates: Record<string, string | null>, resetPage = true) {
    const p = new URLSearchParams(sp.toString());
    for (const [k, v] of Object.entries(updates)) {
      if (v === null || v === "") p.delete(k); else p.set(k, v);
    }
    if (resetPage) p.delete("p");
    router.replace(`${pathname}?${p.toString()}`, { scroll: false });
  }

  // Batch selection
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const toggleSelect = (id: string) => { const n = new Set(selected); n.has(id) ? n.delete(id) : n.add(id); setSelected(n); };
  const selectAll = () => { if (!works.data?.items) return; setSelected(selected.size === works.data.items.length ? new Set() : new Set(works.data.items.map((w: any) => w.id))); };


  const filters = useMemo(() => ({
    search: search || undefined,
    source: sourceFilter || undefined,
    creator_id: creatorFilter || undefined,
    tag: tagFilter || undefined,
    is_nsfw: nsfwFilter === "all" ? undefined : nsfwFilter === "nsfw",
    is_favorite: isFavoriteFilter || undefined,
    is_ai_generated: aiFilter === "all" ? undefined : aiFilter === "ai",
    sort_by: sortBy,
    sort_order: sortOrder,
  }), [search, sourceFilter, creatorFilter, tagFilter, nsfwFilter, isFavoriteFilter, aiFilter, sortBy, sortOrder]);

  const useSearchPageLogic = !!search
    && !sourceFilter
    && !creatorFilter
    && !tagFilter
    && nsfwFilter === "all"
    && !isFavoriteFilter
    && aiFilter === "all"
    && sortBy === "created_at"
    && sortOrder === "desc";

  const works = useQuery({
    queryKey: [...queryKeys.works.all, page, filters, useSearchPageLogic ? "search-api" : "works-api"],
    queryFn: async () => {
      if (useSearchPageLogic) {
        const resp = await api.search(search, page * limit, limit);
        return { total: resp.total, items: resp.results };
      }
      return api.listWorks(page * limit, limit, filters);
    },
  });

  const toggleFavorite = useMutation({
    mutationFn: (id: string) => api.toggleWorkFavorite(id),
    onSuccess: (updated) => {
      // Directly patch the cache so the star updates immediately
      qc.setQueryData([...queryKeys.works.all, page, filters], (old: { total: number; items: WorkListItem[] } | undefined) => {
        if (!old) return old;
        return { ...old, items: old.items.map((w) => w.id === updated.id ? { ...w, is_favorite: updated.is_favorite } : w) };
      });
    },
  });

  const creators = useQuery({
    queryKey: queryKeys.creators.all,
    queryFn: () => api.listCreators(),
  });

  return (
    <main className="max-w-7xl mx-auto p-6 page-transition">
      <PageHeader title={t("works.title")} description={t("works.count", "0 works").replace("{count}", String(works.data?.total ?? 0))} />

      {selected.size > 0 && (
        <div className="mb-3 flex items-center gap-2 rounded-md border border-[#bf8700]/30 bg-[#fff8c5] px-4 py-2 dark:bg-[#bb800926]">
          <span className="text-xs text-[#9a6700] dark:text-[#d29922]">{selected.size} selected</span>
          <button onClick={selectAll} className="rounded-md border border-[#bf8700]/30 px-2 py-0.5 text-xs text-[#9a6700] hover:bg-white/40 dark:text-[#d29922] dark:hover:bg-white/10">
            {selected.size === (works.data?.items?.length || 0) ? "Deselect all" : "Select all"}
          </button>
          <span className="flex-1" />
          <button onClick={() => { setSelected(new Set()); }} className="btn-ghost py-0.5 text-xs">Cancel</button>
        </div>
      )}

      {/* Search & Filters */}
      <div className="toolbar mb-4">
        <input value={inputVal} onChange={(e) => setInputVal(e.target.value)}
          placeholder={t("works.search_title")} className="input w-48 py-1.5" />
        {(sourceFilter || creatorFilter || tagFilter || nsfwFilter !== "all" || aiFilter !== "all" || isFavoriteFilter) && (
          <button onClick={() => updateParams({ source: null, creator: null, tag: null, nsfw: null, ai: null, fav: null })}
            className="btn-danger py-1 text-xs"
            aria-label="Clear all filters">✕ Clear</button>
        )}
        {tagFilter && (
          <button
            onClick={() => updateParams({ tag: null })}
            className="inline-flex max-w-[12rem] items-center gap-1 rounded-full border border-blue-200 bg-blue-50 px-2.5 py-1 text-xs font-medium text-blue-700 hover:bg-blue-100 dark:border-blue-900 dark:bg-blue-950/30 dark:text-blue-300"
            title={`Clear tag filter: ${tagFilter}`}
          >
            <span className="truncate">#{tagFilter}</span>
            <span aria-hidden="true">&times;</span>
          </button>
        )}

        {/* Source filter — dropdown */}
        <select value={sourceFilter} onChange={(e) => updateParams({ source: e.target.value || null })} aria-label="Filter by source"
          className="select px-2 py-1.5 text-xs">
          {SOURCE_FILTERS.map((f) => (
            <option key={f.key} value={f.key}>{f.label}</option>
          ))}
        </select>

        {/* Creator filter */}
        {(creators.data?.items.length || 0) > 0 && (
          <select value={creatorFilter} onChange={(e) => updateParams({ creator: e.target.value || null })} aria-label="Filter by creator"
            className="select px-2 py-1.5 text-xs">
            <option value="">{t("works.filter_all_creators")}</option>
            {creators.data?.items.map((c) => (
              <option key={c.id} value={c.id}>{c.display_name || c.name}</option>
            ))}
          </select>
        )}

        {/* NSFW filter */}
        <div className="segmented-control">
          {NSFW_FILTERS.map((f) => (
            <button key={f.key} onClick={() => updateParams({ nsfw: f.key === "all" ? null : f.key })}
              className={`segment ${nsfwFilter === f.key ? "segment-active" : ""}`}>
              {f.label}
            </button>
          ))}
        </div>

        {/* Favorites filter */}
        <button onClick={() => updateParams({ fav: isFavoriteFilter ? null : "1" })}
          className={`segment border border-transparent ${isFavoriteFilter ? "border-[#bf8700]/30 bg-[#fff8c5] text-[#9a6700] dark:bg-[#bb800926] dark:text-[#d29922]" : ""}`}>
          {"★"} {t("works.filter_favorites")}
        </button>

        {/* AI filter */}
        <div className="segmented-control">
          {[
            { key: "all", label: t("works.ai_filter_all") },
            { key: "human", label: t("works.ai_filter_human") },
            { key: "ai", label: t("works.ai_filter_ai") },
          ].map((f) => (
            <button key={f.key} onClick={() => updateParams({ ai: f.key === "all" ? null : f.key })}
              className={`segment ${aiFilter === f.key ? "segment-active" : ""}`}>
              {f.label}
            </button>
          ))}
        </div>

        {/* Sort — click same field toggles direction */}
        <div className="segmented-control">
          {SORT_OPTIONS.map((s) => {
            const active = sortBy === s.key;
            const dir = active ? sortOrder : "desc";
            const nextDir = dir === "desc" ? "asc" : "desc";
            return (
              <button key={s.key}
                onClick={() => updateParams({
                  sort: s.key === "created_at" && nextDir === "desc" ? null : s.key,
                  order: nextDir === "desc" ? null : nextDir,
                })}
                className={`segment ${active ? "segment-active" : ""}`}>
                {s.label} {active ? (dir === "desc" ? "↓" : "↑") : ""}
              </button>
            );
          })}
        </div>

        <div className="flex-1" />

        {/* View toggle */}
        <div className="segmented-control">
          <button onClick={() => updateParams({ view: null }, false)}
            className={`segment ${viewMode === "grid" ? "segment-active" : ""}`}>
            {t("works.view_grid")}
          </button>
          <button onClick={() => updateParams({ view: "list" }, false)}
            className={`segment ${viewMode === "list" ? "segment-active" : ""}`}>
            {t("works.view_list")}
          </button>
        </div>
      </div>

      {/* Loading */}
      {works.isLoading && viewMode === "grid" && (
        <div className="overflow-x-auto grid grid-cols-2 md:grid-cols-4 lg:grid-cols-5 gap-4">
          {Array.from({ length: 10 }).map((_, i) => (
            <div key={i} className="card p-3 animate-pulse">
              <div className="mb-2 h-32 rounded bg-[#eaeef2] dark:bg-[#21262d]" />
              <div className="h-3 w-3/4 rounded bg-[#eaeef2] dark:bg-[#21262d]" />
            </div>
          ))}
        </div>
      )}
      {works.isLoading && viewMode === "list" && (
        <div className="space-y-1">{Array.from({ length: 8 }).map((_, i) => <div key={i} className="h-12 rounded-md bg-[#eaeef2] dark:bg-[#21262d] animate-pulse" />)}</div>
      )}

      {/* Error */}
      {works.error && <ErrorState message={(works.error as Error).message} onRetry={() => works.refetch()} />}

      {/* Empty */}
      {works.data && !works.data.items?.length && (
        <EmptyState title={t("works.no_works")} description={search || sourceFilter || creatorFilter || tagFilter ? t("works.no_works_filter") : t("works.no_works_desc")} />
      )}

      {/* Grid View */}
      {works.data && works.data.items?.length > 0 && viewMode === "grid" && (
        <div className="overflow-x-auto grid grid-cols-2 md:grid-cols-4 lg:grid-cols-5 gap-4 mb-6">
          {works.data.items.map((w: WorkListItem) => <GridCard key={w.id} w={w} onToggleFavorite={(id) => toggleFavorite.mutate(id)} selected={selected.has(w.id)} onToggleSelect={toggleSelect} />)}
        </div>
      )}

      {/* List View */}
      {works.data && works.data.items?.length > 0 && viewMode === "list" && (
        <div className="space-y-1 mb-6">
          {works.data.items.map((w: WorkListItem) => (
            <div key={w.id} className="card p-3 flex items-center gap-3 cursor-pointer hover:shadow-md transition-shadow" onClick={() => router.push(`/admin/works/${w.id}`)}>
              <div className="h-12 w-12 shrink-0 overflow-hidden rounded-md bg-[#f6f8fa] dark:bg-[#21262d]">
                <Img assetId={w.thumbnail_asset_id} alt={w.title || ""} className="w-full h-full object-cover" />
              </div>
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-2">
                  <span className="truncate text-sm font-medium text-[#24292f] dark:text-[#e6edf3]">{w.title || t("works.untitled")}</span>
                  {w.is_nsfw && <span className="text-xs bg-red-100 dark:bg-red-900/30 text-red-600 px-1 rounded">{t("works.nsfw_badge")}</span>}
                  {w.asset_count > 1 && <span className="text-xs text-gray-400">{w.asset_count}p</span>}
                  {w.has_ugoira && <span className="text-xs bg-purple-100 dark:bg-purple-900/30 text-purple-700 dark:text-purple-400 px-1 rounded">{t("works.gif_badge")}</span>}
                </div>
                <div className="mt-0.5 flex items-center gap-2 text-xs text-[#57606a] dark:text-[#8b949e]">
                  {w.source && <SourceBadge source={w.source} />}
                  {w.creator_name && <span>{w.creator_name}</span>}
                  <span>{w.posted_at ? new Date(w.posted_at).toLocaleDateString() : "—"}</span>
                </div>
              </div>
              <button onClick={(e) => { e.stopPropagation(); toggleFavorite.mutate(w.id); }}
                className={`text-lg shrink-0 ${w.is_favorite ? "text-yellow-500" : "text-gray-300 dark:text-gray-600 hover:text-yellow-400"}`}
                title={w.is_favorite ? "Unfavorite" : "Favorite"}>
                {w.is_favorite ? "★" : "☆"}
              </button>
            </div>
          ))}
        </div>
      )}

      {/* Pagination */}
      {(works.data?.total ?? 0) > 0 && (
        <div className="flex gap-2 justify-center">
          <button disabled={page === 0} onClick={() => updateParams({ p: page <= 1 ? null : String(page - 1) }, false)} className="btn-ghost disabled:opacity-30">{t("works.prev")}</button>
          <span className="px-3 py-1 text-sm text-[#57606a] dark:text-[#8b949e]">{t("works.page").replace("{page}", String(page + 1))}</span>
          <button onClick={() => updateParams({ p: String(page + 1) }, false)} disabled={!works.data || (page + 1) * limit >= works.data.total} className="btn-ghost disabled:opacity-30">{t("works.next")}</button>
        </div>
      )}
    </main>
  );
}

export default function WorksPage() {
  return (
    <Suspense>
      <WorksContent />
    </Suspense>
  );
}

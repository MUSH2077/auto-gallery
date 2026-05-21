"use client";
import { useState, useMemo } from "react";
import { useRouter } from "next/navigation";
import { useQuery, useMutation } from "@tanstack/react-query";
import { useT } from "@/lib/i18n";
import { api, queryKeys, WorkListItem } from "@/lib/api";
import { PageHeader, EmptyState, ErrorState, SourceBadge } from "@/components";

function Img({ assetId, alt, className }: { assetId: string | undefined; alt: string; className?: string }) {
  const t = useT();
  const [err, setErr] = useState(false);
  if (!assetId || err) {
    return <div className={`${className || ""} flex items-center justify-center text-gray-400 text-xs bg-gray-100 dark:bg-slate-700`}>{t("works.na")}</div>;
  }
  return (
    <img src={api.mediaUrl(assetId, "thumb")} alt={alt} className={className} loading="lazy"
      onError={() => setErr(true)} />
  );
}

function GridCard({ w, onToggleFavorite }: { w: WorkListItem; onToggleFavorite: (id: string) => void }) {
  const t = useT();
  const router = useRouter();
  const [pageIdx, setPageIdx] = useState(0);
  const assetIds = w.preview_asset_ids?.length ? w.preview_asset_ids : (w.thumbnail_asset_id ? [w.thumbnail_asset_id] : []);
  const hasMultiple = assetIds.length > 1;
  const currentId = assetIds[pageIdx] || assetIds[0];

  const prevPage = (e: React.MouseEvent) => { e.stopPropagation(); setPageIdx((pageIdx - 1 + assetIds.length) % assetIds.length); };
  const nextPage = (e: React.MouseEvent) => { e.stopPropagation(); setPageIdx((pageIdx + 1) % assetIds.length); };

  return (
    <div className="bg-white dark:bg-slate-800 rounded-lg shadow overflow-hidden cursor-pointer hover:shadow-md transition-shadow group" onClick={() => router.push(`/admin/works/${w.id}`)}>
      <div className="h-32 bg-gray-100 dark:bg-slate-700 flex items-center justify-center text-gray-400 text-xs overflow-hidden relative">
        <Img assetId={currentId} alt={w.title || ""} className="w-full h-full object-cover" />
        <button onClick={(e) => { e.stopPropagation(); onToggleFavorite(w.id); }}
          className={`absolute top-1 right-1 text-base z-10 ${w.is_favorite ? "text-yellow-400" : "text-white/60 hover:text-yellow-300"} drop-shadow`}
          title={w.is_favorite ? "Unfavorite" : "Favorite"}>
          {w.is_favorite ? "★" : "☆"}
        </button>
        {w.asset_count > 1 && (
          <span className="absolute bottom-1 left-1 bg-black/70 text-white text-xs px-1.5 py-0.5 rounded font-medium">{w.asset_count}p</span>
        )}
        {w.is_nsfw && (
          <span className="absolute top-1 left-1 bg-red-600 text-white text-xs px-1.5 py-0.5 rounded">{t("works.nsfw_badge")}</span>
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
        <div className="text-sm font-medium truncate dark:text-white">{w.title || t("works.untitled")}</div>
        <div className="flex items-center gap-1.5 mt-1">
          {w.source && <SourceBadge source={w.source} />}
          {w.has_ugoira && <span className="text-[10px] bg-purple-100 dark:bg-purple-900/30 text-purple-700 dark:text-purple-400 px-1 rounded">{t("works.gif_badge")}</span>}
          {w.creator_name && <span className="text-xs text-gray-400 dark:text-gray-500 truncate">{w.creator_name}</span>}
        </div>
        <div className="text-xs text-gray-400 dark:text-gray-500 mt-0.5">
          {w.posted_at ? new Date(w.posted_at).toLocaleDateString() : t("works.no_date")}
        </div>
      </div>
    </div>
  );
}

type SortKey = "created_at" | "posted_at" | "title";
type ViewMode = "grid" | "list";

export default function WorksPage() {
  const t = useT();
  const router = useRouter();

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
  ];
  const [search, setSearch] = useState("");
  const [page, setPage] = useState(0);
  const [sortBy, setSortBy] = useState<SortKey>("created_at");
  const [sortOrder, setSortOrder] = useState<"desc" | "asc">("desc");
  const [nsfwFilter, setNsfwFilter] = useState("all");
  const [sourceFilter, setSourceFilter] = useState("");
  const [creatorFilter, setCreatorFilter] = useState("");
  const [isFavoriteFilter, setIsFavoriteFilter] = useState(false);
  const [viewMode, setViewMode] = useState<ViewMode>("grid");
  const limit = 25;

  const filters = useMemo(() => ({
    search: search || undefined,
    source: sourceFilter || undefined,
    creator_id: creatorFilter || undefined,
    is_nsfw: nsfwFilter === "all" ? undefined : nsfwFilter === "nsfw",
    is_favorite: isFavoriteFilter || undefined,
    sort_by: sortBy,
    sort_order: sortOrder,
  }), [search, sourceFilter, creatorFilter, nsfwFilter, isFavoriteFilter, sortBy, sortOrder]);

  const works = useQuery({
    queryKey: [...queryKeys.works.all, page, filters],
    queryFn: () => api.listWorks(page * limit, limit, filters),
  });

  const toggleFavorite = useMutation({
    mutationFn: (id: string) => api.toggleWorkFavorite(id),
    onSuccess: () => works.refetch(),
  });

  const creators = useQuery({
    queryKey: queryKeys.creators.all,
    queryFn: () => api.listCreators(),
  });

  return (
    <main className="max-w-7xl mx-auto p-6">
      <PageHeader title={t("works.title")} description={t("works.count", "0 works").replace("{count}", String(works.data?.length || 0)).replace("{page}", String(page + 1))} />

      {/* Search & Filters */}
      <div className="flex flex-wrap items-center gap-2 mb-4">
        <input value={search} onChange={(e) => { setSearch(e.target.value); setPage(0); }}
          placeholder={t("works.search_title")} className="border rounded px-3 py-1.5 text-sm w-48 dark:bg-slate-700 dark:text-white dark:border-slate-600" />

        {/* Source filter */}
        <div className="flex gap-0.5 bg-gray-100 dark:bg-slate-700 rounded p-0.5">
          {SOURCE_FILTERS.map((f) => (
            <button key={f.key} onClick={() => { setSourceFilter(f.key); setPage(0); }}
              className={`px-2.5 py-1 text-xs rounded transition-colors ${sourceFilter === f.key ? "bg-white dark:bg-slate-600 shadow-sm font-medium" : "text-gray-500 hover:text-gray-700 dark:text-gray-400"}`}>
              {f.label}
            </button>
          ))}
        </div>

        {/* Creator filter */}
        {(creators.data?.length || 0) > 0 && (
          <select value={creatorFilter} onChange={(e) => { setCreatorFilter(e.target.value); setPage(0); }}
            className="border rounded px-2 py-1.5 text-xs dark:bg-slate-700 dark:text-white dark:border-slate-600">
            <option value="">{t("works.filter_all_creators")}</option>
            {creators.data?.map((c) => (
              <option key={c.id} value={c.id}>{c.display_name || c.name}</option>
            ))}
          </select>
        )}

        {/* NSFW filter */}
        <div className="flex gap-0.5 bg-gray-100 dark:bg-slate-700 rounded p-0.5">
          {NSFW_FILTERS.map((f) => (
            <button key={f.key} onClick={() => { setNsfwFilter(f.key); setPage(0); }}
              className={`px-2.5 py-1 text-xs rounded transition-colors ${nsfwFilter === f.key ? "bg-white dark:bg-slate-600 shadow-sm font-medium" : "text-gray-500 hover:text-gray-700 dark:text-gray-400"}`}>
              {f.label}
            </button>
          ))}
        </div>

        {/* Favorites filter */}
        <button onClick={() => { setIsFavoriteFilter(!isFavoriteFilter); setPage(0); }}
          className={`px-2.5 py-1 text-xs rounded transition-colors ${isFavoriteFilter ? "bg-yellow-100 dark:bg-yellow-900/30 text-yellow-700 font-medium" : "text-gray-500 hover:text-gray-700 dark:text-gray-400"}`}>
          {"★"} {t("works.filter_favorites")}
        </button>

        {/* Sort */}
        <select value={`${sortBy}-${sortOrder}`} onChange={(e) => {
          const [k, o] = e.target.value.split("-");
          setSortBy(k as SortKey);
          setSortOrder(o as "desc" | "asc");
          setPage(0);
        }} className="border rounded px-2 py-1.5 text-xs dark:bg-slate-700 dark:text-white dark:border-slate-600">
          {SORT_OPTIONS.map((s) => (
            <option key={`${s.key}-desc`} value={`${s.key}-desc`}>{s.label} ↓</option>
          ))}
          {SORT_OPTIONS.map((s) => (
            <option key={`${s.key}-asc`} value={`${s.key}-asc`}>{s.label} ↑</option>
          ))}
        </select>

        <div className="flex-1" />

        {/* View toggle */}
        <div className="flex gap-0.5 bg-gray-100 dark:bg-slate-700 rounded p-0.5">
          <button onClick={() => setViewMode("grid")}
            className={`px-2.5 py-1 rounded text-xs ${viewMode === "grid" ? "bg-white dark:bg-slate-600 shadow-sm" : "text-gray-500"}`}>
            {t("works.view_grid")}
          </button>
          <button onClick={() => setViewMode("list")}
            className={`px-2.5 py-1 rounded text-xs ${viewMode === "list" ? "bg-white dark:bg-slate-600 shadow-sm" : "text-gray-500"}`}>
            {t("works.view_list")}
          </button>
        </div>
      </div>

      {/* Loading */}
      {works.isLoading && viewMode === "grid" && (
        <div className="grid grid-cols-2 md:grid-cols-4 lg:grid-cols-5 gap-4">
          {Array.from({ length: 10 }).map((_, i) => (
            <div key={i} className="bg-white dark:bg-slate-800 rounded-lg shadow p-3 animate-pulse">
              <div className="h-32 bg-gray-200 dark:bg-slate-700 rounded mb-2" />
              <div className="h-3 bg-gray-200 dark:bg-slate-700 rounded w-3/4" />
            </div>
          ))}
        </div>
      )}
      {works.isLoading && viewMode === "list" && (
        <div className="space-y-1">{Array.from({ length: 8 }).map((_, i) => <div key={i} className="h-12 bg-gray-100 dark:bg-slate-700 rounded animate-pulse" />)}</div>
      )}

      {/* Error */}
      {works.error && <ErrorState message={(works.error as Error).message} onRetry={() => works.refetch()} />}

      {/* Empty */}
      {works.data && !works.data.length && (
        <EmptyState title={t("works.no_works")} description={search || sourceFilter || creatorFilter ? t("works.no_works_filter") : t("works.no_works_desc")} />
      )}

      {/* Grid View */}
      {works.data && works.data.length > 0 && viewMode === "grid" && (
        <div className="grid grid-cols-2 md:grid-cols-4 lg:grid-cols-5 gap-4 mb-6">
          {works.data.map((w: WorkListItem) => <GridCard key={w.id} w={w} onToggleFavorite={(id) => toggleFavorite.mutate(id)} />)}
        </div>
      )}

      {/* List View */}
      {works.data && works.data.length > 0 && viewMode === "list" && (
        <div className="space-y-1 mb-6">
          {works.data.map((w: WorkListItem) => (
            <div key={w.id} className="bg-white dark:bg-slate-800 rounded-lg shadow-sm p-3 flex items-center gap-3 cursor-pointer hover:shadow-md transition-shadow" onClick={() => router.push(`/admin/works/${w.id}`)}>
              <div className="w-12 h-12 bg-gray-100 dark:bg-slate-700 rounded overflow-hidden shrink-0">
                <Img assetId={w.thumbnail_asset_id} alt={w.title || ""} className="w-full h-full object-cover" />
              </div>
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-2">
                  <span className="text-sm font-medium dark:text-white truncate">{w.title || t("works.untitled")}</span>
                  {w.is_nsfw && <span className="text-xs bg-red-100 dark:bg-red-900/30 text-red-600 px-1 rounded">{t("works.nsfw_badge")}</span>}
                  {w.asset_count > 1 && <span className="text-xs text-gray-400">{w.asset_count}p</span>}
                  {w.has_ugoira && <span className="text-xs bg-purple-100 dark:bg-purple-900/30 text-purple-700 dark:text-purple-400 px-1 rounded">{t("works.gif_badge")}</span>}
                </div>
                <div className="flex items-center gap-2 text-xs text-gray-400 dark:text-gray-500 mt-0.5">
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
      {(works.data?.length || 0) > 0 && (
        <div className="flex gap-2 justify-center">
          <button disabled={page === 0} onClick={() => setPage(page - 1)} className="px-3 py-1 text-sm border rounded disabled:opacity-30 dark:border-slate-600 dark:text-gray-300">{t("works.prev")}</button>
          <span className="px-3 py-1 text-sm text-gray-500 dark:text-gray-400">{t("works.page").replace("{page}", String(page + 1))}</span>
          <button onClick={() => setPage(page + 1)} disabled={!works.data || works.data.length < limit} className="px-3 py-1 text-sm border rounded disabled:opacity-30 dark:border-slate-600 dark:text-gray-300">{t("works.next")}</button>
        </div>
      )}
    </main>
  );
}

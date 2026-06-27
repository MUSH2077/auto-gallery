"use client";
import { useState, useMemo, useEffect, Suspense } from "react";
import Link from "next/link";
import { useRouter, useSearchParams, usePathname } from "next/navigation";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useT } from "@/lib/i18n";
import { api, queryKeys, WorkListItem } from "@/lib/api";
import { PageHeader, EmptyState, ErrorState, SourceBadge, PageShell, SelectionBar } from "@/components";

function Img({ assetId, alt, className }: { assetId: string | undefined; alt: string; className?: string }) {
  const t = useT();
  const [err, setErr] = useState(false);
  if (!assetId || err) {
    return <div className={`${className || ""} flex items-center justify-center text-muted text-xs bg-subtle`}>{t("works.na")}</div>;
  }
  return (
    <img src={api.mediaUrl(assetId, "thumb")} alt={alt} className={className} loading="lazy"
      onError={() => setErr(true)} />
  );
}

function GridCard({
  w,
  onToggleFavorite,
  trashMode,
  onRestore,
  onPurge,
  selectable,
  selected,
  onToggleSelect,
  index,
}: {
  w: WorkListItem;
  onToggleFavorite: (id: string) => void;
  trashMode?: boolean;
  onRestore?: (id: string) => void;
  onPurge?: (id: string) => void;
  selectable?: boolean;
  selected?: boolean;
  onToggleSelect?: (id: string) => void;
  index?: number;
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
    <div
      className={`card-interactive page-item overflow-hidden cursor-pointer group ${selected ? "ring-2 ring-accent dark:ring-accent" : ""}`}
      style={{ "--delay": `${Math.min((index ?? 0) * 30, 300)}ms` } as React.CSSProperties}
      onClick={() => router.push(`/admin/works/${w.id}`)}
    >
      <div className="h-32 bg-subtle flex items-center justify-center text-muted text-xs overflow-hidden relative">
        <Img assetId={currentId} alt={w.title || ""} className="w-full h-full object-cover" />
        {selectable && (
          <label className="absolute left-1 top-1 z-20 flex h-7 w-7 items-center justify-center rounded bg-black/60 text-white shadow-sm">
            <span className="sr-only">{t("works.select_work")}</span>
            <input
              type="checkbox"
              checked={!!selected}
              onChange={(e) => { e.stopPropagation(); onToggleSelect?.(w.id); }}
              onClick={(e) => e.stopPropagation()}
              className="h-4 w-4 rounded border-white"
            />
          </label>
        )}
        <button onClick={(e) => { e.stopPropagation(); onToggleFavorite(w.id); }}
          className={`absolute top-1 right-1 text-base z-10 ${w.is_favorite ? "text-yellow-400" : "text-white/60 hover:text-yellow-300"} drop-shadow`}
          title={w.is_favorite ? t("works.unfavorite") : t("works.favorite")}
          aria-label={w.is_favorite ? t("works.unfavorite") : t("works.favorite")}>
          {w.is_favorite ? "★" : "☆"}
        </button>
        {w.asset_count > 1 && (
          <span className="absolute bottom-1 left-1 bg-black/70 text-white text-xs px-1.5 py-0.5 rounded font-medium">{w.asset_count}p</span>
        )}
        {w.is_ai_generated && (
          <span className="absolute top-1 left-1 bg-yellow-500 text-white text-xs px-1.5 py-0.5 rounded">{t("works.ai_badge")}</span>
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
        {trashMode && !selectable && (
          <span className="absolute left-1 top-1 rounded bg-danger/90 px-1.5 py-0.5 text-xs font-medium text-white">Trash</span>
        )}
      </div>
      <div className="p-3">
        <div className="text-sm font-medium truncate dark:text-white">{w.title || t("works.untitled")}</div>
        <div className="flex items-center gap-1.5 mt-1">
          {w.source && <SourceBadge source={w.source} href={`/admin/works?source=${w.source}`} />}
          {w.has_ugoira && <span className="text-[10px] bg-purple-100 dark:bg-purple-900/30 text-purple-700 dark:text-purple-400 px-1 rounded">{t("works.gif_badge")}</span>}
          {w.creator_name && w.creator_id && (
  <Link href={`/admin/creators/${w.creator_id}`}
    className="text-xs text-blue-600 hover:underline truncate"
    onClick={(e) => e.stopPropagation()}>
    {w.creator_name}
  </Link>
)}
        </div>
        <div className="text-xs text-muted mt-0.5">
          {w.posted_at ? new Date(w.posted_at).toLocaleDateString() : t("works.no_date")}
        </div>
        {trashMode && (
          <div className="mt-3 flex gap-2">
            <button onClick={(e) => { e.stopPropagation(); onRestore?.(w.id); }} className="rounded border border-border px-2 py-1 text-xs hover:bg-subtle dark:border-border dark:hover:bg-subtle">{t("works.restore")}</button>
            <button onClick={(e) => { e.stopPropagation(); onPurge?.(w.id); }} className="rounded bg-danger px-2 py-1 text-xs text-white hover:bg-danger">{t("works.purge")}</button>
          </div>
        )}
      </div>
    </div>
  );
}

type SortKey = "created_at" | "posted_at" | "title";
type ViewMode = "grid" | "list";

function WorksContent() {
  const t = useT();
  const router = useRouter();
  const qc = useQueryClient();
  const sp = useSearchParams();
  const pathname = usePathname();
  const [selectedWorkIds, setSelectedWorkIds] = useState<Set<string>>(new Set());
  const [filtersOpen, setFiltersOpen] = useState(false);

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
  const nsfwFilter = sp.get("nsfw") ?? "all";
  const isFavoriteFilter = sp.get("fav") === "1";
  const aiFilter = (sp.get("ai") as "all" | "human" | "ai") ?? "all";
  const curationVisibility = sp.get("curation") === "trashed" ? "trashed" : "visible";
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

  function clearFilters() {
    const p = new URLSearchParams();
    if (curationVisibility === "trashed") p.set("curation", "trashed");
    setInputVal("");
    router.replace(p.toString() ? `${pathname}?${p.toString()}` : pathname, { scroll: false });
  }

  const filters = useMemo(() => ({
    search: search || undefined,
    source: sourceFilter || undefined,
    creator_id: creatorFilter || undefined,
    is_nsfw: nsfwFilter === "all" ? undefined : nsfwFilter === "nsfw",
    is_favorite: isFavoriteFilter || undefined,
    is_ai_generated: aiFilter === "all" ? undefined : aiFilter === "ai",
    curation_visibility: curationVisibility,
    sort_by: sortBy,
    sort_order: sortOrder,
  }), [search, sourceFilter, creatorFilter, nsfwFilter, isFavoriteFilter, aiFilter, curationVisibility, sortBy, sortOrder]);

  const activeFilterCount = [
    search,
    sourceFilter,
    creatorFilter,
    nsfwFilter !== "all",
    isFavoriteFilter,
    aiFilter !== "all",
    sortBy !== "created_at" || sortOrder !== "desc",
  ].filter(Boolean).length;

  const useSearchPageLogic = !!search
    && !sourceFilter
    && !creatorFilter
    && nsfwFilter === "all"
    && !isFavoriteFilter
    && aiFilter === "all"
    && curationVisibility === "visible"
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

  useEffect(() => {
    setSelectedWorkIds(new Set());
  }, [page, filters]);

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

  const restoreWork = useMutation({
    mutationFn: (id: string) => api.batchCurateWorks([id], "restore", "restored from trash"),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: queryKeys.works.all });
      qc.invalidateQueries({ queryKey: queryKeys.curation.all });
    },
  });

  const purgeWork = useMutation({
    mutationFn: (id: string) => api.purgeWorks([id], "Purge one trashed work"),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: queryKeys.works.all });
      qc.invalidateQueries({ queryKey: queryKeys.curation.all });
    },
  });

  const batchTrash = useMutation({
    mutationFn: (ids: string[]) => api.batchCurateWorks(ids, "trash", "batch move to trash", `Move ${ids.length} works to trash`),
    onSuccess: () => {
      setSelectedWorkIds(new Set());
      qc.invalidateQueries({ queryKey: queryKeys.works.all });
      qc.invalidateQueries({ queryKey: queryKeys.curation.all });
    },
  });

  const creators = useQuery({
    queryKey: queryKeys.creators.all,
    queryFn: () => api.listCreators(),
  });

  const pageWorkIds = works.data?.items.map((w) => w.id) || [];
  const selectedCount = selectedWorkIds.size;
  const pageAllSelected = pageWorkIds.length > 0 && pageWorkIds.every((id) => selectedWorkIds.has(id));
  const toggleSelectWork = (id: string) => {
    setSelectedWorkIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });
  };
  const toggleSelectPage = () => {
    setSelectedWorkIds((prev) => {
      if (pageAllSelected) return new Set([...prev].filter((id) => !pageWorkIds.includes(id)));
      return new Set([...prev, ...pageWorkIds]);
    });
  };
  const moveSelectedToTrash = () => {
    const ids = [...selectedWorkIds];
    if (!ids.length) return;
    if (window.confirm(t("works.batch_trash_confirm", { count: ids.length }))) batchTrash.mutate(ids);
  };

  return (
    <PageShell>
      <PageHeader title={t("works.title")} description={t("works.count", "0 works").replace("{count}", String(works.data?.total ?? 0))} />

      {/* Search & Filters */}
      <div className="mb-3 flex flex-wrap items-center gap-2 md:hidden">
        <button
          onClick={() => setFiltersOpen((value) => !value)}
          className="rounded-md border border-border px-3 py-1.5 text-sm font-medium dark:border-border"
          aria-expanded={filtersOpen}
          aria-controls="works-filter-panel"
        >
          {t("works.filters")} {activeFilterCount > 0 && <span className="ml-1 rounded-full bg-accent px-1.5 py-0.5 text-xs text-white">{activeFilterCount}</span>}
        </button>
        {activeFilterCount > 0 && (
          <button onClick={clearFilters} className="rounded-md border border-border px-3 py-1.5 text-sm dark:border-border">
            {t("works.clear_filters")}
          </button>
        )}
      </div>

      <div id="works-filter-panel" className={`${filtersOpen ? "flex" : "hidden"} mb-4 flex-col gap-2 rounded-md border border-border bg-white p-3 dark:border-border dark:bg-subtle md:flex md:flex-row md:flex-wrap md:items-center md:border-0 md:bg-transparent md:p-0 md:dark:bg-transparent`}>
        <div className="flex gap-0.5 bg-subtle rounded p-0.5">
          <button onClick={() => updateParams({ curation: null })}
            aria-label={t("works.gallery")}
            className={`px-2.5 py-1 text-xs rounded transition-colors ${curationVisibility === "visible" ? "bg-surface shadow-sm font-medium" : "text-muted hover:text-fg"}`}>
            {t("works.gallery")}
          </button>
          <button onClick={() => updateParams({ curation: "trashed" })}
            aria-label={t("works.trash")}
            className={`px-2.5 py-1 text-xs rounded transition-colors ${curationVisibility === "trashed" ? "bg-surface shadow-sm font-medium" : "text-muted hover:text-fg"}`}>
            {t("works.trash")}
          </button>
        </div>

        <input value={inputVal} onChange={(e) => setInputVal(e.target.value)}
          aria-label={t("works.search_title")}
          placeholder={t("works.search_title")} className="border rounded px-3 py-1.5 text-sm w-48 dark:bg-subtle dark:text-white dark:border-border" />

        {/* Source filter — dropdown */}
        <select value={sourceFilter} onChange={(e) => updateParams({ source: e.target.value || null })}
          aria-label={t("works.filter_source")}
          className="border rounded px-2 py-1.5 text-xs dark:bg-subtle dark:text-white dark:border-border">
          {SOURCE_FILTERS.map((f) => (
            <option key={f.key} value={f.key}>{f.label}</option>
          ))}
        </select>

        {/* Creator filter */}
        {(creators.data?.items.length || 0) > 0 && (
          <select value={creatorFilter} onChange={(e) => updateParams({ creator: e.target.value || null })}
            aria-label={t("works.filter_creator")}
            className="border rounded px-2 py-1.5 text-xs dark:bg-subtle dark:text-white dark:border-border">
            <option value="">{t("works.filter_all_creators")}</option>
            {creators.data?.items.map((c) => (
              <option key={c.id} value={c.id}>{c.display_name || c.name}</option>
            ))}
          </select>
        )}

        {/* NSFW filter */}
        <div className="flex gap-0.5 bg-subtle rounded p-0.5">
          {NSFW_FILTERS.map((f) => (
            <button key={f.key} onClick={() => updateParams({ nsfw: f.key === "all" ? null : f.key })}
              aria-label={f.label}
              className={`px-2.5 py-1 text-xs rounded transition-colors ${nsfwFilter === f.key ? "bg-surface shadow-sm font-medium" : "text-muted hover:text-fg"}`}>
              {f.label}
            </button>
          ))}
        </div>

        {/* Favorites filter */}
        <button onClick={() => updateParams({ fav: isFavoriteFilter ? null : "1" })}
          aria-label={t("works.filter_favorites")}
          className={`px-2.5 py-1 text-xs rounded transition-colors ${isFavoriteFilter ? "bg-yellow-100 dark:bg-yellow-900/30 text-yellow-700 font-medium" : "text-muted hover:text-fg"}`}>
          {"★"} {t("works.filter_favorites")}
        </button>

        {/* AI filter */}
        <div className="flex gap-0.5 bg-subtle rounded p-0.5">
          {[
            { key: "all", label: t("works.ai_filter_all") },
            { key: "human", label: t("works.ai_filter_human") },
            { key: "ai", label: t("works.ai_filter_ai") },
          ].map((f) => (
            <button key={f.key} onClick={() => updateParams({ ai: f.key === "all" ? null : f.key })}
              aria-label={f.label}
              className={`px-2.5 py-1 text-xs rounded transition-colors ${aiFilter === f.key ? "bg-surface shadow-sm font-medium" : "text-muted hover:text-fg"}`}>
              {f.label}
            </button>
          ))}
        </div>

        {/* Sort — click same field toggles direction */}
        <div className="flex gap-0.5 bg-subtle rounded p-0.5">
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
                aria-label={t("works.sort_by", { sort: s.label })}
                className={`px-2.5 py-1 text-xs rounded transition-colors ${active ? "bg-surface shadow-sm font-medium" : "text-muted hover:text-fg"}`}>
                {s.label} {active ? (dir === "desc" ? "↓" : "↑") : ""}
              </button>
            );
          })}
        </div>

        <div className="flex-1" />

        {activeFilterCount > 0 && (
          <button onClick={clearFilters} className="hidden rounded-md border border-border px-2.5 py-1.5 text-xs hover:bg-subtle dark:border-border dark:hover:bg-subtle md:inline-flex">
            {t("works.clear_filters")}
          </button>
        )}

        {/* View toggle */}
        <div className="flex gap-0.5 bg-subtle rounded p-0.5">
          <button onClick={() => updateParams({ view: null }, false)}
            aria-label={t("works.view_grid")}
            className={`px-2.5 py-1 rounded text-xs ${viewMode === "grid" ? "bg-surface shadow-sm" : "text-muted"}`}>
            {t("works.view_grid")}
          </button>
          <button onClick={() => updateParams({ view: "list" }, false)}
            aria-label={t("works.view_list")}
            className={`px-2.5 py-1 rounded text-xs ${viewMode === "list" ? "bg-surface shadow-sm" : "text-muted"}`}>
            {t("works.view_list")}
          </button>
        </div>
      </div>

      {works.data && works.data.items?.length > 0 && curationVisibility === "visible" && (
        selectedCount > 0 ? (
          <SelectionBar
            count={selectedCount}
            label={t("works.selected_count", { count: selectedCount })}
            clearLabel={t("works.clear_selection")}
            onClear={() => setSelectedWorkIds(new Set())}
          >
            <button onClick={toggleSelectPage} className="btn-ghost text-xs">
              {pageAllSelected ? t("works.deselect_page") : t("works.select_page")}
            </button>
            <button onClick={moveSelectedToTrash} disabled={batchTrash.isPending} className="btn-danger text-xs disabled:opacity-50">
              {batchTrash.isPending ? t("works.moving_to_trash") : t("works.move_to_trash")}
            </button>
          </SelectionBar>
        ) : (
          <div className="mb-4 flex flex-wrap items-center gap-2 rounded-md border border-border bg-white px-3 py-2 dark:border-border dark:bg-surface" aria-live="polite">
            <button onClick={toggleSelectPage} className="btn-ghost text-xs">
              {pageAllSelected ? t("works.deselect_page") : t("works.select_page")}
            </button>
          </div>
        )
      )}

      {/* Loading */}
      {works.isLoading && viewMode === "grid" && (
        <div className="overflow-x-auto grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-5 xl:grid-cols-6 gap-4">
          {Array.from({ length: 10 }).map((_, i) => (
            <div key={i} className="bg-surface rounded-lg shadow p-3 animate-pulse">
              <div className="h-32 bg-subtle rounded mb-2" />
              <div className="h-3 bg-subtle rounded w-3/4" />
            </div>
          ))}
        </div>
      )}
      {works.isLoading && viewMode === "list" && (
        <div className="space-y-1">{Array.from({ length: 8 }).map((_, i) => <div key={i} className="h-12 bg-subtle rounded animate-pulse" />)}</div>
      )}

      {/* Error */}
      {works.error && <ErrorState message={(works.error as Error).message} onRetry={() => works.refetch()} />}

      {/* Empty */}
      {works.data && !works.data.items?.length && (
        <EmptyState title={t("works.no_works")} description={search || sourceFilter || creatorFilter ? t("works.no_works_filter") : t("works.no_works_desc")} />
      )}

      {/* Grid View */}
      {works.data && works.data.items?.length > 0 && viewMode === "grid" && (
        <div className="overflow-x-auto grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-5 xl:grid-cols-6 gap-4 mb-6">
          {works.data.items.map((w: WorkListItem, i: number) => (
            <GridCard
              key={w.id}
              index={i}
              w={w}
              trashMode={curationVisibility === "trashed"}
              selectable={curationVisibility === "visible"}
              selected={selectedWorkIds.has(w.id)}
              onToggleSelect={toggleSelectWork}
              onToggleFavorite={(id) => toggleFavorite.mutate(id)}
              onRestore={(id) => restoreWork.mutate(id)}
              onPurge={(id) => {
                if (window.confirm(t("works.purge_confirm"))) purgeWork.mutate(id);
              }}
            />
          ))}
        </div>
      )}

      {/* List View */}
      {works.data && works.data.items?.length > 0 && viewMode === "list" && (
        <div className="space-y-1 mb-6">
          {works.data.items.map((w: WorkListItem) => (
            <div key={w.id} className={`bg-surface rounded-lg shadow-sm p-3 flex items-center gap-3 cursor-pointer hover:shadow-md transition-shadow ${selectedWorkIds.has(w.id) ? "ring-2 ring-accent dark:ring-accent" : ""}`} onClick={() => router.push(`/admin/works/${w.id}`)}>
              {curationVisibility === "visible" && (
                <input
                  type="checkbox"
                  checked={selectedWorkIds.has(w.id)}
                  onClick={(e) => e.stopPropagation()}
                  onChange={(e) => { e.stopPropagation(); toggleSelectWork(w.id); }}
                  aria-label={t("works.select_work")}
                  className="h-4 w-4 shrink-0 rounded border-border"
                />
              )}
              <div className="w-12 h-12 bg-subtle rounded overflow-hidden shrink-0">
                <Img assetId={w.thumbnail_asset_id} alt={w.title || ""} className="w-full h-full object-cover" />
              </div>
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-2">
                  <span className="text-sm font-medium dark:text-white truncate">{w.title || t("works.untitled")}</span>
                  {w.is_nsfw && <span className="text-xs bg-red-100 dark:bg-red-900/30 text-red-600 px-1 rounded">{t("works.nsfw_badge")}</span>}
                  {w.asset_count > 1 && <span className="text-xs text-muted">{w.asset_count}p</span>}
                  {w.has_ugoira && <span className="text-xs bg-purple-100 dark:bg-purple-900/30 text-purple-700 dark:text-purple-400 px-1 rounded">{t("works.gif_badge")}</span>}
                </div>
                <div className="flex items-center gap-2 text-xs text-muted mt-0.5">
                  {w.source && <SourceBadge source={w.source} href={`/admin/works?source=${w.source}`} />}
                  {w.creator_name && w.creator_id && (
  <Link href={`/admin/creators/${w.creator_id}`} onClick={(e) => e.stopPropagation()} className="text-blue-600 hover:underline">{w.creator_name}</Link>
)}
                  <span>{w.posted_at ? new Date(w.posted_at).toLocaleDateString() : "—"}</span>
                </div>
              </div>
              <button onClick={(e) => { e.stopPropagation(); toggleFavorite.mutate(w.id); }}
                className={`text-lg shrink-0 ${w.is_favorite ? "text-yellow-500" : "text-muted hover:text-yellow-400"}`}
                title={w.is_favorite ? t("works.unfavorite") : t("works.favorite")}
                aria-label={w.is_favorite ? t("works.unfavorite") : t("works.favorite")}>
                {w.is_favorite ? "★" : "☆"}
              </button>
              {curationVisibility === "trashed" && (
                <div className="flex shrink-0 gap-2">
                  <button onClick={(e) => { e.stopPropagation(); restoreWork.mutate(w.id); }} className="rounded border border-border px-2 py-1 text-xs hover:bg-subtle dark:border-border dark:hover:bg-subtle">{t("works.restore")}</button>
                  <button onClick={(e) => { e.stopPropagation(); if (window.confirm(t("works.purge_confirm"))) purgeWork.mutate(w.id); }} className="rounded bg-danger px-2 py-1 text-xs text-white hover:bg-danger">{t("works.purge")}</button>
                </div>
              )}
            </div>
          ))}
        </div>
      )}

      {/* Pagination */}
      {(works.data?.total ?? 0) > 0 && (
        <div className="flex gap-2 justify-center">
          <button disabled={page === 0} onClick={() => updateParams({ p: page <= 1 ? null : String(page - 1) }, false)} className="px-3 py-1 text-sm border rounded disabled:opacity-30 dark:border-border dark:text-muted">{t("works.prev")}</button>
          <span className="px-3 py-1 text-sm text-muted">{t("works.page").replace("{page}", String(page + 1))}</span>
          <button onClick={() => updateParams({ p: String(page + 1) }, false)} disabled={!works.data || (page + 1) * limit >= works.data.total} className="px-3 py-1 text-sm border rounded disabled:opacity-30 dark:border-border dark:text-muted">{t("works.next")}</button>
        </div>
      )}
    </PageShell>
  );
}

export default function WorksPage() {
  return (
    <Suspense>
      <WorksContent />
    </Suspense>
  );
}

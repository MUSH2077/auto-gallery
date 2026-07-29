"use client";
import { useState, useMemo, useEffect, useRef, Suspense } from "react";
import Link from "next/link";
import { useRouter, useSearchParams, usePathname } from "next/navigation";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useT } from "@/lib/i18n";
import { api, queryKeys, WorkListItem } from "@/lib/api";
import type { WorkAsset } from "@/lib/api/endpoints/works";
import { useAppearanceSettings } from "@/lib/appearance";
import { useStaggeredEntrance, type StaggeredEntranceProps } from "@/lib/motion";
import { AssetImage, PageHeader, EmptyState, ErrorState, SourceBadge, PageShell, SelectionBar, WorkPreviewOverlay, PermissionGuard, type SlideItem } from "@/components";
import { useSlideshow } from "@/lib/useSlideshow";
import { usePermissions } from "@/lib/usePermissions";
import { useI18nFormat } from "@/lib/i18n-format";
import { Star } from "lucide-react";

type PreviewState = {
  work: WorkListItem;
  anchor: DOMRect;
  assetIds: string[];
  pageIndex: number;
};

function WorkCard({
  w,
  onToggleFavorite,
  trashMode,
  onRestore,
  onPurge,
  selectable,
  selected,
  onToggleSelect,
  entrance,
  previewEnabled,
  previewDelayMs,
  wheelThreshold,
  onOpenPreview,
  onScheduleClosePreview,
  onCancelClosePreview,
  onPreviewPage,
  canCurate,
}: {
  w: WorkListItem;
  onToggleFavorite: (id: string) => void;
  trashMode?: boolean;
  onRestore?: (id: string) => void;
  onPurge?: (id: string) => void;
  selectable?: boolean;
  selected?: boolean;
  onToggleSelect?: (id: string) => void;
  entrance?: StaggeredEntranceProps;
  previewEnabled: boolean;
  previewDelayMs: number;
  wheelThreshold: number;
  onOpenPreview: (preview: PreviewState) => void;
  onScheduleClosePreview: () => void;
  onCancelClosePreview: () => void;
  onPreviewPage: (workId: string, pageIndex: number) => void;
  canCurate: boolean;
}) {
  const t = useT();
  const fmt = useI18nFormat();
  const cardRef = useRef<HTMLElement | null>(null);
  const hoverTimer = useRef<number | null>(null);
  const wheelDelta = useRef(0);
  const [pageIdx, setPageIdx] = useState(0);
  const assetIds = w.preview_asset_ids?.length ? w.preview_asset_ids : (w.thumbnail_asset_id ? [w.thumbnail_asset_id] : []);
  const hasMultiple = assetIds.length > 1;
  const currentId = assetIds[pageIdx] || assetIds[0];

  const updatePage = (next: number) => {
    if (!assetIds.length) return;
    const normalized = (next + assetIds.length) % assetIds.length;
    setPageIdx(normalized);
    onPreviewPage(w.id, normalized);
  };

  const openPreview = () => {
    if (!previewEnabled || !cardRef.current || !assetIds.length || window.matchMedia("(pointer: coarse)").matches) return;
    onOpenPreview({ work: w, anchor: cardRef.current.getBoundingClientRect(), assetIds, pageIndex: pageIdx });
  };

  const clearHoverTimer = () => {
    if (hoverTimer.current) window.clearTimeout(hoverTimer.current);
    hoverTimer.current = null;
  };

  return (
    <article
      ref={cardRef}
      className={`card-interactive relative ${entrance?.className || ""} overflow-hidden group ${selected ? "ring-2 ring-accent" : ""}`}
      style={entrance?.style}
      onMouseEnter={() => {
        onCancelClosePreview();
        clearHoverTimer();
        hoverTimer.current = window.setTimeout(openPreview, previewDelayMs);
      }}
      onMouseLeave={() => {
        clearHoverTimer();
        onScheduleClosePreview();
      }}
      onWheel={(event) => {
        if (!hasMultiple) return;
        wheelDelta.current += event.deltaY;
        if (Math.abs(wheelDelta.current) < wheelThreshold) return;
        event.preventDefault();
        updatePage(pageIdx + (wheelDelta.current > 0 ? 1 : -1));
        wheelDelta.current = 0;
      }}
    >
      <Link
        href={`/admin/works/${w.id}`}
        aria-label={t("common.open_item", { name: w.title || t("works.untitled") })}
        className="absolute inset-0 z-0 rounded-md focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent"
      />
      <div className="pointer-events-none relative z-10 flex h-32 items-center justify-center overflow-hidden bg-subtle text-xs text-muted">
        <AssetImage assetId={currentId} alt={w.title || ""} className="h-full w-full object-cover" fallback={t("works.na")} />
        {selectable && (
          <label className="pointer-events-auto absolute left-1 top-1 z-20 flex h-7 w-7 items-center justify-center rounded bg-black/60 text-white shadow-sm">
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
        {canCurate && (
          <button onClick={(e) => { e.stopPropagation(); onToggleFavorite(w.id); }}
            className={`pointer-events-auto absolute right-0 top-0 z-10 flex h-11 w-11 items-center justify-center text-base ${w.is_favorite ? "text-warning" : "text-white/60 hover:text-warning"} drop-shadow`}
            title={w.is_favorite ? t("works.unfavorite") : t("works.favorite")}
            aria-label={w.is_favorite ? t("works.unfavorite") : t("works.favorite")}>
            <Star className="h-5 w-5" fill={w.is_favorite ? "currentColor" : "none"} aria-hidden="true" />
          </button>
        )}
        {w.asset_count > 1 && (
          <span className="absolute bottom-1 left-1 bg-black/70 text-white text-xs px-1.5 py-0.5 rounded font-medium">{w.asset_count}p</span>
        )}
        {w.is_ai_generated && (
          <span className="absolute top-1 left-1 rounded bg-warning px-1.5 py-0.5 text-xs text-on-primary">{t("works.ai_badge")}</span>
        )}
        {w.has_ugoira && (
          <span className="absolute bottom-1 right-1 rounded bg-accent px-1.5 py-0.5 text-[10px] font-medium text-on-primary">{t("works.gif_badge")}</span>
        )}
        {trashMode && !selectable && (
          <span className="absolute left-1 top-1 rounded bg-danger/90 px-1.5 py-0.5 text-xs font-medium text-white">{t("works.trash_badge")}</span>
        )}
      </div>
      <div className="pointer-events-none relative z-10 p-3">
        <div className="text-sm font-medium truncate text-fg">{w.title || t("works.untitled")}</div>
        <div className="flex items-center gap-1.5 mt-1">
          {w.source && <SourceBadge source={w.source} href={`/admin/works?source=${w.source}`} />}
          {w.has_ugoira && <span className="rounded bg-accent-subtle px-1 text-[10px] text-accent">{t("works.gif_badge")}</span>}
          {w.creator_name && w.creator_id && (
  <Link href={`/admin/creators/${w.creator_id}`}
    className="pointer-events-auto relative z-10 truncate text-xs text-accent hover:underline">
    {w.creator_name}
  </Link>
)}
        </div>
        <div className="text-xs text-muted mt-0.5">
          {w.posted_at ? fmt.date(w.posted_at) : t("works.no_date")}
        </div>
        {trashMode && canCurate && (
          <div className="mt-3 flex gap-2">
            <button onClick={(e) => { e.stopPropagation(); onRestore?.(w.id); }} className="pointer-events-auto rounded border border-border px-2 py-1 text-xs hover:bg-subtle dark:border-border dark:hover:bg-subtle">{t("works.restore")}</button>
            <button onClick={(e) => { e.stopPropagation(); onPurge?.(w.id); }} className="pointer-events-auto rounded bg-danger px-2 py-1 text-xs text-white hover:bg-danger">{t("works.purge")}</button>
          </div>
        )}
      </div>
    </article>
  );
}

type SortKey = "created_at" | "posted_at" | "title";
type ViewMode = "grid" | "list";

function WorksContent() {
  const t = useT();
  const fmt = useI18nFormat();
  const router = useRouter();
  const qc = useQueryClient();
  const sp = useSearchParams();
  const pathname = usePathname();
  const { has } = usePermissions();
  const canCurate = has("curation");
  const { settings: appearance, updateSettings } = useAppearanceSettings();
  const [selectedWorkIds, setSelectedWorkIds] = useState<Set<string>>(new Set());
  const [filtersOpen, setFiltersOpen] = useState(false);
  const [preview, setPreview] = useState<PreviewState | null>(null);
  const closePreviewTimer = useRef<number | null>(null);
  const previewEnabled = appearance.workPreviewEnabled;
  const wheelThreshold = appearance.workPreviewWheelSensitivity === "relaxed" ? 120 : 70;

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
    { key: "weibo", label: t("works.source_weibo") },
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
  const limit = 30;

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

  const scheduleClosePreview = () => {
    if (closePreviewTimer.current) window.clearTimeout(closePreviewTimer.current);
    closePreviewTimer.current = window.setTimeout(() => setPreview(null), 140);
  };

  const cancelClosePreview = () => {
    if (closePreviewTimer.current) window.clearTimeout(closePreviewTimer.current);
    closePreviewTimer.current = null;
  };

  const setPreviewPreference = (enabled: boolean) => {
    updateSettings({ workPreviewEnabled: enabled });
    if (!enabled) setPreview(null);
  };

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

  const slideshow = useSlideshow();
  const slideItems: SlideItem[] = (works.data?.items || [])
    .filter((w): w is WorkListItem & { thumbnail_asset_id: string } => !!w.thumbnail_asset_id)
    .map((w) => ({ assetId: w.thumbnail_asset_id, workId: w.id, title: w.title, creatorName: w.creator_name }));

  const workItems = works.data?.items || [];
  const workEntrance = useStaggeredEntrance(workItems.map((work) => work.id));

  const previewAssets = useQuery({
    queryKey: ["works", preview?.work.id, "assets"],
    queryFn: () => api.getWorkAssets(preview!.work.id),
    enabled: !!preview && previewEnabled,
    staleTime: 60000,
  });

  useEffect(() => {
    if (!preview || !previewAssets.data?.length || !preview.assetIds.length) return;
    const byId = new Map<string, WorkAsset>(previewAssets.data.map((asset) => [asset.id, asset]));
    [preview.pageIndex, preview.pageIndex - 1, preview.pageIndex + 1].forEach((idx) => {
      const id = preview.assetIds[(idx + preview.assetIds.length) % preview.assetIds.length];
      const src = (id ? byId.get(id) : undefined)?.original_url;
      if (!src) return;
      const img = new Image();
      img.src = src;
    });
  }, [preview, previewAssets.data]);

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
      <PageHeader
        title={t("works.title")}
        description={t("works.count").replace("{count}", String(works.data?.total ?? 0))}
        secondaryActions={slideItems.length > 0 ? (
          <button type="button" onClick={() => slideshow.open(slideItems)} className="btn-ghost">
            {t("slideshow.open")}
          </button>
        ) : undefined}
      />

      {/* Search & Filters */}
      <div data-page-primary-content className="mb-3 flex flex-wrap items-center gap-2 md:hidden">
        <button
          onClick={() => setFiltersOpen((value) => !value)}
          className="btn-ghost"
          aria-expanded={filtersOpen}
          aria-controls="works-filter-panel"
        >
          {t("works.filters")} {activeFilterCount > 0 && <span className="ml-1 rounded-full bg-accent px-1.5 py-0.5 text-xs text-white">{activeFilterCount}</span>}
        </button>
        {activeFilterCount > 0 && (
          <button onClick={clearFilters} className="btn-ghost">
            {t("works.clear_filters")}
          </button>
        )}
      </div>

      <div data-page-primary-content id="works-filter-panel" className={`${filtersOpen ? "flex" : "hidden"} toolbar mb-4 flex-col md:flex md:flex-row md:flex-wrap md:items-center`}>
        <div className="segmented-control">
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
          placeholder={t("works.search_title")} className="input w-48 py-1.5 text-sm" />

        {/* Source filter — dropdown */}
        <select value={sourceFilter} onChange={(e) => updateParams({ source: e.target.value || null })}
          aria-label={t("works.filter_source")}
          className="select px-2 py-1.5 text-xs">
          {SOURCE_FILTERS.map((f) => (
            <option key={f.key} value={f.key}>{f.label}</option>
          ))}
        </select>

        {/* NSFW filter */}
        <div className="segmented-control">
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
          className={`px-2.5 py-1 text-xs rounded transition-colors ${isFavoriteFilter ? "bg-warning-subtle text-warning font-medium" : "text-muted hover:text-fg"}`}>
          <Star className="mr-1 inline h-3.5 w-3.5" fill={isFavoriteFilter ? "currentColor" : "none"} aria-hidden="true" />
          {t("works.filter_favorites")}
        </button>

        {/* AI filter */}
        <div className="segmented-control">
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
        <div className="segmented-control">
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

        {viewMode === "grid" && (
          <button
            onClick={() => setPreviewPreference(!previewEnabled)}
            className={`rounded-md border border-border px-2.5 py-1.5 text-xs transition-colors ${previewEnabled ? "bg-accent-subtle text-accent" : "bg-surface text-muted hover:bg-subtle"}`}
          >
            {previewEnabled ? t("works.preview_on") : t("works.preview_off")}
          </button>
        )}
      </div>

      {canCurate && works.data && works.data.items?.length > 0 && curationVisibility === "visible" && (
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
          <div className="mb-4 flex flex-wrap items-center gap-2 rounded-md border border-border bg-surface px-3 py-2" aria-live="polite">
            <button onClick={toggleSelectPage} className="btn-ghost text-xs">
              {pageAllSelected ? t("works.deselect_page") : t("works.select_page")}
            </button>
          </div>
        )
      )}

      {/* Loading */}
      {works.isLoading && viewMode === "grid" && (
        <div className="overflow-x-auto grid gap-4 grid-cols-2 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-5 xl:grid-cols-6">
          {Array.from({ length: 10 }).map((_, i) => (
            <div key={i} className="rounded-md bg-surface p-3 shadow-sm animate-pulse">
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
        <div className="overflow-x-auto grid gap-4 mb-6 grid-cols-2 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-5 xl:grid-cols-6">
          {works.data.items.map((w: WorkListItem, i: number) => (
            <WorkCard
              key={w.id}
              entrance={workEntrance(w.id, i)}
              w={w}
              previewEnabled={previewEnabled}
              previewDelayMs={appearance.workPreviewDelayMs}
              wheelThreshold={wheelThreshold}
              onOpenPreview={setPreview}
              onScheduleClosePreview={scheduleClosePreview}
              onCancelClosePreview={cancelClosePreview}
              onPreviewPage={(workId, pageIndex) => setPreview((current) => current?.work.id === workId ? { ...current, pageIndex } : current)}
              trashMode={curationVisibility === "trashed"}
              selectable={canCurate && curationVisibility === "visible"}
              selected={selectedWorkIds.has(w.id)}
              onToggleSelect={toggleSelectWork}
              onToggleFavorite={(id) => toggleFavorite.mutate(id)}
              onRestore={(id) => restoreWork.mutate(id)}
              onPurge={(id) => {
                if (window.confirm(t("works.purge_confirm"))) purgeWork.mutate(id);
              }}
              canCurate={canCurate}
            />
          ))}
        </div>
      )}

      {/* List View */}
      {works.data && works.data.items?.length > 0 && viewMode === "list" && (
        <div className="space-y-1 mb-6">
          {works.data.items.map((w: WorkListItem, index: number) => {
            const entrance = workEntrance(w.id, index);
            return (
            <div
              key={w.id}
              className={`${entrance.className} flex cursor-pointer items-center gap-3 rounded-md border border-border bg-surface p-3 shadow-sm transition-shadow hover:shadow-md ${selectedWorkIds.has(w.id) ? "ring-2 ring-accent" : ""}`}
              style={entrance.style}
              onClick={() => router.push(`/admin/works/${w.id}`)}
            >
              {canCurate && curationVisibility === "visible" && (
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
                <AssetImage assetId={w.thumbnail_asset_id} alt={w.title || ""} className="w-full h-full object-cover" fallback={t("works.na")} />
              </div>
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-2">
                  <span className="text-sm font-medium text-fg truncate">{w.title || t("works.untitled")}</span>
                  {w.is_nsfw && <span className="rounded bg-danger-subtle px-1 text-xs text-danger">{t("works.nsfw_badge")}</span>}
                  {w.asset_count > 1 && <span className="text-xs text-muted">{w.asset_count}p</span>}
                  {w.has_ugoira && <span className="rounded bg-accent-subtle px-1 text-xs text-accent">{t("works.gif_badge")}</span>}
                </div>
                <div className="flex items-center gap-2 text-xs text-muted mt-0.5">
                  {w.source && <SourceBadge source={w.source} href={`/admin/works?source=${w.source}`} />}
                  {w.creator_name && w.creator_id && (
  <Link href={`/admin/creators/${w.creator_id}`} onClick={(e) => e.stopPropagation()} className="text-accent hover:underline">{w.creator_name}</Link>
)}
                  <span>{w.posted_at ? fmt.date(w.posted_at) : "—"}</span>
                </div>
              </div>
              {canCurate && (
                <button onClick={(e) => { e.stopPropagation(); toggleFavorite.mutate(w.id); }}
                  className={`flex h-11 w-11 shrink-0 items-center justify-center rounded-md ${w.is_favorite ? "text-warning" : "text-muted hover:bg-subtle hover:text-warning"}`}
                  title={w.is_favorite ? t("works.unfavorite") : t("works.favorite")}
                  aria-label={w.is_favorite ? t("works.unfavorite") : t("works.favorite")}>
                  <Star className="h-5 w-5" fill={w.is_favorite ? "currentColor" : "none"} aria-hidden="true" />
                </button>
              )}
              {canCurate && curationVisibility === "trashed" && (
                <div className="flex shrink-0 gap-2">
                  <button onClick={(e) => { e.stopPropagation(); restoreWork.mutate(w.id); }} className="rounded border border-border px-2 py-1 text-xs hover:bg-subtle dark:border-border dark:hover:bg-subtle">{t("works.restore")}</button>
                  <button onClick={(e) => { e.stopPropagation(); if (window.confirm(t("works.purge_confirm"))) purgeWork.mutate(w.id); }} className="rounded bg-danger px-2 py-1 text-xs text-white hover:bg-danger">{t("works.purge")}</button>
                </div>
              )}
            </div>
            );
          })}
        </div>
      )}

      {preview && previewEnabled && (
        <WorkPreviewOverlay
          anchor={preview.anchor}
          title={preview.work.title}
          creatorName={preview.work.creator_name}
          source={preview.work.source}
          assetIds={preview.assetIds}
          assets={previewAssets.data}
          isLoading={previewAssets.isLoading || (previewAssets.isFetching && !previewAssets.data)}
          isError={previewAssets.isError}
          previewSize={appearance.workPreviewSize}
          pageIndex={preview.pageIndex}
          assetCount={preview.work.asset_count}
          onMouseEnter={cancelClosePreview}
          onMouseLeave={scheduleClosePreview}
          onWheelPage={(delta) => {
            if (preview.assetIds.length <= 1) return;
            setPreview((current) => {
              if (!current) return current;
              const next = (current.pageIndex + delta + current.assetIds.length) % current.assetIds.length;
              return { ...current, pageIndex: next };
            });
          }}
          onRefreshAssets={() => previewAssets.refetch()}
        />
      )}

      {/* Pagination */}
      {(works.data?.total ?? 0) > 0 && (
        <div className="flex gap-2 justify-center">
          <button disabled={page === 0} onClick={() => updateParams({ p: page <= 1 ? null : String(page - 1) }, false)} className="px-3 py-1 text-sm border rounded disabled:opacity-30 dark:border-border dark:text-muted">{t("works.prev")}</button>
          <span className="px-3 py-1 text-sm text-muted">{t("works.page").replace("{page}", String(page + 1))}</span>
          <button onClick={() => updateParams({ p: String(page + 1) }, false)} disabled={!works.data || (page + 1) * limit >= works.data.total} className="px-3 py-1 text-sm border rounded disabled:opacity-30 dark:border-border dark:text-muted">{t("works.next")}</button>
        </div>
      )}
      {slideshow.node}
    </PageShell>
  );
}

export default function WorksPage() {
  return (
    <PermissionGuard module="library">
      <Suspense>
        <WorksContent />
      </Suspense>
    </PermissionGuard>
  );
}

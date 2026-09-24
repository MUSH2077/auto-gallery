"use client";
import { useState, useEffect, useMemo, useRef, Suspense } from "react";
import dynamic from "next/dynamic";
import { useRouter, useSearchParams, usePathname } from "next/navigation";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useT } from "@/lib/i18n";
import { api, queryKeys, WorkListItem, type SearchQualifierToken, type SearchResponse } from "@/lib/api";
import type { MediaDerivativeProgress, WorkAsset } from "@/lib/api/endpoints/works";
import { useAppearanceSettings } from "@/lib/appearance";
import { useStaggeredEntrance } from "@/lib/motion";
import PageHeader from "@/components/PageHeader";
import EmptyState from "@/components/EmptyState";
import ErrorState from "@/components/ErrorState";
import PageShell from "@/components/PageShell";
import SelectionBar from "@/components/SelectionBar";
import {
  SmartSearchInput,
  type SearchComposeRequest,
  useSearchBatchComposer,
  useSearchComposer,
} from "@/components/SmartSearchInput";
import PermissionGuard from "@/components/PermissionGuard";
import type { SlideItem } from "@/components/SlideshowPlayer";
import { useSlideshow } from "@/lib/useSlideshow";
import { usePermissions } from "@/lib/usePermissions";
import { useI18nFormat } from "@/lib/i18n-format";
import { resolveMediaKind } from "@/lib/media";
import DomainDangerZone from "@/components/DomainDangerZone";
import { useToast } from "@/components/Toast";
import { pollInterval } from "@/lib/polling";
import { WorksControlSurface, WorksDisplayPanel } from "./WorksControlSurface";
import {
  WorksFilterPanel,
  type WorksFilterValue,
  type WorksMediaFilter,
} from "./WorksFilterPanel";
import { WorksSortPanel, type WorksSortValue } from "./WorksSortPanel";
import { WorkCard as SharedWorkCard, type WorkCardPreview } from "./WorkCard";
import { WorksLayout, WorksLayoutSkeleton } from "./WorksLayout";

const WorkPreviewOverlay = dynamic(
  () => import("@/components/work-interactions").then((module) => module.WorkPreviewOverlay),
  { ssr: false },
);

type PreviewState = WorkCardPreview;

const LEGACY_WORK_QUERY_KEYS = [
  "source",
  "creator",
  "creator_id",
  "tag",
  "nsfw",
  "fav",
  "favorite",
  "ai",
  "sort",
  "order",
  "filter",
  "search",
];

function stripLegacyWorkQuery(params: URLSearchParams): boolean {
  let changed = false;
  for (const key of LEGACY_WORK_QUERY_KEYS) {
    if (params.has(key)) {
      params.delete(key);
      changed = true;
    }
  }
  return changed;
}

function DerivativeProgressCard({
  progress,
  refreshing,
  onRefresh,
}: {
  progress?: MediaDerivativeProgress;
  refreshing: boolean;
  onRefresh: () => void;
}) {
  const t = useT();
  const fmt = useI18nFormat();
  if (!progress || progress.total === 0 || progress.remaining === 0) return null;

  const statusKey = progress.status === "stalled"
    ? "works.derivative_progress_stalled"
    : progress.status === "failed"
      ? "works.derivative_progress_failed"
      : progress.status === "waiting"
        ? "works.derivative_progress_waiting"
        : "works.derivative_progress_running";
  const percent = Math.min(100, Math.max(0, progress.completion_percent));
  const stalled = progress.status === "stalled";

  return (
    <section
      className={`mb-4 rounded-lg border p-4 ${stalled ? "border-warning/40 bg-warning-subtle" : "border-border bg-surface"}`}
      role="region"
      aria-label={t("works.derivative_progress_region")}
    >
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="text-sm font-semibold text-fg">{t("works.derivative_progress_title")}</p>
          <p className={`mt-0.5 text-xs font-medium ${stalled ? "text-warning" : "text-accent"}`} role="status">
            {t(statusKey)}
          </p>
        </div>
        <button
          type="button"
          className="btn-ghost shrink-0"
          onClick={onRefresh}
          disabled={refreshing}
          aria-label={t("works.derivative_progress_refresh")}
        >
          {refreshing ? t("common.refreshing") : t("works.derivative_progress_refresh")}
        </button>
      </div>
      <div
        className="mt-3 h-2 overflow-hidden rounded-full bg-border"
        role="progressbar"
        aria-label={t("works.derivative_progress_title")}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={Math.round(percent)}
      >
        <div
          className={`h-full w-full rounded-full transition-transform duration-slow ease-out ${stalled ? "bg-warning" : "bg-accent"}`}
          style={{ transform: `scaleX(${percent / 100})`, transformOrigin: "left" }}
        />
      </div>
      <div className="mt-2 flex flex-wrap items-center justify-between gap-x-4 gap-y-1 text-xs text-muted">
        <span className="font-mono tabular-nums text-fg">
          {t("works.derivative_progress_count", {
            completed: fmt.number(progress.completed),
            total: fmt.number(progress.total),
          })}
        </span>
        <span>
          {t("works.derivative_progress_remaining", {
            remaining: fmt.number(progress.remaining),
            works: fmt.number(progress.affected_works),
          })}
        </span>
        {progress.last_completed_at ? (
          <span>{t("works.derivative_progress_last", { time: fmt.relative(progress.last_completed_at) })}</span>
        ) : null}
      </div>
    </section>
  );
}

function WorksContent() {
  const t = useT();
  const router = useRouter();
  const qc = useQueryClient();
  const sp = useSearchParams();
  const pathname = usePathname();
  const { isAdmin, has } = usePermissions();
  const canCurate = has("curation");
  const toast = useToast();
  const { settings: appearance, updateSettings } = useAppearanceSettings();
  const [selectedWorkIds, setSelectedWorkIds] = useState<Set<string>>(new Set());
  const [preview, setPreview] = useState<PreviewState | null>(null);
  const closePreviewTimer = useRef<number | null>(null);
  const previewEnabled = appearance.workPreviewEnabled;
  const wheelThreshold = appearance.workPreviewWheelSensitivity === "relaxed" ? 120 : 70;
  // q is canonical search state; page, random seed, and the temporary legacy
  // view override remain separate navigation state.
  const search = sp.get("q") ?? "";
  const requestedPage = Number(sp.get("p") ?? "0");
  const page = Number.isSafeInteger(requestedPage) && requestedPage >= 0
    ? requestedPage
    : 0;
  const requestedSeed = Number(sp.get("seed"));
  const seed = sp.has("seed")
    && Number.isSafeInteger(requestedSeed)
    && requestedSeed >= 0
    && requestedSeed <= 0xffffffff
    ? requestedSeed
    : null;
  const legacyView = sp.get("view");
  const viewMode = legacyView === "grid" || legacyView === "list" || legacyView === "masonry"
    ? legacyView
    : appearance.worksViewMode;
  const limit = 30;
  const navigationParamsRef = useRef(sp.toString());
  useEffect(() => { navigationParamsRef.current = sp.toString(); }, [sp]);

  // Local input for search field — debounced 300ms before writing to URL
  const [inputVal, setInputVal] = useState(search);
  useEffect(() => { setInputVal(search); }, [search]);
  useEffect(() => {
    const next = new URLSearchParams(sp.toString());
    if (!stripLegacyWorkQuery(next)) return;
    router.replace(next.size ? `${pathname}?${next.toString()}` : pathname, { scroll: false });
  }, [pathname, router, sp]);
  useEffect(() => {
    if (inputVal === search) return;
    const timer = setTimeout(() => {
      const p = new URLSearchParams(navigationParamsRef.current);
      stripLegacyWorkQuery(p);
      if (inputVal) p.set("q", inputVal); else p.delete("q");
      p.delete("p");
      navigationParamsRef.current = p.toString();
      router.replace(`${pathname}?${p.toString()}`, { scroll: false });
    }, 300);
    return () => clearTimeout(timer);
  }, [inputVal, pathname, router, search]);

  function updateParams(
    updates: Record<string, string | null>,
    resetPage = true,
    history: "replace" | "push" = "replace",
  ) {
    const p = new URLSearchParams(navigationParamsRef.current);
    stripLegacyWorkQuery(p);
    for (const [k, v] of Object.entries(updates)) {
      if (v === null || v === "") p.delete(k); else p.set(k, v);
    }
    if (resetPage) p.delete("p");
    navigationParamsRef.current = p.toString();
    const href = `${pathname}?${p.toString()}`;
    if (history === "push") {
      router.push(href, { scroll: false });
    } else {
      router.replace(href, { scroll: false });
    }
  }

  function setSearchQuery(next: string) {
    setInputVal(next);
  }

  function clearSearch() {
    composer.discardPendingResult();
    setInputVal("");
    updateParams({ q: null, p: null, seed: null }, false);
  }

  const scheduleClosePreview = () => {
    if (closePreviewTimer.current) window.clearTimeout(closePreviewTimer.current);
    closePreviewTimer.current = window.setTimeout(() => setPreview(null), 140);
  };

  const cancelClosePreview = () => {
    if (closePreviewTimer.current) window.clearTimeout(closePreviewTimer.current);
    closePreviewTimer.current = null;
  };

  const worksQueryKey = [...queryKeys.works.all, "compound-search", page, search, seed] as const;
  const worksQuery = useQuery({
    queryKey: worksQueryKey,
    queryFn: ({ signal }) => {
      const previous = page > 0
        ? qc.getQueryData<SearchResponse>([
            ...queryKeys.works.all,
            "compound-search",
            page - 1,
            search,
            seed,
          ])
        : undefined;
      return api.search(
        search,
        page * limit,
        limit,
        "works",
        signal,
        previous?.next_cursor,
        seed,
      );
    },
    placeholderData: (previous) => previous,
    staleTime: 60_000,
  });
  const works = {
    ...worksQuery,
    data: worksQuery.data?.groups.works,
  };
  const derivativeProgress = useQuery({
    queryKey: queryKeys.works.derivativeProgress,
    queryFn: ({ signal }) => api.getMediaDerivativeProgress(signal),
    staleTime: 10_000,
    refetchInterval: (query) => (
      (query.state.data?.remaining ?? 0) > 0 ? pollInterval(true) : false
    ),
    refetchIntervalInBackground: false,
  });

  useEffect(() => {
    // placeholderData belongs to the previous page/query.  Its cursor must
    // never seed the next-page cache, otherwise a fast page/filter change can
    // cache page N under the key for page N+1 (or send a stale-query cursor).
    if (!worksQuery.data || worksQuery.isPlaceholderData) return;
    const total = worksQuery.data?.groups.works?.total ?? 0;
    const nextPage = page + 1;
    if (nextPage * limit >= total) return;
    void qc.prefetchQuery({
      queryKey: [...queryKeys.works.all, "compound-search", nextPage, search, seed],
      queryFn: ({ signal }) => api.search(
        search,
        nextPage * limit,
        limit,
        "works",
        signal,
        worksQuery.data?.next_cursor,
        seed,
      ),
      staleTime: 60_000,
    });
  }, [limit, page, qc, search, seed, worksQuery.data, worksQuery.isPlaceholderData]);

  const qualifierTokens = (worksQuery.data?.parsed.tokens || []).filter(
    (token): token is SearchQualifierToken => token.kind === "qualifier",
  );
  const qualifierValues = (key: string) => qualifierTokens.filter((token) => token.key === key && !token.negated).map((token) => token.value);
  const sourceFilter = qualifierValues("source")[0] || "";
  const creatorFilter = qualifierValues("creator")[0] || "";
  const isValues = qualifierValues("is");
  const nsfwFilter = isValues.includes("nsfw") ? "nsfw" : isValues.includes("sfw") ? "sfw" : "all";
  const isFavoriteFilter = isValues.includes("favorite");
  const aiFilter = isValues.includes("ai") ? "ai" : isValues.includes("human") ? "human" : "all";
  const curationVisibility = isValues.includes("trashed") ? "trashed" : "visible";
  const explicitSortValue = qualifierValues("sort")[0] || null;
  const activeFilterCount = qualifierTokens.filter((token) => !["type", "sort"].includes(token.key)).length;
  const composer = useSearchComposer({ value: inputVal, scope: "works", onChange: setSearchQuery });
  const batchComposer = useSearchBatchComposer({ value: inputVal, scope: "works", onChange: setSearchQuery });
  const sourceFilters = qualifierValues("source");
  const mediaFilters = qualifierValues("has").filter((value): value is WorksMediaFilter => (
    value === "image" || value === "animation" || value === "video" || value === "multiple-assets"
  ));
  const filterValue = useMemo<WorksFilterValue>(() => ({
    visibility: curationVisibility,
    sources: sourceFilters,
    safety: nsfwFilter,
    ai: aiFilter,
    favorite: isFavoriteFilter,
    media: mediaFilters,
  }), [aiFilter, curationVisibility, isFavoriteFilter, mediaFilters.join("\u0000"), nsfwFilter, sourceFilters.join("\u0000")]);
  const parsedHasText = !!worksQuery.data?.parsed.tokens.some((token) => token.kind === "text");
  const knownSorts: WorksSortValue[] = [
    "relevance", "heat-desc", "random", "created-desc", "created-asc", "posted-desc",
    "posted-asc", "updated-desc", "updated-asc", "title-desc", "title-asc",
  ];
  const defaultSort: WorksSortValue = parsedHasText ? "relevance" : "created-desc";
  const currentSort: WorksSortValue = explicitSortValue && knownSorts.includes(explicitSortValue as WorksSortValue)
    ? explicitSortValue as WorksSortValue
    : defaultSort;
  useEffect(() => {
    if (
      currentSort !== "random"
      || seed !== null
      || inputVal !== search
      || worksQuery.isPlaceholderData
      || worksQuery.data?.canonical_query !== search
      || worksQuery.data.seed === undefined
      || worksQuery.data.seed === null
    ) return;
    updateParams({ seed: String(worksQuery.data.seed) }, false);
  }, [currentSort, inputVal, search, seed, worksQuery.data?.canonical_query, worksQuery.data?.seed, worksQuery.isPlaceholderData]);
  useEffect(() => {
    if (
      seed === null
      || currentSort === "random"
      || inputVal !== search
      || worksQuery.isPlaceholderData
      || worksQuery.data?.canonical_query !== search
    ) return;
    updateParams({ seed: null }, false);
  }, [currentSort, inputVal, search, seed, worksQuery.data?.canonical_query, worksQuery.isPlaceholderData]);
  const sortSummary = currentSort === "heat-desc"
    ? t("works.sort_heat")
    : currentSort === "random"
      ? t("works.sort_random")
      : currentSort === "relevance"
        ? t("works.sort_relevance")
        : currentSort.startsWith("posted")
          ? t("works.sort_posted")
          : currentSort.startsWith("updated")
            ? t("works.sort_updated")
            : currentSort.startsWith("title")
              ? t("works.sort_title")
              : t("works.sort_imported");
  const displaySummary = `${t(`works.view_${viewMode}_plain`)} · ${t(`works.card_size_${appearance.workCardSize}`)}`;

  const applyFilterDraft = (next: WorksFilterValue, close: () => void) => {
    const operations: SearchComposeRequest[] = [
      { key: "is", value: next.visibility === "trashed" ? "trashed" : null, operation: "replace-group", replace_values: ["visible", "trashed"] },
      { key: "source", value: next.sources[0] || null, operation: "set" },
      ...next.sources.slice(1).map((value): SearchComposeRequest => ({ key: "source", value, operation: "add" })),
      { key: "is", value: next.safety === "all" ? null : next.safety, operation: "replace-group", replace_values: ["sfw", "nsfw"] },
      { key: "is", value: next.ai === "all" ? null : next.ai, operation: "replace-group", replace_values: ["human", "ai"] },
      { key: "is", value: next.favorite ? "favorite" : null, operation: "replace-group", replace_values: ["favorite"] },
      { key: "has", value: next.media[0] || null, operation: "replace-group", replace_values: ["image", "animation", "video", "multiple-assets"] },
      ...next.media.slice(1).map((value): SearchComposeRequest => ({ key: "has", value, operation: "add" })),
    ];
    batchComposer.mutate(operations, { onSuccess: close });
  };

  const selectSort = (next: WorksSortValue) => {
    updateParams({ seed: next === "random" ? String(crypto.getRandomValues(new Uint32Array(1))[0]) : null });
    composer.mutate({
      key: "sort",
      value: next === "created-desc" && !parsedHasText ? null : next,
      operation: "set",
    });
  };

  const updateDisplayAppearance = (patch: Parameters<typeof updateSettings>[0]) => {
    if (patch.worksViewMode) updateParams({ view: null }, false);
    updateSettings(patch);
  };
  const filters = search;

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
    refetchInterval: (query) => query.state.data?.some(
      (asset) => asset.derivative_status === "pending" || asset.derivative_status === "processing",
    ) ? pollInterval(true) : false,
    refetchIntervalInBackground: false,
  });
  const previewAssetIds = useMemo(
    () => previewAssets.data?.length
      ? previewAssets.data.slice(0, 10).map((asset) => asset.id)
      : preview?.assetIds || [],
    [preview?.assetIds, previewAssets.data],
  );

  useEffect(() => {
    if (!preview || !previewAssets.data?.length || !previewAssetIds.length) return;
    const byId = new Map<string, WorkAsset>(previewAssets.data.map((asset) => [asset.id, asset]));
    [preview.pageIndex, preview.pageIndex - 1, preview.pageIndex + 1].forEach((idx) => {
      const id = previewAssetIds[(idx + previewAssetIds.length) % previewAssetIds.length];
      const asset = id ? byId.get(id) : undefined;
      const src = asset
        ? resolveMediaKind(asset) === "video"
          ? asset.poster_url || asset.thumb_url
          : asset.original_url
        : undefined;
      if (!src) return;
      const img = new Image();
      img.src = src;
    });
  }, [preview, previewAssetIds, previewAssets.data]);

  useEffect(() => {
    setSelectedWorkIds(new Set());
  }, [page, filters]);

  useEffect(() => {
    if (appearance.workCardShowCheckbox || selectedWorkIds.size === 0) return;
    setSelectedWorkIds(new Set());
    toast.info(t("works.selection_hidden_cleared"));
  }, [appearance.workCardShowCheckbox, selectedWorkIds.size, t, toast]);

  const toggleFavorite = useMutation({
    mutationFn: (id: string) => api.toggleWorkFavorite(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: queryKeys.works.all }),
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
    mutationFn: async (ids: string[]) => {
      for (let start = 0; start < ids.length; start += 25) {
        const chunk = ids.slice(start, start + 25);
        await api.batchCurateWorks(
          chunk,
          "trash",
          "batch move to trash",
          `Move ${chunk.length} works to trash`,
        );
      }
    },
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

      <DerivativeProgressCard
        progress={derivativeProgress.data}
        refreshing={derivativeProgress.isFetching}
        onRefresh={() => { void derivativeProgress.refetch(); }}
      />

      {worksQuery.isFetching && !worksQuery.isLoading && (
        <div className="mb-2 h-0.5 w-full overflow-hidden rounded bg-subtle" role="status" aria-label={t("common.loading")}>
          <div className="h-full w-1/3 animate-pulse rounded bg-accent" />
        </div>
      )}

      <WorksControlSurface
        filterCount={activeFilterCount}
        sortSummary={sortSummary}
        displaySummary={displaySummary}
        search={(
          <SmartSearchInput
            value={inputVal}
            onChange={setInputVal}
            onEditStart={composer.discardPendingResult}
            onClear={clearSearch}
            scope="works"
            ariaLabel={t("works.search_title")}
            placeholder={t("works.search_title")}
            showTokens={false}
            className="w-full"
          />
        )}
        renderFilter={(close) => (
          <WorksFilterPanel
            value={filterValue}
            applying={batchComposer.isPending}
            onApply={(next) => applyFilterDraft(next, close)}
            onCancel={close}
          />
        )}
        renderSort={() => (
          <WorksSortPanel
            value={currentSort}
            hasText={parsedHasText}
            onChange={selectSort}
            onReshuffle={() => updateParams({ seed: String(crypto.getRandomValues(new Uint32Array(1))[0]) })}
          />
        )}
        renderDisplay={() => (
          <WorksDisplayPanel
            appearance={{ ...appearance, worksViewMode: viewMode }}
            updateAppearance={updateDisplayAppearance}
          />
        )}
      />

      {canCurate && appearance.workCardShowCheckbox && works.data && works.data.items?.length > 0 && curationVisibility === "visible" && (
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

      {works.isLoading ? <WorksLayoutSkeleton mode={viewMode} size={appearance.workCardSize} /> : null}

      {/* Error */}
      {works.error && <ErrorState message={(works.error as Error).message} onRetry={() => works.refetch()} />}

      {/* Empty */}
      {works.data && !works.data.items?.length && (
        <EmptyState title={t("works.no_works")} description={search || sourceFilter || creatorFilter ? t("works.no_works_filter") : t("works.no_works_desc")} />
      )}

      {works.data && works.data.items?.length > 0 ? (
        <WorksLayout mode={viewMode} size={appearance.workCardSize}>
          {works.data.items.map((work: WorkListItem, index: number) => (
            <SharedWorkCard
              key={work.id}
              entrance={workEntrance(work.id, index)}
              work={work}
              presentation={{
                layout: viewMode,
                size: appearance.workCardSize,
                showCheckbox: appearance.workCardShowCheckbox,
                showAi: appearance.workCardShowAi,
                showNsfw: appearance.workCardShowNsfw,
                showFavorite: appearance.workCardShowFavorite,
                blurNsfw: appearance.blurNsfw,
              }}
              previewEnabled={previewEnabled}
              previewDelayMs={appearance.workPreviewDelayMs}
              wheelThreshold={wheelThreshold}
              onOpenPreview={setPreview}
              onScheduleClosePreview={scheduleClosePreview}
              onCancelClosePreview={cancelClosePreview}
              onPreviewPage={(workId, pageIndex) => setPreview((current) => current?.work.id === workId ? { ...current, pageIndex } : current)}
              trashMode={curationVisibility === "trashed"}
              selectable={canCurate && curationVisibility === "visible"}
              selected={selectedWorkIds.has(work.id)}
              onToggleSelect={toggleSelectWork}
              onToggleFavorite={(id) => toggleFavorite.mutate(id)}
              onRestore={(id) => restoreWork.mutate(id)}
              onPurge={(id) => {
                if (window.confirm(t("works.purge_confirm"))) purgeWork.mutate(id);
              }}
              canCurate={canCurate}
              canPurge={isAdmin}
              eager={index < 8}
            />
          ))}
        </WorksLayout>
      ) : null}

      {preview && previewEnabled && (
        <WorkPreviewOverlay
          anchor={preview.anchor}
          title={preview.work.title}
          creatorName={preview.work.creator_name}
          source={preview.work.source}
          assetIds={previewAssetIds}
          assets={previewAssets.data}
          isLoading={previewAssets.isLoading || (previewAssets.isFetching && !previewAssets.data)}
          isError={previewAssets.isError}
          previewSize={appearance.workPreviewSize}
          pageIndex={preview.pageIndex}
          assetCount={preview.work.asset_count}
          blurred={preview.work.is_nsfw && appearance.blurNsfw}
          onMouseEnter={cancelClosePreview}
          onMouseLeave={scheduleClosePreview}
          onWheelPage={(delta) => {
            if (previewAssetIds.length <= 1) return;
            setPreview((current) => {
              if (!current) return current;
              const next = (current.pageIndex + delta + previewAssetIds.length) % previewAssetIds.length;
              return { ...current, pageIndex: next };
            });
          }}
          onRefreshAssets={() => previewAssets.refetch()}
        />
      )}

      {/* Pagination */}
      {(works.data?.total ?? 0) > 0 && (
        <div className="flex gap-2 justify-center">
          <button disabled={page === 0} onClick={() => updateParams({ p: page <= 1 ? null : String(page - 1) }, false, "push")} className="px-3 py-1 text-sm border rounded disabled:opacity-30 dark:border-border dark:text-muted">{t("works.prev")}</button>
          <span className="px-3 py-1 text-sm text-muted">{t("works.page").replace("{page}", String(page + 1))}</span>
          <button onClick={() => updateParams({ p: String(page + 1) }, false, "push")} disabled={!works.data || (page + 1) * limit >= works.data.total} className="px-3 py-1 text-sm border rounded disabled:opacity-30 dark:border-border dark:text-muted">{t("works.next")}</button>
        </div>
      )}
      {slideshow.node}
      <DomainDangerZone
        entity="works"
        title={t("datamgmt.danger_clear_works")}
        description={t("datamgmt.danger_clear_works_desc")}
      />
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

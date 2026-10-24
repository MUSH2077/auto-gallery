"use client";

import { useState, useEffect, useMemo, useRef } from 'react';
import { useRouter, useSearchParams, usePathname } from 'next/navigation';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { useT } from '@/lib/i18n';
import { api, queryKeys, type WorkListItem, type SearchQualifierToken, type SearchResponse } from '@/lib/api';
import type { WorkAsset } from '@/lib/api/endpoints/works';
import { useAppearanceSettings } from '@/lib/appearance';
import { useStaggeredEntrance } from '@/lib/motion';
import { useSearchBatchComposer, useSearchComposer, type SearchComposeRequest } from '@/components/SmartSearchInput';
import type { SlideItem } from '@/components/SlideshowPlayer';
import { useSlideshow } from '@/lib/useSlideshow';
import { usePermissions } from '@/lib/usePermissions';
import { resolveMediaKind } from '@/lib/media';
import { useToast } from '@/components/Toast';
import { pollInterval } from '@/lib/polling';
import type { WorksFilterValue, WorksMediaFilter, WorksSortValue, WorkCardPreview as PreviewState } from '@/features/works/types';

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


export function useWorksLibrary({ pageSize = 30 }: { pageSize?: number } = {}) {
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
  const limit = pageSize;
  const navigationParamsRef = useRef(sp.toString());
  useEffect(() => { navigationParamsRef.current = sp.toString(); }, [sp]);

  // Local input for search field — debounced 300ms before writing to URL
  const [inputVal, setInputVal] = useState(search);
  const [isComposing, setIsComposing] = useState(false);
  useEffect(() => { setInputVal(search); }, [search]);
  useEffect(() => {
    const next = new URLSearchParams(sp.toString());
    if (!stripLegacyWorkQuery(next)) return;
    router.replace(next.size ? `${pathname}?${next.toString()}` : pathname, { scroll: false });
  }, [pathname, router, sp]);
  useEffect(() => {
    if (isComposing || inputVal === search) return;
    const timer = setTimeout(() => {
      const p = new URLSearchParams(navigationParamsRef.current);
      stripLegacyWorkQuery(p);
      if (inputVal) p.set("q", inputVal); else p.delete("q");
      p.delete("p");
      navigationParamsRef.current = p.toString();
      router.replace(`${pathname}?${p.toString()}`, { scroll: false });
    }, 300);
    return () => clearTimeout(timer);
  }, [inputVal, isComposing, pathname, router, search]);

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

  const worksQueryKey = [...queryKeys.works.all, "compound-search", page, search, seed, limit] as const;
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
            limit,
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
      queryKey: [...queryKeys.works.all, "compound-search", nextPage, search, seed, limit],
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
  }, [page, filters, seed]);

  const queryTransitioning = isComposing || inputVal !== search || composer.isPending || batchComposer.isPending;
  useEffect(() => {
    if (queryTransitioning) setSelectedWorkIds(new Set());
  }, [queryTransitioning]);

  useEffect(() => {
    if (appearance.workCardShowCheckbox || selectedWorkIds.size === 0) return;
    setSelectedWorkIds(new Set());
    toast.info(t("works.selection_hidden_cleared"));
  }, [appearance.workCardShowCheckbox, selectedWorkIds.size, t, toast]);


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
    onSuccess: (_data, ids) => {
      setSelectedWorkIds(previous => new Set([...previous].filter(id => !ids.includes(id))));
      qc.invalidateQueries({ queryKey: queryKeys.works.all });
      qc.invalidateQueries({ queryKey: queryKeys.curation.all });
    },
  });

  const pageWorkIds = works.data?.items.map((w) => w.id) || [];
  const selectedCount = selectedWorkIds.size;
  const pageAllSelected = pageWorkIds.length > 0 && pageWorkIds.every((id) => selectedWorkIds.has(id));
  const toggleSelectWork = (id: string) => {
    if (queryTransitioning) return;
    setSelectedWorkIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });
  };
  const toggleSelectPage = () => {
    if (queryTransitioning) return;
    setSelectedWorkIds((prev) => {
      if (pageAllSelected) return new Set([...prev].filter((id) => !pageWorkIds.includes(id)));
      return new Set([...prev, ...pageWorkIds]);
    });
  };
  const moveSelectedToTrash = () => {
    if (queryTransitioning) return;
    const ids = [...selectedWorkIds];
    if (!ids.length) return;
    if (window.confirm(t("works.batch_trash_confirm", { count: ids.length }))) batchTrash.mutate(ids);
  };


 return { t, router, qc, sp, pathname, isAdmin, has, canCurate, toast, appearance, updateSettings, selectedWorkIds, setSelectedWorkIds, preview, setPreview, closePreviewTimer, previewEnabled, wheelThreshold, search, requestedPage, page, requestedSeed, seed, legacyView, viewMode, limit, navigationParamsRef, inputVal, setInputVal, isComposing, setIsComposing, updateParams, setSearchQuery, clearSearch, scheduleClosePreview, cancelClosePreview, worksQueryKey, worksQuery, works, derivativeProgress, qualifierTokens, qualifierValues, sourceFilter, creatorFilter, isValues, nsfwFilter, isFavoriteFilter, aiFilter, curationVisibility, explicitSortValue, activeFilterCount, composer, batchComposer, sourceFilters, mediaFilters, filterValue, parsedHasText, knownSorts, defaultSort, currentSort, sortSummary, displaySummary, applyFilterDraft, selectSort, updateDisplayAppearance, filters, queryTransitioning, slideshow, slideItems, workItems, workEntrance, previewAssets, previewAssetIds, restoreWork, purgeWork, batchTrash, pageWorkIds, selectedCount, pageAllSelected, toggleSelectWork, toggleSelectPage, moveSelectedToTrash };
}
export type WorksLibrary = ReturnType<typeof useWorksLibrary>;

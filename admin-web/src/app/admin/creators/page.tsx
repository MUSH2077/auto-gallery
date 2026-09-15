"use client";
import { useState, useMemo, useEffect, useCallback, useRef, Suspense } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api, queryKeys, type CreatorSearchHit, type SearchQualifierToken, type SearchResponse } from "@/lib/api";
import { PageHeader, PageSection, EmptyState, ErrorState, HierarchyDeletionDialog, Modal, FilterBar, SelectionBar, PageShell, PermissionGuard, EntityRow, RowActionMenu, SmartSearchInput, useSearchBatchComposer, CompactSelectionCheckbox, ReferenceSortControl, ReferenceNameRail, ReferenceListLayout, VirtualReferenceList, MatchedIdentityBadge, type VirtualReferenceListHandle, type VirtualReferenceListState, type VirtualReferencePage } from "@/components";
import { useNotifications } from "@/components/NotificationCenter";
import { useRouter, useSearchParams, usePathname } from "next/navigation";
import { useT } from "@/lib/i18n";
import { useI18nFormat } from "@/lib/i18n-format";
import { useToast } from "@/components/Toast";
import { usePermissions } from "@/lib/usePermissions";
import { Star } from "lucide-react";
import DomainDangerZone from "@/components/DomainDangerZone";
import {
  createReferenceListSession,
  legacyPageInitialIndex,
  readReferenceListSession,
  referenceNameAnchorsAvailable,
  referenceSessionStorageKey,
  referenceSortFromTokens,
} from "@/lib/reference-list-state";

type FilterMode = "all" | "active" | "inactive" | "has_danbooru" | "has_subscription" | "no_subscription" | "favorites";

function CreateForm({ isPending, error, onSubmit, onClose }: {
  isPending: boolean; error: Error | null;
  onSubmit: (data: { name: string; display_name?: string; description?: string }) => void;
  onClose: () => void;
}) {
  const t = useT();
  const toast = useToast();
  const [name, setName] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [description, setDescription] = useState("");
  const [urlInput, setUrlInput] = useState("");

  // Auto-detect name from pasted URL
  const handleUrlPaste = (val: string) => {
    setUrlInput(val);
    if (!name) {
      // Extract username from common URL patterns
      let detected = "";
      const m = val.match(/(?:pixiv\.net\/(?:en\/)?users\/|x\.com\/|twitter\.com\/|iwara\.tv\/users?\/|danbooru\.donmai\.us\/artists\/|weibo\.com\/(?:u\/|n\/|p\/)?|lofter\.com\/people\/|bilibili\.com\/)([\w.-]+)/);
      if (m) detected = m[1];
      // Also try pixiv artist ID
      if (!detected) {
        const m2 = val.match(/pixiv\.net\/(?:en\/)?users\/(\d+)/);
        if (m2) detected = "pixiv_" + m2[1];
      }
      if (detected && detected !== "home" && detected !== "n") setName(detected);
    }
  };

  return (
    <div className="space-y-4">
      <div>
        <label className="block text-sm font-medium mb-1">{t("creators.source_url_label")}</label>
        <input value={urlInput} onChange={(e) => handleUrlPaste(e.target.value)}
          className="input w-full font-mono"
          placeholder={t("creators.source_url_placeholder")} />
        <p className="mt-1 text-xs text-muted">{t("creators.source_url_hint")}</p>
      </div>
      <div className="grid grid-cols-2 gap-4">
        <div>
          <label className="block text-sm font-medium mb-1">{t("creators.name_label")} <span className="text-red-400">*</span></label>
          <input value={name} onChange={(e) => setName(e.target.value)} className="input w-full" placeholder={t("creators.name_placeholder")} />
        </div>
        <div>
          <label className="block text-sm font-medium mb-1">{t("creators.display_name_label")}</label>
          <input value={displayName} onChange={(e) => setDisplayName(e.target.value)} className="input w-full" placeholder={t("creators.display_name_placeholder")} />
        </div>
      </div>
      <div><label className="block text-sm font-medium mb-1">{t("creators.description_label")}</label><textarea value={description} onChange={(e) => setDescription(e.target.value)} className="textarea w-full" rows={2} /></div>
      <div className="flex justify-end gap-3 pt-2">
        <button onClick={onClose} className="btn-ghost">{t("creators.cancel")}</button>
        <button onClick={() => onSubmit({ name, display_name: displayName || undefined, description: description || undefined })} disabled={!name || isPending}
          className="btn-primary">
          {isPending ? t("creators.creating") : t("creators.create")}
        </button>
      </div>
      {error && <p className="text-sm text-danger dark:text-danger">{error.message}</p>}
    </div>
  );
}

function CreatorsContent() {
  const t = useT();
  const fmt = useI18nFormat();
  const toast = useToast();
  const router = useRouter();
  const qc = useQueryClient();
  const notify = useNotifications();
  const sp = useSearchParams();
  const pathname = usePathname();
  const { isAdmin, has, user } = usePermissions();
  const canCurate = has("curation");

  // Filter state derived from URL
  const search = sp.get("q") ?? "";
  const legacyPage = sp.get("p");
  const legacyInitialIndex = legacyPageInitialIndex(legacyPage);
  const queryFingerprint = `creators:${search}`;
  const sessionKey = referenceSessionStorageKey(user?.id ?? "current", pathname, queryFingerprint);
  const listRef = useRef<VirtualReferenceListHandle>(null);
  const initialVirtualIndexRef = useRef(legacyInitialIndex);
  const pendingLegacyScrollRef = useRef<number | null>(legacyPage === null ? null : legacyInitialIndex);

  const [showCreate, setShowCreate] = useState(false);
  const [deleteId, setDeleteId] = useState<string | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [confirmBatchDel, setConfirmBatchDel] = useState(false);
  const [deleteFiles, setDeleteFiles] = useState(false);
  const [searchMeta, setSearchMeta] = useState<SearchResponse | null>(null);
  const [listState, setListState] = useState<VirtualReferenceListState<CreatorSearchHit>>({
    total: 0,
    loadedItems: [],
    loadedOffsets: [],
    loadedPages: [],
    visibleOffsets: [],
  });
  const [storedSession, setStoredSession] = useState<{
    key: string;
    value: ReturnType<typeof readReferenceListSession>;
  } | null>(null);
  const restoredSession = storedSession?.key === sessionKey ? storedSession.value : null;
  const sessionReady = storedSession?.key === sessionKey;
  const previousQueryFingerprintRef = useRef(queryFingerprint);

  useEffect(() => {
    const queryChanged = previousQueryFingerprintRef.current !== queryFingerprint;
    previousQueryFingerprintRef.current = queryFingerprint;
    const saved = queryChanged
      ? null
      : readReferenceListSession(window.sessionStorage.getItem(sessionKey), queryFingerprint);
    setStoredSession({
      key: sessionKey,
      value: saved,
    });
    setSelected(new Set(saved?.selectedIds || []));
    if (queryChanged) {
      setSearchMeta(null);
      setListState({ total: 0, loadedItems: [], loadedOffsets: [], loadedPages: [], visibleOffsets: [] });
      window.scrollTo({ top: 0 });
    }
  }, [queryFingerprint, sessionKey]);

  const persistListSession = useCallback(() => {
    if (!sessionReady) return;
    window.sessionStorage.setItem(sessionKey, JSON.stringify(createReferenceListSession({
      queryFingerprint,
      scrollY: window.scrollY,
      loadedOffsets: listState.loadedOffsets,
      selectedIds: [...selected],
    })));
  }, [listState.loadedOffsets, queryFingerprint, selected, sessionKey, sessionReady]);

  useEffect(() => {
    window.addEventListener("pagehide", persistListSession);
    return () => window.removeEventListener("pagehide", persistListSession);
  }, [persistListSession]);

  const restoredScrollKeyRef = useRef<string | null>(null);
  useEffect(() => {
    if (!sessionReady || !listState.total || legacyPage !== null || restoredScrollKeyRef.current === sessionKey) return;
    restoredScrollKeyRef.current = sessionKey;
    const frame = window.requestAnimationFrame(() => window.scrollTo({ top: restoredSession?.scrollY || 0 }));
    return () => window.cancelAnimationFrame(frame);
  }, [legacyPage, listState.total, restoredSession?.scrollY, sessionKey, sessionReady]);

  // Local input for search field — debounced 300ms before writing to URL
  const [inputVal, setInputVal] = useState(search);
  useEffect(() => { setInputVal(search); }, [search]);
  useEffect(() => {
    if (inputVal === search) return;
    const timer = setTimeout(() => {
      setSelected(new Set());
      window.scrollTo({ top: 0 });
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

  const FILTERS: { key: FilterMode; label: string }[] = useMemo(() => [
    { key: "all", label: t("creators.filter_all") },
    { key: "active", label: t("creators.filter_active") },
    { key: "inactive", label: t("creators.filter_inactive") },
    { key: "has_danbooru", label: t("creators.filter_danbooru") },
    { key: "has_subscription", label: t("creators.filter_subscribed") },
    { key: "no_subscription", label: t("creators.filter_no_sub") },
    { key: "favorites", label: t("creators.filter_favorites") },
  ], [t]);

  const loadCreators = useCallback(async (
    offset: number,
    limit: number,
    signal?: AbortSignal,
  ): Promise<VirtualReferencePage<CreatorSearchHit, SearchResponse>> => {
    const response = await api.search(search, offset, limit, "creators", signal);
    return {
      items: response.groups.creators?.items || [],
      total: response.groups.creators?.total || 0,
      meta: response,
    };
  }, [search]);
  const parsedTokens = searchMeta?.query === search ? searchMeta.parsed.tokens : [];
  const qualifierTokens = parsedTokens.filter(
    (token): token is SearchQualifierToken => token.kind === "qualifier",
  );
  const isValues = qualifierTokens.filter((token) => token.key === "is" && !token.negated).map((token) => token.value);
  const hasTokens = qualifierTokens.filter((token) => token.key === "has");
  const filter: FilterMode = isValues.includes("active")
    ? "active"
    : isValues.includes("inactive")
      ? "inactive"
      : isValues.includes("favorite")
        ? "favorites"
        : hasTokens.some((token) => token.value === "danbooru" && !token.negated)
          ? "has_danbooru"
          : hasTokens.some((token) => token.value === "subscription" && token.negated)
            ? "no_subscription"
            : hasTokens.some((token) => token.value === "subscription" && !token.negated)
              ? "has_subscription"
              : "all";
  const sort = referenceSortFromTokens(parsedTokens);
  const anchorsAvailable = !!searchMeta && searchMeta.query === search && referenceNameAnchorsAvailable(parsedTokens);
  const anchors = useQuery({
    queryKey: ["reference-name-anchors", "creators", search],
    queryFn: ({ signal }) => api.referenceNameAnchors("creators", search, signal),
    enabled: anchorsAvailable,
  });
  const creatorItems = listState.loadedItems;
  const deletionPreview = useQuery({
    queryKey: ["deletion-preview", "creator", deleteId],
    queryFn: () => api.getCreatorDeletionPreview(deleteId as string),
    enabled: !!deleteId,
  });
  const batchDeletionPreview = useQuery({
    queryKey: ["deletion-preview", "creator", "batch", ...[...selected].sort()],
    queryFn: () => api.previewBatchDeleteCreators([...selected]),
    enabled: confirmBatchDel && selected.size > 0,
  });

  useEffect(() => {
    if (notify.operationJob?.kind !== "danbooru-import-all" || notify.operationJob.status !== "completed") return;
    qc.invalidateQueries({ queryKey: queryKeys.creators.all });
  }, [notify.operationJob?.jobId, notify.operationJob?.status]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (notify.batchJob?.status !== "completed") return;
    qc.invalidateQueries({ queryKey: queryKeys.creators.all });
  }, [notify.batchJob?.jobId, notify.batchJob?.status]); // eslint-disable-line react-hooks/exhaustive-deps

  const refreshCreatorViews = () => {
    qc.invalidateQueries({ queryKey: queryKeys.creators.all });
    qc.invalidateQueries({ queryKey: queryKeys.subscriptions.all });
  };

  const create = useMutation({
    mutationFn: (data: { name: string; display_name?: string; description?: string }) => api.createCreator(data),
    onSuccess: () => { setShowCreate(false); refreshCreatorViews(); toast.success({ message: t("notification.created") }); },
    onError: (e: Error) => toast.error({ message: e.message }),
  });

  const del = useMutation({
    mutationFn: (id: string) => api.deleteCreator(id, deleteFiles),
    onSuccess: (result) => {
      if (result.task_id) {
        notify.startOperationJob(result.task_id, "hierarchy-delete", t("deletion.permanent_title"), {
          entity: "hierarchy-delete", entity_type: "creator", entity_ids: result.entity_ids,
        });
        toast.success({ message: t("deletion.queued") });
      } else {
        toast.success({ message: t("deletion.soft_deleted") });
      }
      setDeleteId(null);
      setDeleteFiles(false);
      refreshCreatorViews();
    },
    onError: (error: Error) => toast.error({ message: error.message }),
  });

  const batchDel = useMutation({
    mutationFn: (ids: string[]) => api.batchDeleteCreators(ids, deleteFiles),
    onSuccess: (result) => {
      if (result.task_id) {
        notify.startOperationJob(result.task_id, "hierarchy-delete", t("deletion.permanent_title"), {
          entity: "hierarchy-delete", entity_type: "creator", entity_ids: result.entity_ids,
        });
        toast.success({ message: t("deletion.queued") });
      } else {
        toast.success({ message: t("deletion.soft_deleted") });
      }
      setSelected(new Set());
      setConfirmBatchDel(false);
      setDeleteFiles(false);
      refreshCreatorViews();
    },
    onError: (error: Error) => toast.error({ message: error.message }),
  });

  const restoreCreator = useMutation({
    mutationFn: (id: string) => api.curateCreator(id, "restore"),
    onSuccess: () => {
      refreshCreatorViews();
      toast.success({ message: t("notification.updated") });
    },
    onError: (error: Error) => toast.error({ message: error.message }),
  });

  const toggleSelect = (id: string) => {
    const next = new Set(selected);
    next.has(id) ? next.delete(id) : next.add(id);
    setSelected(next);
  };
  const selectAll = () => {
    const loadedIds = creatorItems.map((creator) => creator.id);
    const allLoadedSelected = loadedIds.length > 0 && loadedIds.every((id) => selected.has(id));
    setSelected((current) => {
      const next = new Set(current);
      if (allLoadedSelected) loadedIds.forEach((id) => next.delete(id));
      else loadedIds.forEach((id) => next.add(id));
      return next;
    });
  };

  const toggleFavorite = useMutation({
    mutationFn: (id: string) => api.toggleCreatorFavorite(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: queryKeys.creators.all }),
  });

  const setSearchQuery = (next: string) => {
    setSelected(new Set());
    window.scrollTo({ top: 0 });
    setInputVal(next);
    updateParams({ q: next || null });
  };
  const filterComposer = useSearchBatchComposer({ value: inputVal, scope: "creators", onChange: setSearchQuery });

  const handleFilterChange = (mode: FilterMode) => {
    const composes: Parameters<typeof filterComposer.mutate>[0] = [
      { key: "is", value: null, operation: "replace-group", replace_values: ["active", "inactive", "favorite"] },
      { key: "has", value: null, operation: "replace-group", replace_values: ["danbooru", "subscription"] },
    ];
    if (mode === "active" || mode === "inactive") {
      composes.push({ key: "is", value: mode, operation: "add" });
    } else if (mode === "favorites") {
      composes.push({ key: "is", value: "favorite", operation: "add" });
    } else if (mode === "has_danbooru") {
      composes.push({ key: "has", value: "danbooru", operation: "add" });
    } else if (mode === "has_subscription" || mode === "no_subscription") {
      composes.push({ key: "has", value: "subscription", negated: mode === "no_subscription", operation: "add" });
    }
    filterComposer.mutate(composes);
  };
  const handleSortChange = (value: string) => {
    setSelected(new Set());
    window.scrollTo({ top: 0 });
    filterComposer.mutate([{ key: "sort", value, operation: "set" }]);
  };

  const allLoadedSelected = creatorItems.length > 0
    && creatorItems.every((creator) => selected.has(creator.id));
  const someLoadedSelected = creatorItems.some((creator) => selected.has(creator.id));

  useEffect(() => {
    if (legacyPage === null || !listState.total) return;
    let cleanupFrame = 0;
    const scrollFrame = window.requestAnimationFrame(() => {
      listRef.current?.scrollToIndex(legacyInitialIndex);
      cleanupFrame = window.requestAnimationFrame(() => {
        initialVirtualIndexRef.current = 0;
        const params = new URLSearchParams(sp.toString());
        params.delete("p");
        const suffix = params.toString();
        router.replace(suffix ? `${pathname}?${suffix}` : pathname, { scroll: false });
      });
    });
    return () => {
      window.cancelAnimationFrame(scrollFrame);
      if (cleanupFrame) window.cancelAnimationFrame(cleanupFrame);
    };
  }, [legacyInitialIndex, legacyPage, listState.total, pathname, router, sp]);

  useEffect(() => {
    if (legacyPage !== null || !listState.total || pendingLegacyScrollRef.current === null) return;
    const index = pendingLegacyScrollRef.current;
    let settleFrame = 0;
    const scrollFrame = window.requestAnimationFrame(() => {
      settleFrame = window.requestAnimationFrame(() => {
        listRef.current?.scrollToIndex(index);
        pendingLegacyScrollRef.current = null;
      });
    });
    return () => {
      window.cancelAnimationFrame(scrollFrame);
      if (settleFrame) window.cancelAnimationFrame(settleFrame);
    };
  }, [legacyPage, listState.total]);

  return (
    <PageShell>
      <PageHeader
        title={t("creators.title")}
        description={t("creators.count").replace("{count}", String(listState.total))}
        secondaryActions={
          <button onClick={() => router.push("/admin/creators/duplicates")} className="btn-ghost">{t("creators.duplicates")}</button>
        }
        primaryAction={canCurate ? <button onClick={() => setShowCreate(true)} className="btn-primary">{t("creators.new")}</button> : undefined}
      />

      {/* Toolbar */}
      <div data-page-primary-content>
      <FilterBar>
        <SmartSearchInput
          value={inputVal}
          onChange={setInputVal}
          onEditStart={filterComposer.discardPendingResult}
          scope="creators"
          placeholder={t("creators.search")}
          ariaLabel={t("creators.search")}
          showTokens={false}
          className="w-full sm:w-72"
        />
        <div className="segmented-control max-w-full flex-wrap">
          {FILTERS.map((f) => (
            <button key={f.key} onClick={() => handleFilterChange(f.key)}
              className={`segment ${filter === f.key ? "segment-active" : ""}`}>
              {f.label}
            </button>
          ))}
        </div>
        <ReferenceSortControl
          value={sort}
          labels={{
            group: t("reference_list.sort_group"),
            name: t("reference_list.sort_name"),
            created: t("reference_list.sort_created"),
            updated: t("reference_list.sort_updated"),
            ascending: t("reference_list.ascending"),
            descending: t("reference_list.descending"),
          }}
          onChange={handleSortChange}
        />
      </FilterBar>
      </div>

      <PageSection>
      {canCurate && (
        <SelectionBar
          count={selected.size}
          label={(isAdmin ? t("creators.delete_selected") : t("deletion.archive_selected")).replace("{count}", String(selected.size))}
          clearLabel={t("common.clear")}
          onClear={() => setSelected(new Set())}
        >
          <button onClick={() => { setDeleteFiles(false); setConfirmBatchDel(true); }} className="btn-danger text-xs">
            {(isAdmin ? t("creators.delete_selected") : t("deletion.archive_selected")).replace("{count}", String(selected.size))}
          </button>
        </SelectionBar>
      )}

      {/* Select all */}
      {canCurate && creatorItems.length > 0 && (
        <label className="mb-2 flex cursor-pointer items-center gap-2 text-xs text-muted">
          <CompactSelectionCheckbox
            checked={allLoadedSelected}
            indeterminate={!allLoadedSelected && someLoadedSelected}
            ariaLabel={t("creators.select_all")}
            onChange={selectAll}
            stopPropagation={false}
          />
          {t("creators.select_all")}
          <span aria-live="polite">
            {t("reference_list.selected_loaded", { selected: selected.size, loaded: creatorItems.length })}
          </span>
        </label>
      )}

      <ReferenceListLayout
        rail={anchorsAvailable && Array.isArray(anchors.data?.items) ? (
          <ReferenceNameRail
            items={anchors.data.items}
            ariaLabel={t("reference_list.anchors_label")}
            jumpLabel={(label, count) => t("reference_list.anchor_jump", { label, count })}
            emptyLabel={(label) => t("reference_list.anchor_empty", { label })}
            onSelect={(anchor) => {
              if (anchor.offset !== null) listRef.current?.scrollToIndex(anchor.offset);
            }}
          />
        ) : undefined}
      >
        {!sessionReady ? (
          <div className="space-y-2">{Array.from({ length: 5 }).map((_, i) => <div key={i} className="h-16 rounded-md bg-subtle dark:bg-subtle animate-pulse" />)}</div>
        ) : (
          <VirtualReferenceList<CreatorSearchHit, SearchResponse>
            key={sessionKey}
            ref={listRef}
            queryKey={[...queryKeys.creators.all, "virtual", search]}
            loadPage={loadCreators}
            label={t("creators.title")}
            initialIndex={initialVirtualIndexRef.current}
            initialOffsets={restoredSession?.loadedOffsets}
            estimateSize={84}
            onStateChange={setListState}
            onMetaChange={setSearchMeta}
            renderInitialLoading={() => (
              <div className="space-y-2">{Array.from({ length: 5 }).map((_, i) => <div key={i} className="h-16 rounded-md bg-subtle dark:bg-subtle animate-pulse" />)}</div>
            )}
            renderInitialError={(error, retry) => <ErrorState message={error.message} onRetry={retry} />}
            renderPageError={(_offset, error, retry) => (
              <div className="flex min-h-20 items-center justify-center gap-3 rounded-md border border-danger/30 bg-danger-subtle px-4 text-sm text-danger">
                <span>{error.message}</span>
                <button type="button" className="btn-ghost text-xs" onClick={retry}>{t("common.retry")}</button>
              </div>
            )}
            renderPlaceholder={(index) => <div aria-label={t("reference_list.loading_row", { index: index + 1 })} className="h-20 animate-pulse rounded-md bg-subtle dark:bg-subtle" />}
            renderEmpty={() => (
              <EmptyState
                title={search || filter !== "all" ? t("works.no_works_filter") : t("creators.no_creators")}
                description={search || filter !== "all" ? undefined : t("creators.no_creators_desc")}
                action={(canCurate && !search && filter === "all") ? <button onClick={() => setShowCreate(true)} className="btn-primary">{t("creators.create_creator")}</button> : undefined}
              />
            )}
            renderItem={(c, index, total) => (
            <EntityRow
              key={c.id}
              label={t("common.open_item", { name: c.display_name || c.name })}
              selected={selected.has(c.id)}
              positionInSet={index + 1}
              setSize={total}
              onOpen={() => {
                persistListSession();
                router.push(`/admin/creators/${c.id}`);
              }}
            >
              {canCurate && (
                <CompactSelectionCheckbox
                  ariaLabel={t("common.select_item", { name: c.display_name || c.name })}
                  checked={selected.has(c.id)}
                  onChange={() => toggleSelect(c.id)}
                />
              )}
              <div className="entity-avatar">
                {(c.display_name || c.name).slice(0, 2).toUpperCase()}
              </div>
              <div className="entity-main">
                <div className="entity-title-line">
                  <span className="entity-title">{c.display_name || c.name}</span>
                  {c.display_name && <span className="truncate font-mono text-xs text-muted">{c.name}</span>}
                  <span
                    className={`h-1.5 w-1.5 shrink-0 rounded-full ${c.is_active ? "bg-success" : "bg-border"}`}
                    title={c.is_active ? t("creators.filter_active") : t("creators.filter_inactive")}
                  />
                  {(c as any).danbooru_artist_id && <span className="rounded-full bg-purple-100 px-2 py-0.5 font-mono text-[10px] text-purple-700 dark:bg-purple-900/30 dark:text-purple-400">D#{String((c as any).danbooru_artist_id)}</span>}
                  {(c.subscription_count ?? 0) > 0 && <span className="rounded-full bg-success-subtle px-2 py-0.5 text-[10px] text-success">{t("creators.sub_badge")}</span>}
                </div>
                {c.description && <p className="entity-supporting">{c.description}</p>}
                <MatchedIdentityBadge identity={c.matched_identity} />
                <div className="entity-meta">
                  <span>{t("creators.repository_count", { count: c.repository_count ?? 0 })}</span>
                  <span>{t("creators.source_count", { count: c.source_count ?? 0 })}</span>
                  <span>{t("creators.last_sync", { time: c.last_synced_at ? fmt.dateTime(c.last_synced_at) : t("common.never") })}</span>
                </div>
              </div>
              <div className="entity-actions" onClick={(event) => event.stopPropagation()}>
                {canCurate && (
                  <button
                    type="button"
                    onClick={() => toggleFavorite.mutate(c.id)}
                    className={`btn-icon text-lg ${c.is_favorite ? "text-warning" : "text-muted hover:text-warning"}`}
                    title={c.is_favorite ? t("common.unfavorite") : t("common.favorite")}
                    aria-label={c.is_favorite ? t("common.unfavorite") : t("common.favorite")}
                  >
                    <Star className="h-5 w-5" fill={c.is_favorite ? "currentColor" : "none"} aria-hidden="true" />
                  </button>
                )}
                {canCurate && (
                  <RowActionMenu
                    label={t("common.more_actions")}
                    items={[
                      {
                        label: !isAdmin && !c.is_active
                          ? t("creator_detail.restore")
                          : isAdmin
                            ? t("deletion.permanent_title")
                            : t("creator_detail.archive"),
                        tone: isAdmin || c.is_active ? "danger" : undefined,
                        onSelect: () => {
                          if (!isAdmin && !c.is_active) {
                            restoreCreator.mutate(c.id);
                            return;
                          }
                          setDeleteFiles(false);
                          setDeleteId(c.id);
                        },
                      },
                    ]}
                  />
                )}
              </div>
            </EntityRow>
            )}
          />
        )}
      </ReferenceListLayout>
      </PageSection>

      <Modal open={showCreate} onClose={() => setShowCreate(false)} title={t("creators.new_creator_title")}>
        <CreateForm isPending={create.isPending} error={create.error} onSubmit={(data) => create.mutate(data)} onClose={() => setShowCreate(false)} />
      </Modal>
      {deleteId && (
        <HierarchyDeletionDialog
          open
          title={isAdmin ? t("deletion.permanent_title") : t("creator_detail.archive")}
          confirmationPhrase={creatorItems.find((creator) => creator.id === deleteId)?.display_name || creatorItems.find((creator) => creator.id === deleteId)?.name || deleteId}
          preview={deletionPreview.data}
          previewLoading={deletionPreview.isLoading}
          deleteFiles={deleteFiles}
          onDeleteFilesChange={setDeleteFiles}
          onConfirm={() => del.mutate(deleteId)}
          onCancel={() => { setDeleteId(null); setDeleteFiles(false); }}
          isPending={del.isPending}
          error={(del.error as Error)?.message || (deletionPreview.error as Error)?.message}
        />
      )}
      {confirmBatchDel && (
        <HierarchyDeletionDialog
          open
          title={isAdmin ? t("deletion.permanent_title") : t("deletion.soft_title")}
          confirmationPhrase={String(selected.size)}
          preview={batchDeletionPreview.data}
          previewLoading={batchDeletionPreview.isLoading}
          deleteFiles={deleteFiles}
          onDeleteFilesChange={setDeleteFiles}
          onConfirm={() => batchDel.mutate([...selected])}
          onCancel={() => { setConfirmBatchDel(false); setDeleteFiles(false); }}
          isPending={batchDel.isPending}
          error={(batchDel.error as Error)?.message || (batchDeletionPreview.error as Error)?.message}
        />
      )}
      <DomainDangerZone
        entity="creators"
        title={t("datamgmt.danger_clear_creators")}
        description={t("datamgmt.danger_clear_creators_desc")}
      />
    </PageShell>
  );
}

export default function CreatorsPage() {
  return (
    <PermissionGuard module="library">
      <Suspense>
        <CreatorsContent />
      </Suspense>
    </PermissionGuard>
  );
}

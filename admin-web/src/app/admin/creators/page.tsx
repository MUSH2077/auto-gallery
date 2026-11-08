"use client";
import { useState, useMemo, useEffect, useCallback, useRef, useId, Suspense } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api, queryKeys, type CreatorSearchHit, type SearchQualifierToken, type SearchResponse } from "@/lib/api";
import PageSection from "@/components/PageSection";
import EmptyState from "@/components/EmptyState";
import ErrorState from "@/components/ErrorState";
import HierarchyDeletion from "@/features/gallery-ui/HierarchyDeletion";
import {ToolbarSelect} from "@/features/gallery-ui/Controls";
import SelectionBar from "@/components/SelectionBar";
import { BusinessDangerZone, BusinessModal, BusinessPage } from "@/features/gallery-ui/BusinessPage";
import { BusinessPermissionGuard as PermissionGuard } from "@/features/gallery-ui/BusinessPage";
import { EntityRow } from "@/components/EntityList";
import RowActionMenu from "@/components/RowActionMenu";
import { SmartSearchInput, useSearchBatchComposer } from "@/components/SmartSearchInput";
import CompactSelectionCheckbox from "@/components/CompactSelectionCheckbox";
import ReferenceNameRail, { ReferenceListLayout } from "@/components/ReferenceNameRail";
import VirtualReferenceList, {
  type VirtualReferenceListHandle,
  type VirtualReferenceListState,
  type VirtualReferencePage,
} from "@/components/VirtualReferenceList";
import MatchedIdentityBadge from "@/components/MatchedIdentityBadge";
import { useNotifications } from "@/components/NotificationCenter";
import { useRouter, useSearchParams, usePathname } from "next/navigation";
import { useT } from "@/lib/i18n";
import { useI18nFormat } from "@/lib/i18n-format";
import { useToast } from "@/components/Toast";
import { usePermissions } from "@/lib/usePermissions";
import { Star } from "lucide-react";
import { useCreatorFavorite } from "@/features/creators/useCreatorFavorite";
import {
  legacyPageInitialIndex,
  referenceNameAnchorsAvailable,
  referenceSortFromTokens,
} from "@/lib/reference-list-state";
import { useReferenceListLegacyNavigation, useReferenceListNavigation, useReferenceListSession, useReferenceListState } from "@/lib/useReferenceListNavigation";

function CreatorFavoriteButton({ creator }: { creator: CreatorSearchHit }) {
  const t = useT();
  const favorite = useCreatorFavorite(creator.id, creator.is_favorite);
  return <button type="button" onClick={favorite.toggle}
    className={`btn-icon text-lg ${favorite.isFavorite ? "text-danger" : "text-muted hover:text-danger"}`}
    title={favorite.isFavorite ? t("common.unfavorite") : t("common.favorite")}
    aria-label={favorite.isFavorite ? t("common.unfavorite") : t("common.favorite")}
    aria-pressed={favorite.isFavorite}>
    <Star className="h-5 w-5" fill={favorite.isFavorite ? "currentColor" : "none"} aria-hidden="true" />
  </button>;
}

type FilterMode = "all" | "active" | "inactive" | "has_danbooru" | "has_subscription" | "no_subscription" | "favorites";

function CreateForm({ isPending, error, onSubmit, onClose }: {
  isPending: boolean; error: Error | null;
  onSubmit: (data: { name: string; display_name?: string; description?: string }) => void;
  onClose: () => void;
}) {
  const t = useT();
  const toast = useToast();
  const formId = useId();
  const errorId = `${formId}-error`;
  const errorDescription = error ? errorId : undefined;
  const [name, setName] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [description, setDescription] = useState("");
  const [urlInput, setUrlInput] = useState("");
  const submittingRef = useRef(false);

  useEffect(() => {
    if (!isPending) submittingRef.current = false;
  }, [isPending]);

  const submit = () => {
    if (!name || isPending || submittingRef.current) return;
    submittingRef.current = true;
    onSubmit({ name, display_name: displayName || undefined, description: description || undefined });
  };

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
        <label htmlFor={`${formId}-url`} className="block text-sm font-medium mb-1">{t("creators.source_url_label")}</label>
        <input id={`${formId}-url`} aria-describedby={errorDescription} value={urlInput} onChange={(e) => handleUrlPaste(e.target.value)}
          className="input w-full font-mono"
          placeholder={t("creators.source_url_placeholder")} />
        <p className="mt-1 text-xs text-muted">{t("creators.source_url_hint")}</p>
      </div>
      <div className="grid grid-cols-2 gap-4">
        <div>
          <label htmlFor={`${formId}-name`} className="block text-sm font-medium mb-1">{t("creators.name_label")} <span className="text-red-400">*</span></label>
          <input id={`${formId}-name`} required aria-describedby={errorDescription} value={name} onChange={(e) => setName(e.target.value)} className="input w-full" placeholder={t("creators.name_placeholder")} />
        </div>
        <div>
          <label htmlFor={`${formId}-display-name`} className="block text-sm font-medium mb-1">{t("creators.display_name_label")}</label>
          <input id={`${formId}-display-name`} aria-describedby={errorDescription} value={displayName} onChange={(e) => setDisplayName(e.target.value)} className="input w-full" placeholder={t("creators.display_name_placeholder")} />
        </div>
      </div>
      <div><label htmlFor={`${formId}-description`} className="block text-sm font-medium mb-1">{t("creators.description_label")}</label><textarea id={`${formId}-description`} aria-describedby={errorDescription} value={description} onChange={(e) => setDescription(e.target.value)} className="textarea w-full" rows={2} /></div>
      <div className="flex justify-end gap-3 pt-2">
        <button type="button" onClick={onClose} className="btn-ghost">{t("creators.cancel")}</button>
        <button type="button" onClick={submit} disabled={!name || isPending}
          className="btn-primary">
          {isPending ? t("creators.creating") : t("creators.create")}
        </button>
      </div>
      {error && <p id={errorId} role="alert" className="text-sm text-danger dark:text-danger">{error.message}</p>}
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
  const listRef = useRef<VirtualReferenceListHandle>(null);
  const initialVirtualIndexRef = useRef(legacyInitialIndex);

  const [showCreate, setShowCreate] = useState(false);
  const [deleteId, setDeleteId] = useState<string | null>(null);
  const [confirmBatchDel, setConfirmBatchDel] = useState(false);
  const [deleteFiles, setDeleteFiles] = useState(false);
  const [searchMeta, setSearchMeta] = useState<SearchResponse | null>(null);
  const { listState, setListState, selected, selection, setSelected, onStateChange, restoreSelection, getSelectedOffsets } = useReferenceListState<CreatorSearchHit>({
    total: 0,
    loadedItems: [],
    loadedOffsets: [],
    loadedPages: [],
    visibleOffsets: [],
  });
  const paramsString = sp.toString();
  const { sessionKey, restoredSession, sessionReady, persistListSession } = useReferenceListSession({
    userId: user?.id ?? "current", pathname, queryFingerprint, legacyPage,
    total: listState.total, loadedOffsets: listState.requestedOffsets ?? listState.loadedOffsets, selected: selection, restoreSelection, getSelectedOffsets,
    onQueryChange: () => {
      setSearchMeta(null);
      setListState({ total: 0, loadedItems: [], loadedOffsets: [], loadedPages: [], visibleOffsets: [] });
    },
  });
  const { inputVal, setInputVal, updateParams } = useReferenceListNavigation({
    router, pathname, paramsString, search, setSelected,
  });
  useReferenceListLegacyNavigation({ legacyPage, legacyInitialIndex, total: listState.total, listRef, initialVirtualIndexRef, pathname, paramsString, router });

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
    setSelected((current) => {
      const next = new Set(current);
      next.has(id) ? next.delete(id) : next.add(id);
      return next;
    });
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

  return (
    <BusinessPage
      title={t("creators.title")}
      description={t("creators.count").replace("{count}", String(listState.total))}
      className="gallery-creators-page"
      actions={(
        <>
          <button onClick={() => router.push("/admin/creators/duplicates")} className="btn-ghost">{t("creators.duplicates")}</button>
          {canCurate && <button onClick={() => setShowCreate(true)} className="btn-primary">{t("creators.new")}</button>}
        </>
      )}
    >

      {/* Toolbar */}
      <div data-page-primary-content>
      <div className="business-toolbar" role="group" aria-label={t("creators.title")}>
        <SmartSearchInput
          value={inputVal}
          onChange={setInputVal}
          onEditStart={filterComposer.discardPendingResult}
          scope="creators"
          placeholder={t("creators.search")}
          ariaLabel={t("creators.search")}
          showTokens={false}
          className="business-search"
        />
        <ToolbarSelect label={t("works.filter_button")} value={filter} onValueChange={value=>handleFilterChange(value as FilterMode)} options={FILTERS.map(f=>({value:f.key,label:f.label}))}/>
        <ToolbarSelect label={t("reference_list.sort_group")} value={sort.field+"-"+sort.direction} onValueChange={handleSortChange} options={(["name","created","updated"] as const).flatMap(field=>(["asc","desc"] as const).map(direction=>({value:field+"-"+direction,label:t("reference_list.sort_"+field)+" · "+t(direction==="asc"?"reference_list.ascending":"reference_list.descending")})))}/>
      </div>
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
        rail={anchorsAvailable && anchors.data ? (
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
            onStateChange={onStateChange}
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
              deferOffscreen={false}
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
                  {c.display_name && c.display_name!==c.name && <span className="truncate font-mono text-xs text-muted">{c.name}</span>}
                  <span
                    className={`h-1.5 w-1.5 shrink-0 rounded-full ${c.is_active ? "bg-success" : "bg-border"}`}
                    title={c.is_active ? t("creators.filter_active") : t("creators.filter_inactive")}
                  />
                  {c.danbooru_artist_id && <span className="rounded-full border border-accent/40 bg-subtle px-2 py-0.5 font-mono text-accent">D#{String(c.danbooru_artist_id)}</span>}
                  {(c.subscription_count ?? 0) > 0 && <span className="rounded-full bg-success-subtle px-2 py-0.5 text-sm text-success">{t("creators.sub_badge")}</span>}
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
                  <CreatorFavoriteButton creator={c} />
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

      <BusinessModal open={showCreate} onClose={() => setShowCreate(false)} title={t("creators.new_creator_title")}>
        <CreateForm isPending={create.isPending} error={create.error} onSubmit={(data) => create.mutate(data)} onClose={() => setShowCreate(false)} />
      </BusinessModal>
      {deleteId && (
        <HierarchyDeletion
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
        <HierarchyDeletion
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
      <BusinessDangerZone
        entity="creators"
        title={t("datamgmt.danger_clear_creators")}
        description={t("datamgmt.danger_clear_creators_desc")}
      />
    </BusinessPage>
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

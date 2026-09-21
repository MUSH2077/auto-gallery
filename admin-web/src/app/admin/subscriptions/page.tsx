"use client";
import { useMemo, useState, useEffect, useCallback, useRef, Suspense } from "react";
import { useToast } from "@/components/Toast";
import { useT, type TFunction } from "@/lib/i18n";
import { useRouter, useSearchParams, usePathname } from "next/navigation";
import { useQuery, useQueries, useMutation, useQueryClient } from "@tanstack/react-query";
import { api, queryKeys, type SearchQualifierToken, type SearchResponse, type SubscriptionSearchHit, type SubscriptionSummary } from "@/lib/api";
import { PageHeader, PageSection, EmptyState, ErrorState, HierarchyDeletionDialog, Modal, StatusBadge, FilterBar, SelectionBar, PageShell, PermissionGuard, EntityRow, RowActionMenu, SmartSearchInput, useSearchBatchComposer, CompactSelectionCheckbox, ReferenceSortControl, ReferenceNameRail, ReferenceListLayout, VirtualReferenceList, MatchedIdentityBadge, type VirtualReferenceListHandle, type VirtualReferenceListState, type VirtualReferencePage } from "@/components";
import { useNotifications } from "@/components/NotificationCenter";
import { calendarScheduleRuleLabel, scheduleModeLabel, useI18nFormat } from "@/lib/i18n-format";
import { usePermissions } from "@/lib/usePermissions";
import DomainDangerZone from "@/components/DomainDangerZone";
import PaginatedCreatorSelect from "@/components/PaginatedCreatorSelect";
import {
  createReferenceListSession,
  legacyPageInitialIndex,
  readReferenceListSession,
  referenceNameAnchorsAvailable,
  referenceSessionStorageKey,
  referenceSortFromTokens,
} from "@/lib/reference-list-state";
import { pollInterval } from "@/lib/polling";

type FilterMode = "all" | "active" | "inactive" | "sync_on" | "sync_off" | "never_synced";

function subscriptionStatePresentation(t: TFunction, summary?: SubscriptionSummary) {
  const state = summary?.latest_state;
  if (!state) return { status: "unknown", label: t("subscriptions.state_loading") };
  if (state.state === "attention") {
    return { status: state.status || "failed", label: undefined };
  }
  if (state.state === "active") {
    return { status: state.status || "running", label: t("subscriptions.state_active") };
  }
  if (state.state === "success") {
    const outcome = state.outcome_code && ["no_changes", "new_content"].includes(state.outcome_code)
      ? t(`sync_outcome.${state.outcome_code}`)
      : null;
    return {
      status: "complete",
      label: outcome
        ? `${t("subscriptions.state_success")} · ${outcome}`
        : t("subscriptions.state_success"),
    };
  }
  if (state.state === "manual") return { status: "paused", label: t("subscriptions.manual") };
  if (state.state === "disabled") return { status: "down", label: t("subscriptions.filter_inactive") };
  return { status: "unknown", label: t("subscription_detail.never_synced") };
}

function CreateForm({ isPending, error, onSubmit, onClose }: {
  isPending: boolean; error: Error | null;
  onSubmit: (data: { creator_id: string; name?: string }) => void;
  onClose: () => void;
}) {
  const [creatorId, setCreatorId] = useState(""); const [name, setName] = useState("");
  const t = useT();
  return (
    <div className="space-y-4">
      <PaginatedCreatorSelect
        id="new-subscription-creator"
        label={t("subscriptions.creator_label")}
        value={creatorId}
        onChange={setCreatorId}
        placeholder={t("subscriptions.select_creator")}
      />
      <div><label htmlFor="new-subscription-label" className="block text-sm font-medium mb-1">{t("subscriptions.label_field")}</label><input id="new-subscription-label" value={name} onChange={(e) => setName(e.target.value)} className="input w-full" placeholder={t("subscriptions.label_placeholder")} /></div>
      <div className="flex justify-end gap-3 pt-2">
        <button onClick={onClose} className="btn-ghost">{t("subscriptions.cancel")}</button>
        <button onClick={() => onSubmit({ creator_id: creatorId, name: name || undefined })} disabled={!creatorId || isPending}
          className="btn-primary">
          {isPending ? t("subscriptions.creating") : t("subscriptions.subscribe")}
        </button>
      </div>
      {error && <p className="text-sm text-danger dark:text-danger">{error.message}</p>}
    </div>
  );
}

function SubscriptionsContent() {
  const router = useRouter();
  const t = useT();
  const fmt = useI18nFormat();
  const toast = useToast();
  const qc = useQueryClient();
  const notify = useNotifications();
  const { user } = usePermissions();
  const sp = useSearchParams();
  const pathname = usePathname();

  // Filter state derived from URL
  const search = sp.get("q") ?? "";
  const legacyPage = sp.get("p");
  const legacyInitialIndex = legacyPageInitialIndex(legacyPage);
  const queryFingerprint = `subscriptions:${search}`;
  const sessionKey = referenceSessionStorageKey(user?.id ?? "current", pathname, queryFingerprint);
  const listRef = useRef<VirtualReferenceListHandle>(null);
  const initialVirtualIndexRef = useRef(legacyInitialIndex);
  const pendingLegacyScrollRef = useRef<number | null>(legacyPage === null ? null : legacyInitialIndex);
  const [showCreate, setShowCreate] = useState(false);
  const [deleteId, setDeleteId] = useState<string | null>(null);
  const [deleteFiles, setDeleteFiles] = useState(false);
  const [syncingSubId, setSyncingSubId] = useState<string | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [searchMeta, setSearchMeta] = useState<SearchResponse | null>(null);
  const [listState, setListState] = useState<VirtualReferenceListState<SubscriptionSearchHit>>({
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
  const paramsString = sp.toString();
  const navigationParamsRef = useRef(paramsString);
  const committedParamsRef = useRef(paramsString);
  const pendingParamsRef = useRef(new Set<string>());

  useEffect(() => {
    if (committedParamsRef.current === paramsString) return;
    committedParamsRef.current = paramsString;
    if (pendingParamsRef.current.delete(paramsString)) {
      // An earlier replace can finish after the user has typed a new query.
      // Acknowledging our navigation must not replace that newer input.
      return;
    }
    pendingParamsRef.current.clear();
    navigationParamsRef.current = paramsString;
    setInputVal(search);
  }, [paramsString, search]);

  useEffect(() => {
    const restoreHistoryQuery = () => {
      const params = new URLSearchParams(window.location.search);
      pendingParamsRef.current.clear();
      navigationParamsRef.current = params.toString();
      setInputVal(params.get("q") ?? "");
    };
    window.addEventListener("popstate", restoreHistoryQuery);
    return () => window.removeEventListener("popstate", restoreHistoryQuery);
  }, []);

  const updateParams = useCallback((updates: Record<string, string | null>, resetPage = true) => {
    const p = new URLSearchParams(navigationParamsRef.current);
    for (const [k, v] of Object.entries(updates)) {
      if (v === null || v === "") p.delete(k); else p.set(k, v);
    }
    if (resetPage) p.delete("p");
    const next = p.toString();
    navigationParamsRef.current = next;
    pendingParamsRef.current.add(next);
    router.replace(`${pathname}?${next}`, { scroll: false });
  }, [pathname, router]);

  useEffect(() => {
    if (inputVal === search && (new URLSearchParams(navigationParamsRef.current).get("q") ?? "") === search) return;
    const timer = setTimeout(() => {
      setSelected(new Set());
      window.scrollTo({ top: 0 });
      updateParams({ q: inputVal || null });
    }, 300);
    return () => clearTimeout(timer);
  }, [inputVal, search, updateParams]);

  const FILTERS: { key: FilterMode; label: string }[] = [
    { key: "all", label: t("subscriptions.filter_all") },
    { key: "active", label: t("subscriptions.filter_active") },
    { key: "inactive", label: t("subscriptions.filter_inactive") },
    { key: "sync_on", label: t("subscriptions.filter_sync_on") },
    { key: "sync_off", label: t("subscriptions.filter_sync_off") },
    { key: "never_synced", label: t("subscriptions.filter_never") },
  ];

  const loadSubscriptions = useCallback(async (
    offset: number,
    limit: number,
    signal?: AbortSignal,
  ): Promise<VirtualReferencePage<SubscriptionSearchHit, SearchResponse>> => {
    const response = await api.search(search, offset, limit, "subscriptions", signal);
    return {
      items: response.groups.subscriptions?.items || [],
      total: response.groups.subscriptions?.total || 0,
      meta: response,
    };
  }, [search]);
  const parsedTokens = searchMeta?.query === search ? searchMeta.parsed.tokens : [];
  const isValues = parsedTokens
    .filter((token): token is SearchQualifierToken => token.kind === "qualifier" && token.key === "is" && !token.negated)
    .map((token) => token.value);
  const filter: FilterMode = isValues.includes("active")
    ? "active"
    : isValues.includes("inactive")
      ? "inactive"
      : isValues.includes("sync-enabled")
        ? "sync_on"
        : isValues.includes("sync-disabled")
          ? "sync_off"
          : isValues.includes("never-synced")
            ? "never_synced"
            : "all";
  const sort = referenceSortFromTokens(parsedTokens);
  const anchorsAvailable = !!searchMeta && searchMeta.query === search && referenceNameAnchorsAvailable(parsedTokens);
  const anchors = useQuery({
    queryKey: ["reference-name-anchors", "subscriptions", search],
    queryFn: ({ signal }) => api.referenceNameAnchors("subscriptions", search, signal),
    enabled: anchorsAvailable,
  });
  const subscriptionItems = listState.loadedItems;

  useEffect(() => {
    if (notify.operationJob?.kind !== "danbooru-import-all" || notify.operationJob.status !== "completed") return;
    qc.invalidateQueries({ queryKey: queryKeys.subscriptions.all });
  }, [notify.operationJob?.jobId, notify.operationJob?.status]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (notify.batchJob?.status !== "completed") return;
    qc.invalidateQueries({ queryKey: queryKeys.subscriptions.all });
  }, [notify.batchJob?.jobId, notify.batchJob?.status]); // eslint-disable-line react-hooks/exhaustive-deps
  const summaryQueries = useQueries({
    queries: listState.loadedPages.map((page) => {
      const ids = page.items.map((subscription) => subscription.id);
      const visible = listState.visibleOffsets.includes(page.offset);
      return {
        queryKey: queryKeys.subscriptions.summaries(ids),
        queryFn: () => api.subscriptionSummaries(ids),
        enabled: visible && ids.length > 0,
        refetchInterval: visible ? () => pollInterval(false) : (false as const),
        refetchIntervalInBackground: false,
        staleTime: 60_000,
      };
    }),
  });
  const summaryBySub = useMemo(
    () => new Map(summaryQueries.flatMap((query) => query.data?.items || []).map((item) => [item.subscription_id, item])),
    [summaryQueries],
  );

  const refreshSubscriptionViews = () => {
    qc.invalidateQueries({ queryKey: queryKeys.subscriptions.all });
    qc.invalidateQueries({ queryKey: queryKeys.creators.all });
  };

  const create = useMutation({
    mutationFn: (data: { creator_id: string; name?: string }) => api.createSubscription(data),
    onSuccess: () => { setShowCreate(false); refreshSubscriptionViews(); },
  });

  const [confirmBatchDel, setConfirmBatchDel] = useState(false);
  const deletionPreview = useQuery({
    queryKey: ["deletion-preview", "subscription", deleteId],
    queryFn: () => api.getSubscriptionDeletionPreview(deleteId as string),
    enabled: !!deleteId,
  });
  const batchDeletionPreview = useQuery({
    queryKey: ["deletion-preview", "subscription", "batch", ...[...selected].sort()],
    queryFn: () => api.previewBatchDeleteSubscriptions([...selected]),
    enabled: confirmBatchDel && selected.size > 0,
  });

  const del = useMutation({
    mutationFn: (id: string) => api.deleteSubscription(id, deleteFiles),
    onSuccess: (result) => {
      if (result.task_id) {
        notify.startOperationJob(result.task_id, "hierarchy-delete", t("subscriptions.remove_title"), {
          entity: "hierarchy-delete", entity_type: "subscription", entity_ids: result.entity_ids,
        });
        toast.success({ message: t("subscriptions.remove_queued") });
      } else {
        toast.success({ message: t("subscriptions.removed") });
      }
      setDeleteId(null);
      setDeleteFiles(false);
      refreshSubscriptionViews();
    },
    onError: (error: Error) => toast.error({ message: error.message }),
  });

  const syncNow = useMutation({
    mutationFn: (id: string) => api.syncNowSubscription(id),
    onMutate: (id) => setSyncingSubId(id),
    onSuccess: (data) => {
      refreshSubscriptionViews();
      qc.invalidateQueries({ queryKey: queryKeys.downloadJobs.all });
      qc.invalidateQueries({ queryKey: queryKeys.tasks.all });
      if (data.status === "error" || data.status === "partial_error") {
        toast.warning({
          title: t("subscriptions.sync_partial_failed"),
          message: (data as any).message || t("subscriptions.sync_partial_failed_desc"),
        });
      } else if (data.job_ids.length === 0) {
        toast.warning({ message: t("subscriptions.sync_no_jobs") });
      } else {
        toast.success({
          message: t("subscriptions.sync_result", { count: data.job_ids.length, skipped: data.skipped_count ?? 0 }),
          action: data.task_id ? { label: t("jobs.open_task"), onClick: () => router.push(`/admin/jobs?tab=admin&task=${data.task_id}`) } : undefined,
        });
      }
    },
    onError: (e: Error) => toast.error({ message: e.message }),
    onSettled: () => setSyncingSubId(null),
  });

  const batchDel = useMutation({
    mutationFn: (ids: string[]) => api.batchDeleteSubscriptions(ids, deleteFiles),
    onSuccess: (result) => {
      if (result.task_id) {
        notify.startOperationJob(result.task_id, "hierarchy-delete", t("subscriptions.remove_title"), {
          entity: "hierarchy-delete", entity_type: "subscription", entity_ids: result.entity_ids,
        });
        toast.success({ message: t("subscriptions.remove_queued") });
      } else {
        toast.success({ message: t("subscriptions.removed") });
      }
      setSelected(new Set());
      setConfirmBatchDel(false);
      setDeleteFiles(false);
      refreshSubscriptionViews();
    },
    onError: (error: Error) => toast.error({ message: error.message }),
  });

  const restoreSubscription = useMutation({
    mutationFn: (id: string) => api.updateSubscription(id, { is_active: true }),
    onSuccess: () => {
      refreshSubscriptionViews();
      toast.success({ message: t("notification.updated") });
    },
    onError: (error: Error) => toast.error({ message: error.message }),
  });

  const batchSync = useMutation({
    mutationFn: (params: { ids: string[]; enable: boolean }) => api.batchToggleSyncSubscriptions(params.ids, params.enable),
    onSuccess: () => { setSelected(new Set()); refreshSubscriptionViews(); toast.success({ message: t("notification.updated") }); },
  });

  const toggleSelect = (id: string) => {
    const next = new Set(selected);
    next.has(id) ? next.delete(id) : next.add(id);
    setSelected(next);
  };
  const selectAll = () => {
    const loadedIds = subscriptionItems.map((subscription) => subscription.id);
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
  const filterComposer = useSearchBatchComposer({ value: inputVal, scope: "subscriptions", onChange: setSearchQuery });
  const handleFilterChange = (mode: FilterMode) => {
    const value = {
      active: "active",
      inactive: "inactive",
      sync_on: "sync-enabled",
      sync_off: "sync-disabled",
      never_synced: "never-synced",
    }[mode as Exclude<FilterMode, "all">];
    filterComposer.mutate([
      {
        key: "is",
        value: value || null,
        operation: "replace-group",
        replace_values: ["active", "inactive", "sync-enabled", "sync-disabled", "never-synced"],
      },
    ]);
  };
  const handleSortChange = (value: string) => {
    setSelected(new Set());
    window.scrollTo({ top: 0 });
    filterComposer.mutate([{ key: "sort", value, operation: "set" }]);
  };

  const allLoadedSelected = subscriptionItems.length > 0
    && subscriptionItems.every((subscription) => selected.has(subscription.id));
  const someLoadedSelected = subscriptionItems.some((subscription) => selected.has(subscription.id));

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
        title={t("subscriptions.title")}
        description={t("subscriptions.count").replace("{count}", String(listState.total))}
        primaryAction={<button onClick={() => setShowCreate(true)} className="btn-primary">{t("subscriptions.new")}</button>}
      />

      {/* Toolbar */}
      <div data-page-primary-content>
      <FilterBar>
        <SmartSearchInput
          value={inputVal}
          onChange={setInputVal}
          onEditStart={filterComposer.discardPendingResult}
          scope="subscriptions"
          placeholder={t("subscriptions.search")}
          ariaLabel={t("subscriptions.search")}
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
      <SelectionBar
        count={selected.size}
        label={t("subscriptions.remove_selected", { count: selected.size })}
        clearLabel={t("common.clear")}
        onClear={() => setSelected(new Set())}
      >
        <button onClick={() => batchSync.mutate({ ids: [...selected], enable: true })} disabled={batchSync.isPending}
          className="btn-primary text-xs disabled:opacity-50">{t("subscriptions.enable_sync")}</button>
        <button onClick={() => batchSync.mutate({ ids: [...selected], enable: false })} disabled={batchSync.isPending}
          className="btn-ghost text-xs disabled:opacity-50">{t("subscriptions.disable_sync")}</button>
        <button onClick={() => { setDeleteFiles(false); setConfirmBatchDel(true); }} className="btn-danger text-xs">
          {t("subscriptions.remove_selected", { count: selected.size })}
        </button>
      </SelectionBar>

      {/* Select all */}
      {subscriptionItems.length > 0 && (
        <label className="mb-2 flex cursor-pointer items-center gap-2 text-xs text-muted">
          <CompactSelectionCheckbox
            checked={allLoadedSelected}
            indeterminate={!allLoadedSelected && someLoadedSelected}
            ariaLabel={t("subscriptions.select_all")}
            onChange={selectAll}
            stopPropagation={false}
          />
          {t("subscriptions.select_all")}
          <span aria-live="polite">
            {t("reference_list.selected_loaded", { selected: selected.size, loaded: subscriptionItems.length })}
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
          <VirtualReferenceList<SubscriptionSearchHit, SearchResponse>
            key={sessionKey}
            ref={listRef}
            queryKey={[...queryKeys.subscriptions.all, "virtual", search]}
            loadPage={loadSubscriptions}
            label={t("subscriptions.title")}
            initialIndex={initialVirtualIndexRef.current}
            initialOffsets={restoredSession?.loadedOffsets}
            estimateSize={132}
            onStateChange={setListState}
            onMetaChange={setSearchMeta}
            renderInitialLoading={() => (
              <div className="space-y-2">{Array.from({ length: 5 }).map((_, i) => <div key={i} className="h-24 rounded-md bg-subtle dark:bg-subtle animate-pulse" />)}</div>
            )}
            renderInitialError={(error, retry) => <ErrorState message={error.message} onRetry={retry} />}
            renderPageError={(_offset, error, retry) => (
              <div className="flex min-h-28 items-center justify-center gap-3 rounded-md border border-danger/30 bg-danger-subtle px-4 text-sm text-danger">
                <span>{error.message}</span>
                <button type="button" className="btn-ghost text-xs" onClick={retry}>{t("common.retry")}</button>
              </div>
            )}
            renderPlaceholder={(index) => <div aria-label={t("reference_list.loading_row", { index: index + 1 })} className="h-28 animate-pulse rounded-md bg-subtle dark:bg-subtle" />}
            renderEmpty={() => (
              <EmptyState
                title={search || filter !== "all" ? t("works.no_works_filter") : t("subscriptions.no_subs")}
                description={search || filter !== "all" ? undefined : t("subscriptions.no_subs_desc")}
                action={!search && filter === "all" ? <button onClick={() => setShowCreate(true)} className="btn-primary">{t("subscriptions.create_sub")}</button> : undefined}
              />
            )}
            renderItem={(s, index, total) => {
            const name = s.name || s.creator_display_name || s.creator_name || s.creator_id.slice(0, 8);
            const creatorName = s.creator_display_name || s.creator_name || s.creator_id.slice(0, 8);
            const summary = summaryBySub.get(s.id);
            const blocked = summary?.schedule.blocked_sources || 0;
            const due = summary?.schedule.due_sources || 0;
            const schedule = summary?.schedule;
            const calendarMode = schedule?.effective_mode === "calendar" || schedule?.effective_mode === "fixed_time";
            const scheduleValue = calendarMode
              ? `${scheduleModeLabel(t, schedule?.effective_mode)} · ${calendarScheduleRuleLabel(t, schedule?.schedule_rule, schedule?.scheduled_times)}`
              : schedule?.effective_mode === "manual"
                ? t("subscriptions.manual")
                : t("subscriptions.schedule_interval", { hours: schedule?.sync_interval_hours || s.sync_interval_hours });
            const scheduleLabel = schedule?.inherited
              ? t("subscriptions.schedule_inherited", { schedule: scheduleValue })
              : scheduleValue;
            const statePresentation = subscriptionStatePresentation(t, summary);
            return (
              <EntityRow
                key={s.id}
                label={t("common.open_item", { name })}
                selected={selected.has(s.id)}
                positionInSet={index + 1}
                setSize={total}
                onOpen={() => {
                  persistListSession();
                  router.push(`/admin/subscriptions/${s.id}`);
                }}
              >
                <CompactSelectionCheckbox
                  ariaLabel={t("common.select_item", { name })}
                  checked={selected.has(s.id)}
                  onChange={() => toggleSelect(s.id)}
                />
                <div className="entity-avatar">
                  {creatorName.trim().slice(0, 2).toUpperCase()}
                </div>
                <div className="entity-main">
                  <div className="entity-title-line">
                    <span className="entity-title">{name}</span>
                    <span
                      className={`h-1.5 w-1.5 shrink-0 rounded-full ${s.is_active ? "bg-success" : "bg-border"}`}
                      title={s.is_active ? t("subscriptions.filter_active") : t("subscriptions.filter_inactive")}
                    />
                    <StatusBadge
                      status={statePresentation.status}
                      label={statePresentation.label}
                      className="py-0 text-[10px]"
                    />
                    {due > 0 && <span className="rounded-full bg-accent-subtle px-2 py-0.5 text-[10px] font-medium text-accent">{t("subscriptions.due_sources", { count: due })}</span>}
                    {blocked > 0 && <span className="rounded-full bg-danger-subtle px-2 py-0.5 text-[10px] font-medium text-danger">{t("subscriptions.blocked_sources", { count: blocked })}</span>}
                  </div>
                  <div className="entity-supporting">
                    {t("subscriptions.creator_prefix")}{" "}
                    <button
                      type="button"
                      className="text-accent hover:underline"
                      onClick={(event) => {
                        event.stopPropagation();
                        router.push(`/admin/creators/${s.creator_id}`);
                      }}
                    >
                      {creatorName}
                    </button>
                  </div>
                  <MatchedIdentityBadge identity={s.matched_identity} />
                  <div className="entity-meta">
                    <span>{t("subscriptions.sources_summary", { enabled: summary?.enabled_source_count ?? s.enabled_source_count ?? 0, total: summary?.source_count ?? s.source_count ?? 0 })}</span>
                    <span>{t("subscriptions.last_success", { time: fmt.relative(s.last_synced_at, "subscriptions.never") })}</span>
                    <span>{scheduleLabel}</span>
                    <span>{t("subscriptions.next_due", { time: fmt.dateTime(schedule?.next_due_at) })}</span>
                    {s.sync_enabled && (summary?.enabled_source_count ?? s.enabled_source_count ?? 0) === 0 && (
                      <span className="text-warning">{t("subscriptions.no_enabled_source")}</span>
                    )}
                  </div>
                </div>
                <div className="entity-actions" onClick={(event) => event.stopPropagation()}>
                  <button
                    type="button"
                    onClick={() => syncNow.mutate(s.id)}
                    disabled={syncNow.isPending || !s.is_active}
                    className="btn-primary text-xs"
                  >
                    {syncingSubId === s.id ? t("subscriptions.syncing") : t("subscriptions.sync_all")}
                  </button>
                  <RowActionMenu
                    label={t("common.more_actions")}
                    items={[
                      {
                        label: !s.is_active
                          ? t("creator_detail.restore")
                          : t("subscriptions.remove_title"),
                        tone: s.is_active ? "danger" : undefined,
                        onSelect: () => {
                          if (!s.is_active) {
                            restoreSubscription.mutate(s.id);
                            return;
                          }
                          setDeleteFiles(false);
                          setDeleteId(s.id);
                        },
                      },
                    ]}
                  />
                </div>
              </EntityRow>
            );
            }}
          />
        )}
      </ReferenceListLayout>
      </PageSection>

      <Modal open={showCreate} onClose={() => setShowCreate(false)} title={t("subscriptions.new_sub_title")}>
        <CreateForm isPending={create.isPending} error={create.error} onSubmit={(data) => create.mutate(data)} onClose={() => setShowCreate(false)} />
      </Modal>
      {deleteId && (
        <HierarchyDeletionDialog
          open
          title={t("subscriptions.remove_title")}
          message={t("subscriptions.remove_message")}
          confirmationPhrase={subscriptionItems.find((subscription) => subscription.id === deleteId)?.name || deleteId}
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
          title={t("subscriptions.remove_title")}
          message={t("subscriptions.remove_message")}
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
        entity="subscriptions"
        title={t("datamgmt.danger_clear_subs")}
        description={t("datamgmt.danger_clear_subs_desc")}
      />
    </PageShell>
  );
}

export default function SubscriptionsPage() {
  return (
    <PermissionGuard module="subscriptions">
      <Suspense>
        <SubscriptionsContent />
      </Suspense>
    </PermissionGuard>
  );
}

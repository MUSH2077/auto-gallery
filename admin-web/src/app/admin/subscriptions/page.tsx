"use client";
import { useMemo, useState, useEffect, Suspense } from "react";
import { useToast } from "@/components/Toast";
import { useT } from "@/lib/i18n";
import { useStaggeredEntrance } from "@/lib/motion";
import { useRouter, useSearchParams, usePathname } from "next/navigation";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api, queryKeys, type SearchQualifierToken, type SubscriptionSearchHit } from "@/lib/api";
import { PageHeader, PageSection, EmptyState, ErrorState, ConfirmDialog, Modal, StatusBadge, FilterBar, SelectionBar, PageShell, PermissionGuard, EntityList, EntityRow, RowActionMenu, SmartSearchInput, useSearchBatchComposer } from "@/components";
import { useNotifications } from "@/components/NotificationCenter";
import { scheduleModeLabel, useI18nFormat } from "@/lib/i18n-format";
import DomainDangerZone from "@/components/DomainDangerZone";

type FilterMode = "all" | "active" | "inactive" | "sync_on" | "sync_off" | "never_synced";

function CreateForm({ isPending, error, onSubmit, onClose }: {
  isPending: boolean; error: Error | null;
  onSubmit: (data: { creator_id: string; name?: string }) => void;
  onClose: () => void;
}) {
  const [creatorId, setCreatorId] = useState(""); const [name, setName] = useState("");
  const t = useT();
  const toast = useToast();
  const creators = useQuery({ queryKey: queryKeys.creators.all, queryFn: () => api.listCreators() });
  return (
    <div className="space-y-4">
      <div>
        <label className="block text-sm font-medium mb-1">{t("subscriptions.creator_label")}</label>
        <select value={creatorId} onChange={(e) => setCreatorId(e.target.value)} className="select w-full">
          <option value="">{t("subscriptions.select_creator")}</option>
          {creators.data?.items.map((c) => <option key={c.id} value={c.id}>{c.display_name || c.name}</option>)}
        </select>
      </div>
      <div><label className="block text-sm font-medium mb-1">{t("subscriptions.label_field")}</label><input value={name} onChange={(e) => setName(e.target.value)} className="input w-full" placeholder={t("subscriptions.label_placeholder")} /></div>
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
  const sp = useSearchParams();
  const pathname = usePathname();

  // Filter state derived from URL
  const search = sp.get("q") ?? "";
  const page = Number(sp.get("p") ?? "0");
  const limit = 25;
  const [showCreate, setShowCreate] = useState(false);
  const [deleteId, setDeleteId] = useState<string | null>(null);
  const [syncingSubId, setSyncingSubId] = useState<string | null>(null);

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

  const FILTERS: { key: FilterMode; label: string }[] = [
    { key: "all", label: t("subscriptions.filter_all") },
    { key: "active", label: t("subscriptions.filter_active") },
    { key: "inactive", label: t("subscriptions.filter_inactive") },
    { key: "sync_on", label: t("subscriptions.filter_sync_on") },
    { key: "sync_off", label: t("subscriptions.filter_sync_off") },
    { key: "never_synced", label: t("subscriptions.filter_never") },
  ];

  const subsQuery = useQuery({
    queryKey: [...queryKeys.subscriptions.all, "compound-search", page, search],
    queryFn: () => api.search(search, page * limit, limit, "subscriptions"),
    placeholderData: (previousData) => previousData,
  });
  const subs = {
    ...subsQuery,
    data: subsQuery.data?.groups.subscriptions?.items,
  };
  const isValues = (subsQuery.data?.parsed.tokens || [])
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
  const subscriptionItems = subs.data || [];
  const subscriptionEntrance = useStaggeredEntrance(subscriptionItems.map((subscription) => subscription.id));

  useEffect(() => {
    if (notify.operationJob?.kind !== "danbooru-import-all" || notify.operationJob.status !== "completed") return;
    subs.refetch();
  }, [notify.operationJob?.jobId, notify.operationJob?.status]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (notify.batchJob?.status !== "completed") return;
    subs.refetch();
  }, [notify.batchJob?.jobId, notify.batchJob?.status]); // eslint-disable-line react-hooks/exhaustive-deps
  const systemSettings = useQuery({
    queryKey: queryKeys.admin.settings,
    queryFn: api.getAdminSettings,
  });
  const decisions = useQuery({
    queryKey: queryKeys.schedulerDecisions,
    queryFn: api.schedulerDecisions,
    refetchInterval: 15000,
  });
  const sysDefaults = systemSettings.data?.subscription_defaults;
  const decisionBySub = useMemo(() => {
    const grouped = new Map<string, { due: number; blocked: number; nextDueAt?: string | null }>();
    for (const item of decisions.data?.items || []) {
      const current = grouped.get(item.subscription_id) || { due: 0, blocked: 0, nextDueAt: null };
      if (item.due) current.due += 1;
      if (["auth_unhealthy", "url_invalid", "unknown_provider", "provider_not_downloadable"].includes(item.reason)) current.blocked += 1;
      if (item.next_due_at && (!current.nextDueAt || item.next_due_at < current.nextDueAt)) current.nextDueAt = item.next_due_at;
      grouped.set(item.subscription_id, current);
    }
    return grouped;
  }, [decisions.data?.items]);

  const refreshSubscriptionViews = () => {
    qc.invalidateQueries({ queryKey: queryKeys.subscriptions.all });
    qc.invalidateQueries({ queryKey: queryKeys.creators.all });
  };

  const create = useMutation({
    mutationFn: (data: { creator_id: string; name?: string }) => api.createSubscription(data),
    onSuccess: () => { setShowCreate(false); refreshSubscriptionViews(); },
  });

  const del = useMutation({
    mutationFn: (id: string) => api.deleteSubscription(id),
    onSuccess: () => { setDeleteId(null); refreshSubscriptionViews(); },
  });

  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [confirmBatchDel, setConfirmBatchDel] = useState(false);

  const syncNow = useMutation({
    mutationFn: (id: string) => api.syncNowSubscription(id),
    onMutate: (id) => setSyncingSubId(id),
    onSuccess: (data) => {
      subs.refetch();
      refreshSubscriptionViews();
      qc.invalidateQueries({ queryKey: queryKeys.downloadJobs.all });
      qc.invalidateQueries({ queryKey: queryKeys.tasks.all });
      qc.invalidateQueries({ queryKey: queryKeys.schedulerDecisions });
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
    mutationFn: (ids: string[]) => api.batchDeleteSubscriptions(ids),
    onSuccess: () => { setSelected(new Set()); setConfirmBatchDel(false); refreshSubscriptionViews(); },
  });

  const batchSync = useMutation({
    mutationFn: (params: { ids: string[]; enable: boolean }) => api.batchToggleSyncSubscriptions(params.ids, params.enable),
    onSuccess: () => { setSelected(new Set()); refreshSubscriptionViews(); toast.success({ message: t("notification.updated") }); },
  });

  useEffect(() => {
    if (!subs.data) return;
    const visibleIds = new Set(subs.data.map((sub) => sub.id));
    setSelected((prev) => {
      const next = new Set([...prev].filter((id) => visibleIds.has(id)));
      return next.size === prev.size ? prev : next;
    });
  }, [subs.data]);

  const toggleSelect = (id: string) => {
    const next = new Set(selected);
    next.has(id) ? next.delete(id) : next.add(id);
    setSelected(next);
  };
  const selectAll = () => {
    if (selected.size === (subs.data?.length || 0)) setSelected(new Set());
    else setSelected(new Set((subs.data || []).map((s) => s.id)));
  };
  const setSearchQuery = (next: string) => {
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

  return (
    <PageShell>
      <PageHeader
        title={t("subscriptions.title")}
        description={t("subscriptions.count").replace("{count}", String(subsQuery.data?.groups.subscriptions?.total ?? 0))}
        primaryAction={<button onClick={() => setShowCreate(true)} className="btn-primary">{t("subscriptions.new")}</button>}
      />

      {/* Toolbar */}
      <div data-page-primary-content>
      <FilterBar>
        <SmartSearchInput
          value={inputVal}
          onChange={setInputVal}
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
      </FilterBar>
      </div>

      <PageSection>
      <SelectionBar
        count={selected.size}
        label={t("subscriptions.delete_selected").replace("{count}", String(selected.size))}
        clearLabel={t("common.clear")}
        onClear={() => setSelected(new Set())}
      >
        <button onClick={() => batchSync.mutate({ ids: [...selected], enable: true })} disabled={batchSync.isPending}
          className="btn-primary text-xs disabled:opacity-50">{t("subscriptions.enable_sync")}</button>
        <button onClick={() => batchSync.mutate({ ids: [...selected], enable: false })} disabled={batchSync.isPending}
          className="btn-ghost text-xs disabled:opacity-50">{t("subscriptions.disable_sync")}</button>
        <button onClick={() => setConfirmBatchDel(true)} className="btn-danger text-xs">
          {t("subscriptions.delete_selected").replace("{count}", String(selected.size))}
        </button>
      </SelectionBar>

      {/* Select all */}
      {subs.data && subs.data.length > 0 && (
        <label className="mb-2 flex cursor-pointer items-center gap-2 text-xs text-muted">
          <input type="checkbox" aria-label={t("subscriptions.select_all")} checked={selected.size === subs.data.length && subs.data.length > 0} onChange={selectAll} className="rounded" />
          {t("subscriptions.select_all")}
        </label>
      )}

      {/* Content */}
      {subs.isLoading && <div className="space-y-2">{Array.from({ length: 5 }).map((_, i) => <div key={i} className="h-16 rounded-md bg-subtle dark:bg-subtle animate-pulse" />)}</div>}
      {subs.error && <ErrorState message={(subs.error as Error).message} onRetry={() => subs.refetch()} />}
      {subs.data && !subs.data.length && (
        <EmptyState
          title={search || filter !== "all" ? t("works.no_works_filter") : t("subscriptions.no_subs")}
          description={search || filter !== "all" ? undefined : t("subscriptions.no_subs_desc")}
          action={!search && filter === "all" ? <button onClick={() => setShowCreate(true)} className="btn-primary">{t("subscriptions.create_sub")}</button> : undefined}
        />
      )}

      {subs.data && subs.data.length > 0 && (
        <EntityList label={t("subscriptions.title")}>
          {subs.data.map((s: SubscriptionSearchHit, i: number) => {
            const name = s.name || s.creator_display_name || s.creator_name || s.creator_id.slice(0, 8);
            const creatorName = s.creator_display_name || s.creator_name || s.creator_id.slice(0, 8);
            const decision = decisionBySub.get(s.id);
            const blocked = decision?.blocked || 0;
            const due = decision?.due || 0;
            const scheduleMode = s.schedule_mode || sysDefaults?.schedule_mode || "interval";
            const scheduleValue = s.schedule_mode === "fixed_time"
              ? (s.scheduled_times || t("scheduler.fixed_time"))
              : s.schedule_mode === "manual"
                ? t("subscriptions.manual")
                : s.schedule_mode === "interval"
                  ? `${s.sync_interval_hours}h`
                  : sysDefaults?.schedule_mode === "fixed_time"
                    ? t("scheduler.fixed_time")
                    : `${s.sync_interval_hours || sysDefaults?.default_sync_interval_hours}h`;
            return (
              <EntityRow
                key={s.id}
                label={t("common.open_item", { name })}
                selected={selected.has(s.id)}
                entrance={subscriptionEntrance(s.id, i)}
                onOpen={() => router.push(`/admin/subscriptions/${s.id}`)}
              >
                <input
                  type="checkbox"
                  aria-label={t("common.select_item", { name })}
                  checked={selected.has(s.id)}
                  onChange={() => toggleSelect(s.id)}
                  className="shrink-0 rounded"
                  onClick={(event) => event.stopPropagation()}
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
                      status={s.latest_job_status || "unknown"}
                      label={s.latest_job_status ? undefined : t("subscriptions.no_jobs")}
                      className="py-0 text-[10px]"
                    />
                    {due > 0 && <span className="rounded-full bg-accent-subtle px-2 py-0.5 text-[10px] font-medium text-accent">{t("subscriptions.due_sources", { count: due })}</span>}
                    {blocked > 0 && <span className="rounded-full bg-danger-subtle px-2 py-0.5 text-[10px] font-medium text-danger">{t("subscriptions.blocked_sources", { count: blocked })}</span>}
                    {(s.running_job_count || 0) > 0 && <span className="rounded-full bg-accent-subtle px-2 py-0.5 text-[10px] font-medium text-accent">{t("subscriptions.running_jobs", { count: s.running_job_count || 0 })}</span>}
                    {(s.failed_job_count || 0) > 0 && <span className="rounded-full bg-danger-subtle px-2 py-0.5 text-[10px] font-medium text-danger">{t("subscriptions.failed_jobs", { count: s.failed_job_count || 0 })}</span>}
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
                  <div className="entity-meta">
                    <span>{t("subscriptions.sources_summary", { enabled: s.enabled_source_count ?? 0, total: s.source_count ?? 0 })}</span>
                    <span className={s.sync_enabled ? "text-success" : ""}>{s.sync_enabled ? t("subscriptions.auto_sync") : t("subscriptions.manual")}</span>
                    <span>{t("subscriptions.last_success", { time: fmt.relative(s.last_synced_at, "subscriptions.never") })}</span>
                    <span>{scheduleModeLabel(t, scheduleMode)} · {scheduleValue}</span>
                    <span>{t("subscriptions.next_due", { time: fmt.dateTime(decision?.nextDueAt) })}</span>
                  </div>
                </div>
                <div className="entity-actions" onClick={(event) => event.stopPropagation()}>
                  <button
                    type="button"
                    onClick={() => syncNow.mutate(s.id)}
                    disabled={syncNow.isPending}
                    className="btn-primary text-xs"
                  >
                    {syncingSubId === s.id ? t("subscriptions.syncing") : t("subscriptions.sync_all")}
                  </button>
                  <RowActionMenu
                    label={t("common.more_actions")}
                    items={[
                      { label: t("subscriptions.del"), tone: "danger", onSelect: () => setDeleteId(s.id) },
                    ]}
                  />
                </div>
              </EntityRow>
            );
          })}
        </EntityList>
      )}

      {/* Pagination */}
      {subs.data && subs.data.length > 0 && (
        <div className="flex gap-2 justify-center mt-4">
          <button disabled={page === 0} onClick={() => updateParams({ p: page <= 1 ? null : String(page - 1) }, false)} className="btn-ghost disabled:opacity-30">{t("common.prev")}</button>
          <span className="px-3 py-1 text-sm text-muted">{t("common.page").replace("{page}", String(page + 1))}</span>
          <button onClick={() => updateParams({ p: String(page + 1) }, false)} disabled={!subs.data || subs.data.length < limit} className="btn-ghost disabled:opacity-30">{t("common.next")}</button>
        </div>
      )}
      </PageSection>

      <Modal open={showCreate} onClose={() => setShowCreate(false)} title={t("subscriptions.new_sub_title")}>
        <CreateForm isPending={create.isPending} error={create.error} onSubmit={(data) => create.mutate(data)} onClose={() => setShowCreate(false)} />
      </Modal>
      {deleteId && <ConfirmDialog open title={t("subscriptions.delete_title")} message={t("subscriptions.delete_msg")} onConfirm={() => del.mutate(deleteId)} onCancel={() => setDeleteId(null)} isPending={del.isPending} error={(del.error as Error)?.message} />}
      {confirmBatchDel && <ConfirmDialog open title={t("subscriptions.batch_delete_title")} message={t("subscriptions.batch_delete_msg").replace("{count}", String(selected.size))} onConfirm={() => batchDel.mutate([...selected])} onCancel={() => setConfirmBatchDel(false)} isPending={batchDel.isPending} error={(batchDel.error as Error)?.message} />}
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

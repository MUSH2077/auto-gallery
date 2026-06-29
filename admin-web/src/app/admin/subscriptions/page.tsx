"use client";
import { useMemo, useState, useEffect, Suspense } from "react";
import { useToast } from "@/components/Toast";
import { useT } from "@/lib/i18n";
import { useRouter, useSearchParams, usePathname } from "next/navigation";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api, queryKeys, Subscription } from "@/lib/api";
import { PageHeader, EmptyState, ErrorState, ConfirmDialog, Modal, SourceBadge, StatusBadge, FilterBar, SelectionBar, PageShell } from "@/components";
import { useNotifications } from "@/components/NotificationCenter";
import { scheduleModeLabel, useI18nFormat } from "@/lib/i18n-format";

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

function buildFilters(filter: FilterMode, search: string) {
  const f: { search?: string; is_active?: boolean; sync_enabled?: boolean; never_synced?: boolean } = {};
  if (search) f.search = search;
  switch (filter) {
    case "active": f.is_active = true; break;
    case "inactive": f.is_active = false; break;
    case "sync_on": f.sync_enabled = true; break;
    case "sync_off": f.sync_enabled = false; break;
    case "never_synced": f.never_synced = true; break;
  }
  return f;
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
  const filter = (sp.get("filter") as FilterMode) ?? "all";
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

  const filters = buildFilters(filter, search);

  const subsCount = useQuery({ queryKey: queryKeys.subscriptions.count, queryFn: () => api.countSubscriptions() });
  const subs = useQuery({
    queryKey: queryKeys.subscriptions.list(page, limit, filters),
    queryFn: () => api.listSubscriptions(page * limit, limit, filters),
    placeholderData: (previousData) => previousData,
  });

  useEffect(() => {
    if (notify.operationJob?.kind !== "danbooru-import-all" || notify.operationJob.status !== "completed") return;
    subs.refetch();
    subsCount.refetch();
  }, [notify.operationJob?.jobId, notify.operationJob?.status]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (notify.batchJob?.status !== "completed") return;
    subs.refetch();
    subsCount.refetch();
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
        toast.warning({ message: (data as any).message || "Sync partially failed" });
      } else if (data.job_ids.length === 0) {
        toast.warning({ message: t("subscriptions.sync_no_jobs") });
      } else {
        toast.success({
          message: t("subscriptions.sync_result", { count: data.job_ids.length, skipped: data.skipped_count ?? 0 }),
          action: data.task_id ? { label: t("jobs.open_task", "View task"), onClick: () => router.push(`/admin/jobs?tab=admin&task=${data.task_id}`) } : undefined,
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

  return (
    <PageShell size="normal">
      <PageHeader title={t("subscriptions.title")} description={t("subscriptions.count", "0 subscriptions").replace("{count}", String(subsCount.data?.count ?? 0))}>
        <button onClick={() => setShowCreate(true)} className="btn-primary">{t("subscriptions.new")}</button>
      </PageHeader>

      {/* Toolbar */}
      <FilterBar>
        <input value={inputVal} onChange={(e) => { setInputVal(e.target.value); }} placeholder={t("subscriptions.search")} className="input w-56 py-1.5" />
        <div className="segmented-control">
          {FILTERS.map((f) => (
            <button key={f.key} onClick={() => updateParams({ filter: f.key === "all" ? null : f.key })}
              className={`segment ${filter === f.key ? "segment-active" : ""}`}>
              {f.label}
            </button>
          ))}
        </div>
      </FilterBar>

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
          <input type="checkbox" aria-label="Select item" checked={selected.size === subs.data.length && subs.data.length > 0} onChange={selectAll} className="rounded" />
          {t("subscriptions.select_all")}
        </label>
      )}

      {/* Content */}
      {subs.isLoading && <div className="space-y-2">{Array.from({ length: 5 }).map((_, i) => <div key={i} className="h-16 rounded-md bg-subtle dark:bg-subtle animate-pulse" />)}</div>}
      {subs.error && <ErrorState message={(subs.error as Error).message} />}
      {subs.data && !subs.data.length && <EmptyState title={t("subscriptions.no_subs")} description={t("subscriptions.no_subs_desc")} action={<button onClick={() => setShowCreate(true)} className="btn-primary">{t("subscriptions.create_sub")}</button>} />}

      {subs.data && subs.data.length > 0 && (
        <div className="overflow-hidden rounded-md border border-border bg-surface dark:border-border dark:bg-surface">
          <div className="hidden grid-cols-[minmax(0,1.45fr)_minmax(120px,0.8fr)_minmax(140px,0.9fr)_minmax(150px,1fr)_minmax(170px,1fr)] gap-3 border-b border-border bg-subtle px-4 py-2 text-xs font-medium uppercase text-muted lg:grid">
            <span>{t("subscriptions.col_group", "Sync group")}</span>
            <span>{t("subscriptions.col_sources", "Sources")}</span>
            <span>{t("subscriptions.col_health", "Health")}</span>
            <span>{t("subscriptions.col_schedule", "Schedule")}</span>
            <span>{t("subscriptions.col_actions", "Actions")}</span>
          </div>
          {subs.data.map((s: Subscription) => (
            <div key={s.id} className={`grid cursor-pointer grid-cols-1 gap-3 border-b border-border p-4 last:border-b-0 hover:bg-subtle dark:border-border dark:hover:bg-subtle lg:grid-cols-[minmax(0,1.45fr)_minmax(120px,0.8fr)_minmax(140px,0.9fr)_minmax(150px,1fr)_minmax(170px,1fr)] ${selected.has(s.id) ? "bg-accent-subtle dark:bg-accent-subtle" : ""}`} onClick={() => router.push(`/admin/subscriptions/${s.id}`)}>
              <div className="flex min-w-0 gap-3">
                <input type="checkbox" aria-label="Select item" checked={selected.has(s.id)} onChange={() => toggleSelect(s.id)} className="mt-1 shrink-0 rounded" onClick={(e) => e.stopPropagation()} />
                <div className="min-w-0">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="truncate text-sm font-semibold text-accent">{s.name || s.creator_display_name || s.creator_name || s.creator_id.slice(0, 8)}</span>
                  {s.is_active ? <span className="w-1.5 h-1.5 bg-green-500 rounded-full shrink-0" /> : <span className="w-1.5 h-1.5 bg-subtle rounded-full shrink-0" />}
                  <StatusBadge status={s.latest_job_status || "unknown"} label={s.latest_job_status ? undefined : t("subscriptions.no_jobs", "No jobs")} className="py-0 text-[10px]" />
                </div>
                <div className="text-xs text-muted">
                  {t("subscriptions.creator_prefix")}{" "}
                  <span className="text-blue-600 hover:underline" onClick={(e) => { e.stopPropagation(); router.push(`/admin/creators/${s.creator_id}`); }}>
                    {s.creator_display_name || s.creator_name || s.creator_id.slice(0, 8)}
                  </span>
                </div>
                <div className="mt-2 flex flex-wrap gap-2 text-xs text-muted">
                  <span>{t("subscriptions.last_success", { time: fmt.relative(s.last_synced_at, "subscriptions.never") })}</span>
                  {s.latest_job_id && <span className="font-mono">{s.latest_job_id.slice(0, 8)}</span>}
                </div>
                </div>
              </div>
              <div className="text-xs text-muted">
                <div className="font-semibold text-fg">{t("subscriptions.sources_summary", { enabled: s.enabled_source_count ?? 0, total: s.source_count ?? 0 })}</div>
                <div className="mt-1 flex flex-wrap gap-2">
                  {s.sync_enabled ? <span className="text-success">{t("subscriptions.auto_sync")}</span> : <span>{t("subscriptions.manual")}</span>}
                  {(decisionBySub.get(s.id)?.due || 0) > 0 && <span className="text-accent">{t("subscriptions.due_sources", { count: decisionBySub.get(s.id)?.due || 0 })}</span>}
                </div>
              </div>
              <div className="text-xs text-muted">
                <div className={`font-semibold ${(s.failed_job_count || 0) > 0 || (decisionBySub.get(s.id)?.blocked || 0) > 0 ? "text-danger" : (s.running_job_count || 0) > 0 ? "text-accent" : "text-fg"}`}>
                  {(s.running_job_count || 0) > 0 ? t("subscriptions.running_jobs", { count: s.running_job_count || 0 }) : (s.failed_job_count || 0) > 0 ? t("subscriptions.failed_jobs", { count: s.failed_job_count || 0 }) : t("subscriptions.healthy", "Healthy")}
                </div>
                <div className="mt-1">{t("subscriptions.blocked_sources", { count: decisionBySub.get(s.id)?.blocked || 0 })}</div>
              </div>
              <div className="text-xs text-muted">
                <div className="font-semibold text-fg">{scheduleModeLabel(t, s.schedule_mode || sysDefaults?.schedule_mode || "interval")}</div>
                <div className="mt-1">{
                  s.schedule_mode === "fixed_time" ? (s.scheduled_times || t("scheduler.fixed_time")) :
                  s.schedule_mode === "manual" ? t("subscriptions.manual") :
                  s.schedule_mode === "interval" ? `${s.sync_interval_hours}h` :
                  sysDefaults?.schedule_mode === "fixed_time" ? t("scheduler.fixed_time") :
                  sysDefaults?.schedule_mode === "interval" ? `${s.sync_interval_hours || sysDefaults?.default_sync_interval_hours}h` :
                  `${s.sync_interval_hours}h`
                }</div>
                <div className="mt-1">{t("subscriptions.next_due", { time: fmt.dateTime(decisionBySub.get(s.id)?.nextDueAt) })}</div>
              </div>
              <div className="flex flex-wrap items-center gap-3 text-xs" onClick={(e) => e.stopPropagation()}>
                <button onClick={(e) => { e.stopPropagation(); syncNow.mutate(s.id); }} disabled={syncNow.isPending}
                  className="text-accent hover:underline disabled:opacity-50 dark:text-accent">
                  {syncingSubId === s.id ? t("subscriptions.syncing") : t("subscriptions.sync_all", "Sync all")}
                </button>
                <button onClick={(e) => { e.stopPropagation(); router.push(`/admin/jobs?tab=downloads&subscription_id=${s.id}`); }} className="text-accent hover:underline dark:text-accent">{t("scheduler.open_jobs")}</button>
                <button onClick={(e) => { e.stopPropagation(); setDeleteId(s.id); }} className="text-danger hover:underline dark:text-danger">{t("subscriptions.del")}</button>
              </div>
            </div>
          ))}
        </div>
      )}

      {/* Pagination */}
      {subs.data && subs.data.length > 0 && (
        <div className="flex gap-2 justify-center mt-4">
          <button disabled={page === 0} onClick={() => updateParams({ p: page <= 1 ? null : String(page - 1) }, false)} className="btn-ghost disabled:opacity-30">{t("common.prev")}</button>
          <span className="px-3 py-1 text-sm text-muted">{t("common.page").replace("{page}", String(page + 1))}</span>
          <button onClick={() => updateParams({ p: String(page + 1) }, false)} disabled={!subs.data || subs.data.length < limit} className="btn-ghost disabled:opacity-30">{t("common.next")}</button>
        </div>
      )}

      <Modal open={showCreate} onClose={() => setShowCreate(false)} title={t("subscriptions.new_sub_title")}>
        <CreateForm isPending={create.isPending} error={create.error} onSubmit={(data) => create.mutate(data)} onClose={() => setShowCreate(false)} />
      </Modal>
      {deleteId && <ConfirmDialog open title={t("subscriptions.delete_title")} message={t("subscriptions.delete_msg")} onConfirm={() => del.mutate(deleteId)} onCancel={() => setDeleteId(null)} isPending={del.isPending} error={(del.error as Error)?.message} />}
      {confirmBatchDel && <ConfirmDialog open title={t("subscriptions.batch_delete_title")} message={t("subscriptions.batch_delete_msg").replace("{count}", String(selected.size))} onConfirm={() => batchDel.mutate([...selected])} onCancel={() => setConfirmBatchDel(false)} isPending={batchDel.isPending} error={(batchDel.error as Error)?.message} />}
    </PageShell>
  );
}

export default function SubscriptionsPage() {
  return (
    <Suspense>
      <SubscriptionsContent />
    </Suspense>
  );
}

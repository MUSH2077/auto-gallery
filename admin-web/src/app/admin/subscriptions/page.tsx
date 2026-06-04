"use client";
import { useState, useEffect, Suspense } from "react";
import { useToast } from "@/components/Toast";
import { useT } from "@/lib/i18n";
import { useRouter, useSearchParams, usePathname } from "next/navigation";
import { useQuery, useMutation } from "@tanstack/react-query";
import { api, queryKeys, Subscription } from "@/lib/api";
import { PageHeader, EmptyState, ErrorState, ConfirmDialog, Modal, SourceBadge } from "@/components";

type FilterMode = "all" | "active" | "inactive" | "sync_on" | "sync_off" | "never_synced";

function jobClass(status?: string) {
  if (!status) return "bg-[#eaeef2] text-[#57606a] dark:bg-[#30363d] dark:text-[#8b949e]";
  if (["pending", "downloading", "downloaded", "importing"].includes(status)) return "bg-[#ddf4ff] text-[#0969da] dark:bg-[#1f6feb26] dark:text-[#58a6ff]";
  if (["failed", "stale"].includes(status)) return "bg-[#ffebe9] text-[#cf222e] dark:bg-[#f8514926] dark:text-[#f85149]";
  return "bg-[#dafbe1] text-[#1a7f37] dark:bg-[#2ea04326] dark:text-[#3fb950]";
}

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
        <select value={creatorId} onChange={(e) => setCreatorId(e.target.value)} className="w-full border rounded px-3 py-2 text-sm dark:bg-slate-700 dark:text-white">
          <option value="">{t("subscriptions.select_creator")}</option>
          {creators.data?.map((c) => <option key={c.id} value={c.id}>{c.display_name || c.name}</option>)}
        </select>
      </div>
      <div><label className="block text-sm font-medium mb-1">{t("subscriptions.label_field")}</label><input value={name} onChange={(e) => setName(e.target.value)} className="w-full border rounded px-3 py-2 text-sm dark:bg-slate-700 dark:text-white" placeholder={t("subscriptions.label_placeholder")} /></div>
      <div className="flex justify-end gap-3 pt-2">
        <button onClick={onClose} className="px-4 py-2 text-sm border rounded hover:bg-gray-50 dark:hover:bg-slate-700 dark:text-gray-300">{t("subscriptions.cancel")}</button>
        <button onClick={() => onSubmit({ creator_id: creatorId, name: name || undefined })} disabled={!creatorId || isPending}
          className="px-4 py-2 text-sm bg-slate-900 dark:bg-slate-700 text-white rounded hover:bg-slate-800 dark:hover:bg-slate-600 disabled:opacity-50">
          {isPending ? t("subscriptions.creating") : t("subscriptions.subscribe")}
        </button>
      </div>
      {error && <p className="text-red-600 text-sm">{error.message}</p>}
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
  const toast = useToast();
  const sp = useSearchParams();
  const pathname = usePathname();

  // Filter state derived from URL
  const search = sp.get("q") ?? "";
  const filter = (sp.get("filter") as FilterMode) ?? "all";
  const page = Number(sp.get("p") ?? "0");
  const limit = 25;
  const [showCreate, setShowCreate] = useState(false);
  const [deleteId, setDeleteId] = useState<string | null>(null);

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

  const subsCount = useQuery({ queryKey: ["subs-count"], queryFn: () => api.countSubscriptions() });
  const subs = useQuery({
    queryKey: [...queryKeys.subscriptions.all, page, filters],
    queryFn: () => api.listSubscriptions(page * limit, limit, filters),
  });
  const systemSettings = useQuery({
    queryKey: queryKeys.admin.settings,
    queryFn: api.getAdminSettings,
  });
  const sysDefaults = systemSettings.data?.subscription_defaults;

  const create = useMutation({
    mutationFn: (data: { creator_id: string; name?: string }) => api.createSubscription(data),
    onSuccess: () => { setShowCreate(false); subs.refetch(); },
  });

  const del = useMutation({
    mutationFn: (id: string) => api.deleteSubscription(id),
    onSuccess: () => { setDeleteId(null); subs.refetch(); },
  });

  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [confirmBatchDel, setConfirmBatchDel] = useState(false);

  const syncNow = useMutation({
    mutationFn: (id: string) => api.syncNowSubscription(id),
    onSuccess: (data) => {
      subs.refetch();
      if (data.status === "error" || data.status === "partial_error") {
        toast.warning({ message: (data as any).message || "Sync partially failed" });
      } else if (data.job_ids.length === 0) {
        toast.warning({ message: t("subscriptions.sync_no_jobs") });
      }
    },
    onError: (e: Error) => toast.error({ message: e.message }),
  });

  const batchDel = useMutation({
    mutationFn: (ids: string[]) => api.batchDeleteSubscriptions(ids),
    onSuccess: () => { setSelected(new Set()); setConfirmBatchDel(false); subs.refetch(); },
  });

  const batchSync = useMutation({
    mutationFn: (params: { ids: string[]; enable: boolean }) => api.batchToggleSyncSubscriptions(params.ids, params.enable),
    onSuccess: () => { setSelected(new Set()); subs.refetch(); toast.success({ message: t("notification.updated") }); },
  });

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
    <main className="max-w-6xl mx-auto p-6">
      <PageHeader title={t("subscriptions.title")} description={t("subscriptions.count", "0 subscriptions").replace("{count}", String(subsCount.data?.count ?? 0))}>
        <button onClick={() => setShowCreate(true)} className="btn-primary">{t("subscriptions.new")}</button>
      </PageHeader>

      {/* Toolbar */}
      <div className="flex items-center gap-3 mb-4 flex-wrap">
        <input value={inputVal} onChange={(e) => { setInputVal(e.target.value); }} placeholder={t("subscriptions.search")} className="w-56 rounded-md border border-[#d8dee4] px-3 py-1.5 text-sm dark:border-[#30363d] dark:bg-[#0d1117] dark:text-white" />
        <div className="flex gap-1 rounded-md border border-[#d8dee4] bg-[#f6f8fa] p-0.5 dark:border-[#30363d] dark:bg-[#161b22]">
          {FILTERS.map((f) => (
            <button key={f.key} onClick={() => updateParams({ filter: f.key === "all" ? null : f.key })}
              className={`rounded px-3 py-1 text-xs transition-colors ${filter === f.key ? "bg-white font-medium shadow-sm dark:bg-[#30363d]" : "text-[#57606a] hover:text-[#24292f] dark:text-[#8b949e] dark:hover:text-[#e6edf3]"}`}>
              {f.label}
            </button>
          ))}
        </div>
        <div className="flex-1" />
        {selected.size > 0 && (
          <div className="flex gap-2">
            <button onClick={() => batchSync.mutate({ ids: [...selected], enable: true })} disabled={batchSync.isPending}
              className="btn-primary disabled:opacity-50">{t("subscriptions.enable_sync")}</button>
            <button onClick={() => batchSync.mutate({ ids: [...selected], enable: false })} disabled={batchSync.isPending}
              className="btn-ghost disabled:opacity-50">{t("subscriptions.disable_sync")}</button>
            <button onClick={() => setConfirmBatchDel(true)} className="rounded-md border border-[#cf222e]/40 px-3 py-1.5 text-sm font-medium text-[#cf222e] hover:bg-[#ffebe9] dark:text-[#f85149] dark:hover:bg-[#f8514926]">
              {t("subscriptions.delete_selected").replace("{count}", String(selected.size))}
            </button>
          </div>
        )}
      </div>

      {/* Select all */}
      {subs.data && subs.data.length > 0 && (
        <label className="flex items-center gap-2 mb-2 text-xs text-gray-500 dark:text-gray-400 cursor-pointer">
          <input type="checkbox" checked={selected.size === subs.data.length && subs.data.length > 0} onChange={selectAll} className="rounded" />
          {t("subscriptions.select_all")}
        </label>
      )}

      {/* Content */}
      {subs.isLoading && <div className="space-y-2">{Array.from({ length: 5 }).map((_, i) => <div key={i} className="h-16 bg-gray-100 dark:bg-slate-700 rounded animate-pulse" />)}</div>}
      {subs.error && <ErrorState message={(subs.error as Error).message} />}
      {subs.data && !subs.data.length && <EmptyState title={t("subscriptions.no_subs")} description={t("subscriptions.no_subs_desc")} action={<button onClick={() => setShowCreate(true)} className="px-4 py-2 bg-slate-900 dark:bg-slate-700 text-white rounded text-sm">{t("subscriptions.create_sub")}</button>} />}

      {subs.data && subs.data.length > 0 && (
        <div className="overflow-hidden rounded-md border border-[#d8dee4] bg-white dark:border-[#30363d] dark:bg-[#161b22]">
          {subs.data.map((s: Subscription) => (
            <div key={s.id} className={`flex cursor-pointer items-center gap-3 border-b border-[#d8dee4] p-4 last:border-b-0 hover:bg-[#f6f8fa] dark:border-[#30363d] dark:hover:bg-[#21262d] ${selected.has(s.id) ? "bg-[#ddf4ff] dark:bg-[#1f6feb26]" : ""}`} onClick={() => router.push(`/admin/subscriptions/${s.id}`)}>
              <input type="checkbox" checked={selected.has(s.id)} onChange={() => toggleSelect(s.id)} className="rounded shrink-0" onClick={(e) => e.stopPropagation()} />
              <div className="flex-1 min-w-0">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="truncate text-sm font-semibold text-[#0969da] dark:text-[#58a6ff]">{s.name || s.creator_display_name || s.creator_name || s.creator_id.slice(0, 8)}</span>
                  {s.is_active ? <span className="w-1.5 h-1.5 bg-green-500 rounded-full shrink-0" /> : <span className="w-1.5 h-1.5 bg-gray-300 rounded-full shrink-0" />}
                  <span className={`rounded-full px-2 py-0.5 text-[10px] font-medium ${jobClass(s.latest_job_status)}`}>{s.latest_job_status || "no jobs"}</span>
                </div>
                <div className="text-xs text-[#57606a] dark:text-[#8b949e]">
                  {t("subscriptions.creator_prefix")}{" "}
                  <span className="text-blue-600 hover:underline" onClick={(e) => { e.stopPropagation(); router.push(`/admin/creators/${s.creator_id}`); }}>
                    {s.creator_display_name || s.creator_name || s.creator_id.slice(0, 8)}
                  </span>
                </div>
                <div className="mt-2 flex flex-wrap gap-3 text-xs text-[#57606a] dark:text-[#8b949e]">
                  <span>{s.source_count ?? 0} repositories</span>
                  <span>{s.enabled_source_count ?? 0} enabled</span>
                  <span>{s.last_synced_at ? `last sync ${new Date(s.last_synced_at).toLocaleDateString()}` : t("subscriptions.never")}</span>
                </div>
              </div>
              <div className="flex items-center gap-3 shrink-0 text-xs" onClick={(e) => e.stopPropagation()}>
                {s.sync_enabled ? <span className="text-green-600 dark:text-green-400">{t("subscriptions.auto_sync")}</span> : <span className="text-gray-400">{t("subscriptions.manual")}</span>}
                <span className="text-[#57606a] dark:text-[#8b949e]">{
  s.schedule_mode === "fixed_time" ? (s.scheduled_times || t("scheduler.fixed_time")) :
  s.schedule_mode === "manual" ? t("subscriptions.manual") :
  s.schedule_mode === "interval" ? `${s.sync_interval_hours}h` :
  // Inherit from system
  sysDefaults?.schedule_mode === "fixed_time" ? t("scheduler.fixed_time") :
  sysDefaults?.schedule_mode === "interval" ? `${s.sync_interval_hours || sysDefaults?.default_sync_interval_hours}h` :
  `${s.sync_interval_hours}h`
}</span>
                <button onClick={(e) => { e.stopPropagation(); syncNow.mutate(s.id); }} disabled={syncNow.isPending}
                  className="text-[#0969da] hover:underline disabled:opacity-50 dark:text-[#58a6ff]">
                  {syncNow.isPending ? t("subscriptions.syncing") : t("subscriptions.sync")}
                </button>
                <button onClick={(e) => { e.stopPropagation(); setDeleteId(s.id); }} className="text-[#cf222e] hover:underline dark:text-[#f85149]">{t("subscriptions.del")}</button>
              </div>
            </div>
          ))}
        </div>
      )}

      {/* Pagination */}
      {subs.data && subs.data.length > 0 && (
        <div className="flex gap-2 justify-center mt-4">
          <button disabled={page === 0} onClick={() => updateParams({ p: page <= 1 ? null : String(page - 1) }, false)} className="px-3 py-1 text-sm border rounded disabled:opacity-30 dark:border-slate-600 dark:text-gray-300">{t("common.prev")}</button>
          <span className="px-3 py-1 text-sm text-gray-500 dark:text-gray-400">{t("common.page").replace("{page}", String(page + 1))}</span>
          <button onClick={() => updateParams({ p: String(page + 1) }, false)} disabled={!subs.data || subs.data.length < limit} className="px-3 py-1 text-sm border rounded disabled:opacity-30 dark:border-slate-600 dark:text-gray-300">{t("common.next")}</button>
        </div>
      )}

      <Modal open={showCreate} onClose={() => setShowCreate(false)} title={t("subscriptions.new_sub_title")}>
        <CreateForm isPending={create.isPending} error={create.error} onSubmit={(data) => create.mutate(data)} onClose={() => setShowCreate(false)} />
      </Modal>
      {deleteId && <ConfirmDialog open title={t("subscriptions.delete_title")} message={t("subscriptions.delete_msg")} onConfirm={() => del.mutate(deleteId)} onCancel={() => setDeleteId(null)} isPending={del.isPending} error={(del.error as Error)?.message} />}
      {confirmBatchDel && <ConfirmDialog open title={t("subscriptions.batch_delete_title")} message={t("subscriptions.batch_delete_msg").replace("{count}", String(selected.size))} onConfirm={() => batchDel.mutate([...selected])} onCancel={() => setConfirmBatchDel(false)} isPending={batchDel.isPending} error={(batchDel.error as Error)?.message} />}
    </main>
  );
}

export default function SubscriptionsPage() {
  return (
    <Suspense>
      <SubscriptionsContent />
    </Suspense>
  );
}

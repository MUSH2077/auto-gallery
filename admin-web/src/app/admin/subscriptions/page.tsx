"use client";
import { useState, useEffect, Suspense } from "react";
import { useT } from "@/lib/i18n";
import { useRouter, useSearchParams, usePathname } from "next/navigation";
import { useQuery, useMutation } from "@tanstack/react-query";
import { api, queryKeys, Subscription } from "@/lib/api";
import { PageHeader, EmptyState, ErrorState, ConfirmDialog, Modal, SourceBadge } from "@/components";

type FilterMode = "all" | "active" | "inactive" | "sync_on" | "sync_off" | "never_synced";

function CreateForm({ isPending, error, onSubmit, onClose }: {
  isPending: boolean; error: Error | null;
  onSubmit: (data: { creator_id: string; name?: string }) => void;
  onClose: () => void;
}) {
  const [creatorId, setCreatorId] = useState(""); const [name, setName] = useState("");
  const t = useT();
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

  const subs = useQuery({
    queryKey: [...queryKeys.subscriptions.all, page, filters],
    queryFn: () => api.listSubscriptions(page * limit, limit, filters),
  });

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
    onSuccess: () => subs.refetch(),
  });

  const batchDel = useMutation({
    mutationFn: (ids: string[]) => api.batchDeleteSubscriptions(ids),
    onSuccess: () => { setSelected(new Set()); setConfirmBatchDel(false); subs.refetch(); },
  });

  const batchSync = useMutation({
    mutationFn: (params: { ids: string[]; enable: boolean }) => api.batchToggleSyncSubscriptions(params.ids, params.enable),
    onSuccess: () => { setSelected(new Set()); subs.refetch(); },
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
      <PageHeader title={t("subscriptions.title")} description={subs.data?.length ? t("common.page").replace("{page}", String(page + 1)) : t("subscriptions.desc", "")}>
        <button onClick={() => setShowCreate(true)} className="px-4 py-2 bg-slate-900 dark:bg-slate-700 text-white rounded text-sm hover:bg-slate-800 dark:hover:bg-slate-600">{t("subscriptions.new")}</button>
      </PageHeader>

      {/* Toolbar */}
      <div className="flex items-center gap-3 mb-4 flex-wrap">
        <input value={inputVal} onChange={(e) => { setInputVal(e.target.value); }} placeholder={t("subscriptions.search")} className="border rounded px-3 py-2 text-sm w-48 dark:bg-slate-700 dark:text-white dark:border-slate-600" />
        <div className="flex gap-1 bg-gray-100 dark:bg-slate-700 rounded p-0.5">
          {FILTERS.map((f) => (
            <button key={f.key} onClick={() => updateParams({ filter: f.key === "all" ? null : f.key })}
              className={`px-3 py-1 text-xs rounded transition-colors ${filter === f.key ? "bg-white dark:bg-slate-600 shadow-sm font-medium" : "text-gray-500 hover:text-gray-700 dark:text-gray-400"}`}>
              {f.label}
            </button>
          ))}
        </div>
        <div className="flex-1" />
        {selected.size > 0 && (
          <div className="flex gap-2">
            <button onClick={() => batchSync.mutate({ ids: [...selected], enable: true })} disabled={batchSync.isPending}
              className="px-3 py-1.5 text-xs bg-green-600 text-white rounded hover:bg-green-700 disabled:opacity-50">{t("subscriptions.enable_sync")}</button>
            <button onClick={() => batchSync.mutate({ ids: [...selected], enable: false })} disabled={batchSync.isPending}
              className="px-3 py-1.5 text-xs bg-gray-600 text-white rounded hover:bg-gray-700 disabled:opacity-50">{t("subscriptions.disable_sync")}</button>
            <button onClick={() => setConfirmBatchDel(true)} className="px-3 py-1.5 text-xs bg-red-600 text-white rounded hover:bg-red-700">
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
        <div className="space-y-1">
          {subs.data.map((s: Subscription) => (
            <div key={s.id} className={`bg-white dark:bg-slate-800 rounded-lg shadow-sm p-3 flex items-center gap-3 cursor-pointer hover:shadow-md transition-shadow ${selected.has(s.id) ? "ring-2 ring-blue-500" : ""}`} onClick={() => router.push(`/admin/subscriptions/${s.id}`)}>
              <input type="checkbox" checked={selected.has(s.id)} onChange={() => toggleSelect(s.id)} className="rounded shrink-0" onClick={(e) => e.stopPropagation()} />
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-2">
                  <span className="font-medium text-sm truncate">{s.name || s.creator_display_name || s.creator_name || s.creator_id.slice(0, 8)}</span>
                  {s.is_active ? <span className="w-1.5 h-1.5 bg-green-500 rounded-full shrink-0" /> : <span className="w-1.5 h-1.5 bg-gray-300 rounded-full shrink-0" />}
                </div>
                <div className="text-xs text-gray-400 dark:text-gray-500">
                  {t("subscriptions.creator_prefix")}{" "}
                  <span className="text-blue-600 hover:underline" onClick={(e) => { e.stopPropagation(); router.push(`/admin/creators/${s.creator_id}`); }}>
                    {s.creator_display_name || s.creator_name || s.creator_id.slice(0, 8)}
                  </span>
                </div>
              </div>
              <div className="flex items-center gap-3 shrink-0 text-xs" onClick={(e) => e.stopPropagation()}>
                {s.sync_enabled ? <span className="text-green-600 dark:text-green-400">{t("subscriptions.auto_sync")}</span> : <span className="text-gray-400">{t("subscriptions.manual")}</span>}
                <span className="text-gray-400 dark:text-gray-500">{s.sync_interval_hours}h</span>
                <span className="text-gray-400 dark:text-gray-500 hidden sm:inline">{s.last_synced_at ? `${t("subscriptions.last_sync")} ${new Date(s.last_synced_at).toLocaleDateString()}` : t("subscriptions.never")}</span>
                <button onClick={(e) => { e.stopPropagation(); syncNow.mutate(s.id); }} disabled={syncNow.isPending}
                  className="text-blue-600 hover:underline disabled:opacity-50">
                  {syncNow.isPending ? t("subscriptions.syncing") : t("subscriptions.sync")}
                </button>
                <button onClick={(e) => { e.stopPropagation(); setDeleteId(s.id); }} className="text-red-500 hover:text-red-700 dark:text-red-400">{t("subscriptions.del")}</button>
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

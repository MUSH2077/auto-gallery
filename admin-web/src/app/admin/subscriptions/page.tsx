"use client";
import { useState, useMemo } from "react";
import { useRouter } from "next/navigation";
import { useQuery, useMutation } from "@tanstack/react-query";
import { api, queryKeys, Subscription } from "@/lib/api";
import { PageHeader, EmptyState, ErrorState, ConfirmDialog, Modal, SourceBadge } from "@/components";

type FilterMode = "all" | "active" | "inactive" | "sync_on" | "sync_off" | "never_synced";

const FILTERS: { key: FilterMode; label: string }[] = [
  { key: "all", label: "All" },
  { key: "active", label: "Active" },
  { key: "inactive", label: "Inactive" },
  { key: "sync_on", label: "Auto-Sync On" },
  { key: "sync_off", label: "Manual Only" },
  { key: "never_synced", label: "Never Synced" },
];

function CreateForm({ isPending, error, onSubmit, onClose }: {
  isPending: boolean; error: Error | null;
  onSubmit: (data: { creator_id: string; name?: string }) => void;
  onClose: () => void;
}) {
  const [creatorId, setCreatorId] = useState(""); const [name, setName] = useState("");
  const creators = useQuery({ queryKey: queryKeys.creators.all, queryFn: () => api.listCreators() });
  return (
    <div className="space-y-4">
      <div>
        <label className="block text-sm font-medium mb-1">Creator *</label>
        <select value={creatorId} onChange={(e) => setCreatorId(e.target.value)} className="w-full border rounded px-3 py-2 text-sm dark:bg-slate-700 dark:text-white">
          <option value="">Select a creator...</option>
          {creators.data?.map((c) => <option key={c.id} value={c.id}>{c.display_name || c.name}</option>)}
        </select>
      </div>
      <div><label className="block text-sm font-medium mb-1">Label</label><input value={name} onChange={(e) => setName(e.target.value)} className="w-full border rounded px-3 py-2 text-sm dark:bg-slate-700 dark:text-white" placeholder="Optional label" /></div>
      <div className="flex justify-end gap-3 pt-2">
        <button onClick={onClose} className="px-4 py-2 text-sm border rounded hover:bg-gray-50 dark:hover:bg-slate-700 dark:text-gray-300">Cancel</button>
        <button onClick={() => onSubmit({ creator_id: creatorId, name: name || undefined })} disabled={!creatorId || isPending}
          className="px-4 py-2 text-sm bg-slate-900 dark:bg-slate-700 text-white rounded hover:bg-slate-800 dark:hover:bg-slate-600 disabled:opacity-50">
          {isPending ? "Creating..." : "Subscribe"}
        </button>
      </div>
      {error && <p className="text-red-600 text-sm">{error.message}</p>}
    </div>
  );
}

export default function SubscriptionsPage() {
  const router = useRouter();
  const [search, setSearch] = useState("");
  const [filter, setFilter] = useState<FilterMode>("all");
  const [showCreate, setShowCreate] = useState(false);
  const [deleteId, setDeleteId] = useState<string | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [confirmBatchDel, setConfirmBatchDel] = useState(false);

  const subs = useQuery({ queryKey: queryKeys.subscriptions.all, queryFn: () => api.listSubscriptions() });
  const creators = useQuery({ queryKey: queryKeys.creators.all, queryFn: () => api.listCreators() });

  const create = useMutation({
    mutationFn: (data: { creator_id: string; name?: string }) => api.createSubscription(data),
    onSuccess: () => { setShowCreate(false); subs.refetch(); },
  });

  const del = useMutation({
    mutationFn: (id: string) => api.deleteSubscription(id),
    onSuccess: () => { setDeleteId(null); subs.refetch(); },
  });

  const batchDel = useMutation({
    mutationFn: (ids: string[]) => api.batchDeleteSubscriptions(ids),
    onSuccess: () => { setSelected(new Set()); setConfirmBatchDel(false); subs.refetch(); },
  });

  const batchSync = useMutation({
    mutationFn: (params: { ids: string[]; enable: boolean }) => api.batchToggleSyncSubscriptions(params.ids, params.enable),
    onSuccess: () => { setSelected(new Set()); subs.refetch(); },
  });

  const creatorMap = useMemo(() => {
    const m = new Map<string, string>();
    creators.data?.forEach((c) => m.set(c.id, c.display_name || c.name));
    return m;
  }, [creators.data]);

  const filtered = useMemo(() => {
    let list = subs.data || [];
    if (search) {
      const q = search.toLowerCase();
      list = list.filter((s) => {
        const cn = creatorMap.get(s.creator_id) || "";
        return cn.toLowerCase().includes(q) || (s.name || "").toLowerCase().includes(q);
      });
    }
    switch (filter) {
      case "active": list = list.filter((s) => s.is_active); break;
      case "inactive": list = list.filter((s) => !s.is_active); break;
      case "sync_on": list = list.filter((s) => s.sync_enabled); break;
      case "sync_off": list = list.filter((s) => !s.sync_enabled); break;
      case "never_synced": list = list.filter((s) => !s.last_synced_at); break;
    }
    return list;
  }, [subs.data, search, filter, creatorMap]);

  const toggleSelect = (id: string) => {
    const next = new Set(selected);
    next.has(id) ? next.delete(id) : next.add(id);
    setSelected(next);
  };
  const selectAll = () => {
    if (selected.size === filtered.length) setSelected(new Set());
    else setSelected(new Set(filtered.map((s) => s.id)));
  };

  return (
    <main className="max-w-6xl mx-auto p-6">
      <PageHeader title="Subscriptions" description={`${filtered.length} of ${subs.data?.length || 0} subscriptions`}>
        <button onClick={() => setShowCreate(true)} className="px-4 py-2 bg-slate-900 dark:bg-slate-700 text-white rounded text-sm hover:bg-slate-800 dark:hover:bg-slate-600">+ New</button>
      </PageHeader>

      {/* Toolbar */}
      <div className="flex items-center gap-3 mb-4 flex-wrap">
        <input value={search} onChange={(e) => setSearch(e.target.value)} placeholder="Search..." className="border rounded px-3 py-2 text-sm w-48 dark:bg-slate-700 dark:text-white dark:border-slate-600" />
        <div className="flex gap-1 bg-gray-100 dark:bg-slate-700 rounded p-0.5">
          {FILTERS.map((f) => (
            <button key={f.key} onClick={() => setFilter(f.key)}
              className={`px-3 py-1 text-xs rounded transition-colors ${filter === f.key ? "bg-white dark:bg-slate-600 shadow-sm font-medium" : "text-gray-500 hover:text-gray-700 dark:text-gray-400"}`}>
              {f.label}
            </button>
          ))}
        </div>
        <div className="flex-1" />
        {selected.size > 0 && (
          <div className="flex gap-2">
            <button onClick={() => batchSync.mutate({ ids: [...selected], enable: true })} disabled={batchSync.isPending}
              className="px-3 py-1.5 text-xs bg-green-600 text-white rounded hover:bg-green-700 disabled:opacity-50">
              Enable Sync
            </button>
            <button onClick={() => batchSync.mutate({ ids: [...selected], enable: false })} disabled={batchSync.isPending}
              className="px-3 py-1.5 text-xs bg-gray-600 text-white rounded hover:bg-gray-700 disabled:opacity-50">
              Disable Sync
            </button>
            <button onClick={() => setConfirmBatchDel(true)} className="px-3 py-1.5 text-xs bg-red-600 text-white rounded hover:bg-red-700">
              Delete {selected.size}
            </button>
          </div>
        )}
      </div>

      {/* Select all */}
      {filtered.length > 0 && (
        <label className="flex items-center gap-2 mb-2 text-xs text-gray-500 dark:text-gray-400 cursor-pointer">
          <input type="checkbox" checked={selected.size === filtered.length && filtered.length > 0} onChange={selectAll} className="rounded" />
          Select all
        </label>
      )}

      {/* Content */}
      {subs.isLoading && <div className="space-y-2">{Array.from({ length: 5 }).map((_, i) => <div key={i} className="h-16 bg-gray-100 dark:bg-slate-700 rounded animate-pulse" />)}</div>}
      {subs.error && <ErrorState message={(subs.error as Error).message} />}
      {subs.data && !subs.data.length && <EmptyState title="No subscriptions" description="Create a subscription to start tracking a creator." action={<button onClick={() => setShowCreate(true)} className="px-4 py-2 bg-slate-900 dark:bg-slate-700 text-white rounded text-sm">Create Subscription</button>} />}

      {filtered.length > 0 && (
        <div className="space-y-1">
          {filtered.map((s: Subscription) => (
            <div key={s.id} className={`bg-white dark:bg-slate-800 rounded-lg shadow-sm p-3 flex items-center gap-3 transition-colors ${selected.has(s.id) ? "ring-2 ring-blue-500" : ""}`}>
              <input type="checkbox" checked={selected.has(s.id)} onChange={() => toggleSelect(s.id)} className="rounded shrink-0" onClick={(e) => e.stopPropagation()} />
              <div className="flex-1 min-w-0 cursor-pointer" onClick={() => router.push(`/admin/subscriptions/${s.id}`)}>
                <div className="flex items-center gap-2">
                  <span className="font-medium text-sm truncate">{s.name || creatorMap.get(s.creator_id) || s.creator_id.slice(0, 8)}</span>
                  {s.is_active ? <span className="w-1.5 h-1.5 bg-green-500 rounded-full shrink-0" /> : <span className="w-1.5 h-1.5 bg-gray-300 rounded-full shrink-0" />}
                </div>
                <div className="text-xs text-gray-400 dark:text-gray-500">Creator: {creatorMap.get(s.creator_id) || s.creator_id.slice(0, 8)}</div>
              </div>
              <div className="flex items-center gap-3 shrink-0 text-xs">
                {s.sync_enabled ? <span className="text-green-600 dark:text-green-400">Auto-sync</span> : <span className="text-gray-400">Manual</span>}
                <span className="text-gray-400 dark:text-gray-500">{s.sync_interval_hours}h</span>
                <span className="text-gray-400 dark:text-gray-500 hidden sm:inline">{s.last_synced_at ? `Last: ${new Date(s.last_synced_at).toLocaleDateString()}` : "Never"}</span>
                <button onClick={() => router.push(`/admin/subscriptions/${s.id}`)} className="text-blue-600 hover:underline">View</button>
                <button onClick={(e) => { e.stopPropagation(); setDeleteId(s.id); }} className="text-red-500 hover:text-red-700 dark:text-red-400">Del</button>
              </div>
            </div>
          ))}
        </div>
      )}

      <Modal open={showCreate} onClose={() => setShowCreate(false)} title="New Subscription">
        <CreateForm isPending={create.isPending} error={create.error} onSubmit={(data) => create.mutate(data)} onClose={() => setShowCreate(false)} />
      </Modal>
      {deleteId && <ConfirmDialog open title="Delete Subscription" message="This will permanently delete the subscription and its source configurations." onConfirm={() => del.mutate(deleteId)} onCancel={() => setDeleteId(null)} isPending={del.isPending} error={(del.error as Error)?.message} />}
      {confirmBatchDel && <ConfirmDialog open title="Batch Delete" message={`Delete ${selected.size} subscriptions? This is irreversible.`} onConfirm={() => batchDel.mutate([...selected])} onCancel={() => setConfirmBatchDel(false)} isPending={batchDel.isPending} error={(batchDel.error as Error)?.message} />}
    </main>
  );
}

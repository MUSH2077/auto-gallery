"use client";
import { useState } from "react";
import { useRouter } from "next/navigation";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api, queryKeys, Subscription } from "@/lib/api";
import { PageHeader, StatusBadge, EmptyState, ErrorState, ConfirmDialog, Modal } from "@/components";

function CreateForm({ onClose }: { onClose: () => void }) {
  const [creatorId, setCreatorId] = useState(""); const [name, setName] = useState("");
  const qc = useQueryClient();
  const creators = useQuery({ queryKey: queryKeys.creators.all, queryFn: () => api.listCreators() });
  const create = useMutation({
    mutationFn: () => api.createSubscription({ creator_id: creatorId, name: name || undefined }),
    onSuccess: () => { qc.invalidateQueries({ queryKey: queryKeys.subscriptions.all }); onClose(); },
  });
  return (
    <div className="space-y-4">
      <div>
        <label className="block text-sm font-medium mb-1">Creator *</label>
        <select value={creatorId} onChange={(e) => setCreatorId(e.target.value)} className="w-full border rounded px-3 py-2 text-sm">
          <option value="">Select a creator...</option>
          {creators.data?.map((c) => <option key={c.id} value={c.id}>{c.display_name || c.name}</option>)}
        </select>
      </div>
      <div><label className="block text-sm font-medium mb-1">Label</label><input value={name} onChange={(e) => setName(e.target.value)} className="w-full border rounded px-3 py-2 text-sm" placeholder="Optional label" /></div>
      <div className="flex justify-end gap-3 pt-2">
        <button onClick={onClose} className="px-4 py-2 text-sm border rounded hover:bg-gray-50">Cancel</button>
        <button onClick={() => create.mutate()} disabled={!creatorId || create.isPending} className="px-4 py-2 text-sm bg-slate-900 text-white rounded hover:bg-slate-800 disabled:opacity-50">{create.isPending ? "Creating..." : "Subscribe"}</button>
      </div>
      {create.error && <p className="text-red-600 text-sm">{(create.error as Error).message}</p>}
    </div>
  );
}

export default function SubscriptionsPage() {
  const router = useRouter(); const qc = useQueryClient();
  const [showCreate, setShowCreate] = useState(false);
  const [deleteId, setDeleteId] = useState<string | null>(null);

  const subs = useQuery({ queryKey: queryKeys.subscriptions.all, queryFn: () => api.listSubscriptions() });
  const creators = useQuery({ queryKey: queryKeys.creators.all, queryFn: () => api.listCreators() });
  const del = useMutation({
    mutationFn: (id: string) => api.deleteSubscription(id),
    onSuccess: () => { qc.invalidateQueries({ queryKey: queryKeys.subscriptions.all }); setDeleteId(null); },
  });

  const getCreatorName = (creatorId: string) => {
    const c = creators.data?.find((c) => c.id === creatorId);
    return c ? (c.display_name || c.name) : creatorId.slice(0, 8);
  };

  return (
    <main className="max-w-6xl mx-auto p-6">
      <PageHeader title="Subscriptions" description={`${subs.data?.length || 0} subscriptions`}>
        <button onClick={() => setShowCreate(true)} className="px-4 py-2 bg-slate-900 text-white rounded text-sm hover:bg-slate-800">+ New Subscription</button>
      </PageHeader>

      {subs.isLoading && <div className="space-y-2">{Array.from({ length: 5 }).map((_, i) => <div key={i} className="h-16 bg-gray-100 rounded animate-pulse" />)}</div>}
      {subs.error && <ErrorState message={(subs.error as Error).message} />}
      {subs.data && !subs.data.length && <EmptyState title="No subscriptions" description="Create a subscription to start tracking a creator." action={<button onClick={() => setShowCreate(true)} className="px-4 py-2 bg-slate-900 text-white rounded text-sm">Create Subscription</button>} />}

      {subs.data && subs.data.length > 0 && (
        <div className="space-y-2">
          {subs.data.map((s: Subscription) => (
            <div key={s.id} className="bg-white rounded-lg shadow p-4 flex items-center justify-between hover:shadow-md cursor-pointer" onClick={() => router.push(`/admin/subscriptions/${s.id}`)}>
              <div>
                <div className="font-medium">{s.name || getCreatorName(s.creator_id)}</div>
                <div className="text-xs text-gray-400">Creator: {getCreatorName(s.creator_id)}</div>
              </div>
              <div className="flex items-center gap-3">
                <StatusBadge status={s.is_active ? "up" : "down"} />
                {s.sync_enabled ? <span className="text-xs text-green-600">Auto-sync</span> : <span className="text-xs text-gray-400">Manual</span>}
                <span className="text-xs text-gray-400">{s.last_synced_at ? `Last: ${new Date(s.last_synced_at).toLocaleDateString()}` : "Never synced"}</span>
                <button onClick={(e) => { e.stopPropagation(); setDeleteId(s.id); }} className="text-xs text-red-500 hover:text-red-700 px-2 py-1 border border-red-200 rounded hover:bg-red-50">Delete</button>
              </div>
            </div>
          ))}
        </div>
      )}

      <Modal open={showCreate} onClose={() => setShowCreate(false)} title="New Subscription"><CreateForm onClose={() => setShowCreate(false)} /></Modal>
      {deleteId && <ConfirmDialog open title="Delete Subscription" message="This will permanently delete the subscription and its source configurations." onConfirm={() => del.mutate(deleteId)} onCancel={() => setDeleteId(null)} isPending={del.isPending} error={(del.error as Error)?.message} />}
    </main>
  );
}

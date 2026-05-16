"use client";
import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api, queryKeys, Creator } from "@/lib/api";
import { PageHeader, StatusBadge, EmptyState, ErrorState, ConfirmDialog, Modal } from "@/components";
import { useRouter } from "next/navigation";

function CreateForm({ onClose }: { onClose: () => void }) {
  const [name, setName] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [description, setDescription] = useState("");
  const qc = useQueryClient();
  const create = useMutation({
    mutationFn: () => api.createCreator({ name, display_name: displayName || undefined, description: description || undefined }),
    onSuccess: () => { qc.invalidateQueries({ queryKey: queryKeys.creators.all }); onClose(); },
  });
  return (
    <div className="space-y-4">
      <div><label className="block text-sm font-medium mb-1">Name *</label><input value={name} onChange={(e) => setName(e.target.value)} className="w-full border rounded px-3 py-2 text-sm" placeholder="Creator name" /></div>
      <div><label className="block text-sm font-medium mb-1">Display Name</label><input value={displayName} onChange={(e) => setDisplayName(e.target.value)} className="w-full border rounded px-3 py-2 text-sm" /></div>
      <div><label className="block text-sm font-medium mb-1">Description</label><textarea value={description} onChange={(e) => setDescription(e.target.value)} className="w-full border rounded px-3 py-2 text-sm" rows={3} /></div>
      <div className="flex justify-end gap-3 pt-2">
        <button onClick={onClose} className="px-4 py-2 text-sm border rounded hover:bg-gray-50">Cancel</button>
        <button onClick={() => create.mutate()} disabled={!name || create.isPending} className="px-4 py-2 text-sm bg-slate-900 text-white rounded hover:bg-slate-800 disabled:opacity-50">{create.isPending ? "Creating..." : "Create"}</button>
      </div>
      {create.error && <p className="text-red-600 text-sm">{(create.error as Error).message}</p>}
    </div>
  );
}

export default function CreatorsPage() {
  const router = useRouter();
  const qc = useQueryClient();
  const [search, setSearch] = useState("");
  const [showCreate, setShowCreate] = useState(false);
  const [deleteId, setDeleteId] = useState<string | null>(null);

  const creators = useQuery({ queryKey: queryKeys.creators.all, queryFn: () => api.listCreators() });
  const del = useMutation({
    mutationFn: (id: string) => api.deleteCreator(id),
    onSuccess: () => { qc.invalidateQueries({ queryKey: queryKeys.creators.all }); setDeleteId(null); },
  });

  const filtered = creators.data?.filter((c) =>
    !search || c.name.toLowerCase().includes(search.toLowerCase()) || (c.display_name && c.display_name.toLowerCase().includes(search.toLowerCase()))
  );

  return (
    <main className="max-w-6xl mx-auto p-6">
      <PageHeader title="Creators" description={`${creators.data?.length || 0} creators`}>
        <button onClick={() => setShowCreate(true)} className="px-4 py-2 bg-slate-900 text-white rounded text-sm hover:bg-slate-800">+ New Creator</button>
      </PageHeader>

      <div className="mb-4"><input value={search} onChange={(e) => setSearch(e.target.value)} placeholder="Search creators..." className="w-full max-w-md border rounded px-3 py-2 text-sm" /></div>

      {creators.isLoading && <div className="space-y-2">{Array.from({ length: 5 }).map((_, i) => <div key={i} className="h-16 bg-gray-100 rounded animate-pulse" />)}</div>}
      {creators.error && <ErrorState message={(creators.error as Error).message} onRetry={() => creators.refetch()} />}
      {creators.data && !creators.data.length && <EmptyState title="No creators" description="Create your first canonical creator." action={<button onClick={() => setShowCreate(true)} className="px-4 py-2 bg-slate-900 text-white rounded text-sm">Create Creator</button>} />}

      {filtered && filtered.length > 0 && (
        <div className="space-y-2">
          {filtered.map((c) => (
            <div key={c.id} className="bg-white rounded-lg shadow p-4 flex items-center justify-between hover:shadow-md cursor-pointer" onClick={() => router.push(`/admin/creators/${c.id}`)}>
              <div>
                <div className="font-medium">{c.display_name || c.name}</div>
                {c.display_name && <div className="text-xs text-gray-400">{c.name}</div>}
                {c.description && <p className="text-sm text-gray-500 mt-1 line-clamp-1">{c.description}</p>}
              </div>
              <div className="flex items-center gap-3">
                <StatusBadge status={c.is_active ? "up" : "down"} />
                <button onClick={(e) => { e.stopPropagation(); setDeleteId(c.id); }}
                  className="text-xs text-red-500 hover:text-red-700 px-2 py-1 border border-red-200 rounded hover:bg-red-50">Delete</button>
              </div>
            </div>
          ))}
        </div>
      )}

      <Modal open={showCreate} onClose={() => setShowCreate(false)} title="New Creator"><CreateForm onClose={() => setShowCreate(false)} /></Modal>
      {deleteId && <ConfirmDialog open title="Delete Creator" message="This will permanently delete the creator and all associated links. This action cannot be undone." onConfirm={() => del.mutate(deleteId)} onCancel={() => setDeleteId(null)} isPending={del.isPending} error={(del.error as Error)?.message} />}
    </main>
  );
}

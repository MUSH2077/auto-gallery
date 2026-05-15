"use client";
import { useState } from "react";
import { useParams, useRouter } from "next/navigation";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api, queryKeys, SubscriptionSource as SS, ProviderInfo } from "@/lib/api";
import { PageHeader, StatusBadge, SourceBadge, Modal, ConfirmDialog } from "@/components";

function AddSourceForm({ subId, onClose }: { subId: string; onClose: () => void }) {
  const [source, setSource] = useState("pixiv"); const [sourceUrl, setSourceUrl] = useState(""); const [sourceCreatorId, setSourceCreatorId] = useState("");
  const qc = useQueryClient();
  const create = useMutation({
    mutationFn: () => api.createSubscriptionSource(subId, { source, source_url: sourceUrl || undefined, source_creator_id: sourceCreatorId || undefined, is_enabled: true }),
    onSuccess: () => { qc.invalidateQueries({ queryKey: queryKeys.subscriptions.sources(subId) }); onClose(); },
  });
  const sources = useQuery({ queryKey: queryKeys.sources, queryFn: api.sources });

  return (
    <div className="space-y-4">
      <div>
        <label className="block text-sm font-medium mb-1">Source *</label>
        <select value={source} onChange={(e) => setSource(e.target.value)} className="w-full border rounded px-3 py-2 text-sm">
          {sources.data?.sources?.filter((s: ProviderInfo) => s.capabilities.can_download || s.capabilities.can_import_local).map((s: ProviderInfo) => <option key={s.source_name} value={s.source_name}>{s.display_name} ({s.source_name})</option>)}
        </select>
        <p className="text-xs text-gray-400 mt-1">Each subscription can sync from multiple sources. Select the source platform to enable.</p>
      </div>
      <div><label className="block text-sm font-medium mb-1">Source URL</label><input value={sourceUrl} onChange={(e) => setSourceUrl(e.target.value)} className="w-full border rounded px-3 py-2 text-sm" placeholder="https://..." /></div>
      <div><label className="block text-sm font-medium mb-1">Source Creator ID</label><input value={sourceCreatorId} onChange={(e) => setSourceCreatorId(e.target.value)} className="w-full border rounded px-3 py-2 text-sm" placeholder="e.g. 123456" /></div>
      <div className="flex justify-end gap-3 pt-2">
        <button onClick={onClose} className="px-4 py-2 text-sm border rounded hover:bg-gray-50">Cancel</button>
        <button onClick={() => create.mutate()} disabled={create.isPending} className="px-4 py-2 text-sm bg-slate-900 text-white rounded hover:bg-slate-800 disabled:opacity-50">{create.isPending ? "Adding..." : "Add Source"}</button>
      </div>
      {create.error && <p className="text-red-600 text-sm">{(create.error as Error).message}</p>}
    </div>
  );
}

export default function SubscriptionDetailPage() {
  const params = useParams(); const router = useRouter(); const qc = useQueryClient();
  const id = params.id as string;

  const sub = useQuery({ queryKey: queryKeys.subscriptions.detail(id), queryFn: () => api.getSubscription(id) });
  const sources = useQuery({ queryKey: queryKeys.subscriptions.sources(id), queryFn: () => api.listSubscriptionSources(id) });
  const creators = useQuery({ queryKey: queryKeys.creators.all, queryFn: () => api.listCreators() });
  const [showAddSource, setShowAddSource] = useState(false);
  const [editing, setEditing] = useState(false); const [editName, setEditName] = useState("");
  const [deleteSsId, setDeleteSsId] = useState<string | null>(null);
  const [toggleId, setToggleId] = useState<string | null>(null);

  const update = useMutation({
    mutationFn: (data: Record<string, unknown>) => api.updateSubscription(id, data),
    onSuccess: () => { qc.invalidateQueries({ queryKey: queryKeys.subscriptions.detail(id) }); setEditing(false); },
  });
  const toggleSource = useMutation({
    mutationFn: ({ ssId, enabled }: { ssId: string; enabled: boolean }) => api.updateSubscriptionSource(id, ssId, { is_enabled: enabled }),
    onSuccess: () => { sources.refetch(); setToggleId(null); },
  });
  const deleteSource = useMutation({
    mutationFn: (ssId: string) => api.deleteSubscriptionSource(id, ssId),
    onSuccess: () => { sources.refetch(); setDeleteSsId(null); },
  });
  const startSync = useMutation({
    mutationFn: (ssId: string) => api.createDownloadJob({ subscription_id: id, subscription_source_id: ssId, source: "", source_url: "" }),
    onSuccess: (data) => alert(`Download job created: ${data.job_id}`),
  });

  const getCreatorName = (creatorId: string) => {
    const c = creators.data?.find((c) => c.id === creatorId);
    return c ? (c.display_name || c.name) : creatorId.slice(0, 8);
  };

  if (sub.isLoading) return <main className="max-w-4xl mx-auto p-6"><div className="animate-pulse space-y-4"><div className="h-8 bg-gray-200 rounded w-1/4" /><div className="h-32 bg-gray-200 rounded" /></div></main>;
  if (sub.error) return <main className="max-w-4xl mx-auto p-6"><div className="p-4 bg-red-50 border border-red-200 rounded-lg text-red-700">{(sub.error as Error).message}</div></main>;
  if (!sub.data) return null;
  const s = sub.data;

  return (
    <main className="max-w-4xl mx-auto p-6">
      <PageHeader title={s.name || `Subscription: ${getCreatorName(s.creator_id)}`} description={`Creator: ${getCreatorName(s.creator_id)}`}>
        <div className="flex gap-2">
          <button onClick={() => { setEditName(s.name || ""); setEditing(true); }} className="px-3 py-2 text-sm bg-slate-900 text-white rounded hover:bg-slate-800">Edit</button>
        </div>
      </PageHeader>

      <div className="grid grid-cols-1 md:grid-cols-3 gap-6 mb-8">
        <div className="col-span-2">
          <div className="bg-white rounded-lg shadow p-4 mb-4">
            <h3 className="font-medium mb-3">Details</h3>
            <dl className="text-sm space-y-2">
              <div className="flex gap-2"><dt className="text-gray-500 w-28">Creator:</dt><dd className="cursor-pointer text-blue-600 hover:underline" onClick={() => router.push(`/admin/creators/${s.creator_id}`)}>{getCreatorName(s.creator_id)}</dd></div>
              <div className="flex gap-2"><dt className="text-gray-500 w-28">Status:</dt><dd><StatusBadge status={s.is_active ? "up" : "down"} /></dd></div>
              <div className="flex gap-2"><dt className="text-gray-500 w-28">Auto-sync:</dt><dd>{s.sync_enabled ? <span className="text-green-600">Enabled</span> : <span className="text-gray-400">Manual only</span>}</dd></div>
              <div className="flex gap-2"><dt className="text-gray-500 w-28">Last synced:</dt><dd className="text-xs">{s.last_synced_at ? new Date(s.last_synced_at).toLocaleString() : "Never"}</dd></div>
              <div className="flex gap-2"><dt className="text-gray-500 w-28">Created:</dt><dd className="text-xs">{new Date(s.created_at).toLocaleString()}</dd></div>
            </dl>
          </div>

          <div className="bg-white rounded-lg shadow p-4">
            <div className="flex items-center justify-between mb-3">
              <h3 className="font-medium">Sync Sources ({sources.data?.length || 0})</h3>
              <button onClick={() => setShowAddSource(true)} className="text-xs px-3 py-1 bg-slate-900 text-white rounded hover:bg-slate-800">+ Add Source</button>
            </div>
            {sources.data && sources.data.length > 0 ? (
              <div className="space-y-2">
                {sources.data.map((ss: SS) => (
                  <div key={ss.id} className="border rounded-lg p-3 text-sm">
                    <div className="flex items-center justify-between mb-2">
                      <div className="flex items-center gap-2">
                        <SourceBadge source={ss.source} />
                        <span className="font-medium">{ss.source_creator_id || "—"}</span>
                      </div>
                      <div className="flex items-center gap-2">
                        <button onClick={() => setToggleId(ss.id)}
                          className={`text-xs px-2 py-0.5 rounded ${ss.is_enabled ? "bg-green-100 text-green-700" : "bg-gray-100 text-gray-500"}`}>
                          {ss.is_enabled ? "Enabled" : "Disabled"}
                        </button>
                        <button onClick={() => startSync.mutate(ss.id)} disabled={startSync.isPending}
                          className="text-xs px-2 py-0.5 bg-blue-100 text-blue-700 rounded hover:bg-blue-200 disabled:opacity-50">
                          {startSync.isPending ? "Syncing..." : "Sync Now"}
                        </button>
                        <button onClick={() => setDeleteSsId(ss.id)} className="text-xs text-red-500 hover:text-red-700">Remove</button>
                      </div>
                    </div>
                    <div className="text-xs text-gray-500 space-y-1">
                      {ss.source_url && <div>URL: <span className="text-blue-600">{ss.source_url}</span></div>}
                      <div className="flex gap-4">
                        <span>Auth: <StatusBadge status={ss.auth_healthy ? "up" : "down"} /></span>
                        {ss.last_successful_auth && <span>Last auth: {new Date(ss.last_successful_auth).toLocaleDateString()}</span>}
                      </div>
                    </div>
                  </div>
                ))}
              </div>
            ) : (
              <p className="text-sm text-gray-400">No sources configured. Add a source to enable syncing from that platform.</p>
            )}
          </div>
        </div>

        <div className="bg-blue-50 border border-blue-200 rounded-lg p-4 h-fit text-sm">
          <h4 className="font-medium text-blue-800 mb-2">Multi-Source Design</h4>
          <p className="text-blue-700">A subscription follows a canonical creator. Each subscription can have multiple sync sources enabled. This means one subscription can sync from Pixiv, X, Iwara, and other platforms simultaneously.</p>
        </div>
      </div>

      <Modal open={editing} onClose={() => setEditing(false)} title="Edit Subscription">
        <div className="space-y-4">
          <div><label className="block text-sm font-medium mb-1">Label</label><input value={editName} onChange={(e) => setEditName(e.target.value)} className="w-full border rounded px-3 py-2 text-sm" /></div>
          <div className="flex justify-end gap-3 pt-2">
            <button onClick={() => setEditing(false)} className="px-4 py-2 text-sm border rounded hover:bg-gray-50">Cancel</button>
            <button onClick={() => update.mutate({ name: editName || undefined })} disabled={update.isPending} className="px-4 py-2 text-sm bg-slate-900 text-white rounded hover:bg-slate-800">Save</button>
          </div>
        </div>
      </Modal>

      <Modal open={showAddSource} onClose={() => setShowAddSource(false)} title="Add Sync Source"><AddSourceForm subId={id} onClose={() => setShowAddSource(false)} /></Modal>
      {toggleId && <ConfirmDialog open title={sources.data?.find((ss: SS) => ss.id === toggleId)?.is_enabled ? "Disable Source" : "Enable Source"} message="Toggle this source's sync status?" onConfirm={() => { const ss = sources.data?.find((s: SS) => s.id === toggleId); if (ss) toggleSource.mutate({ ssId: toggleId, enabled: !ss.is_enabled }); }} onCancel={() => setToggleId(null)} />}
      {deleteSsId && <ConfirmDialog open title="Remove Source" message="Remove this source configuration? This cannot be undone." onConfirm={() => deleteSource.mutate(deleteSsId)} onCancel={() => setDeleteSsId(null)} />}
    </main>
  );
}

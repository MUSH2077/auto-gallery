"use client";
import { useState } from "react";
import { useParams, useRouter } from "next/navigation";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api, queryKeys, CreatorLink as CreatorLinkType, SourceCreator as SourceCreatorType } from "@/lib/api";
import { PageHeader, StatusBadge, SourceBadge, EmptyState, Modal, ConfirmDialog } from "@/components";

function AddLinkForm({ creatorId, onClose }: { creatorId: string; onClose: () => void }) {
  const [url, setUrl] = useState(""); const [linkType, setLinkType] = useState("website"); const [source, setSource] = useState("");
  const qc = useQueryClient();
  const create = useMutation({
    mutationFn: () => api.createCreatorLink(creatorId, { url, link_type: linkType, source: source || undefined }),
    onSuccess: () => { qc.invalidateQueries({ queryKey: queryKeys.creators.links(creatorId) }); onClose(); },
  });
  return (
    <div className="space-y-4">
      <div><label className="block text-sm font-medium mb-1">URL *</label><input value={url} onChange={(e) => setUrl(e.target.value)} className="w-full border rounded px-3 py-2 text-sm" placeholder="https://..." /></div>
      <div className="grid grid-cols-2 gap-4">
        <div><label className="block text-sm font-medium mb-1">Type</label>
          <select value={linkType} onChange={(e) => setLinkType(e.target.value)} className="w-full border rounded px-3 py-2 text-sm">
            <option value="website">Website</option><option value="pixiv">Pixiv</option><option value="x">X / Twitter</option><option value="iwara">Iwara</option><option value="danbooru">Danbooru</option><option value="other">Other</option>
          </select>
        </div>
        <div><label className="block text-sm font-medium mb-1">Source</label><input value={source} onChange={(e) => setSource(e.target.value)} className="w-full border rounded px-3 py-2 text-sm" placeholder="e.g. pixiv" /></div>
      </div>
      <div className="flex justify-end gap-3 pt-2">
        <button onClick={onClose} className="px-4 py-2 text-sm border rounded hover:bg-gray-50">Cancel</button>
        <button onClick={() => create.mutate()} disabled={!url || create.isPending} className="px-4 py-2 text-sm bg-slate-900 text-white rounded hover:bg-slate-800 disabled:opacity-50">{create.isPending ? "Adding..." : "Add Link"}</button>
      </div>
    </div>
  );
}

function AddSourceForm({ creatorId, onClose }: { creatorId: string; onClose: () => void }) {
  const [source, setSource] = useState("pixiv"); const [sourceCreatorId, setSourceCreatorId] = useState(""); const [sourceUrl, setSourceUrl] = useState(""); const [displayName, setDisplayName] = useState("");
  const qc = useQueryClient();
  const create = useMutation({
    mutationFn: () => api.createSourceCreator(creatorId, { source, source_creator_id: sourceCreatorId, source_url: sourceUrl || undefined, display_name: displayName || undefined }),
    onSuccess: () => { qc.invalidateQueries({ queryKey: queryKeys.creators.detail(creatorId) }); onClose(); },
  });
  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 gap-4">
        <div><label className="block text-sm font-medium mb-1">Source *</label>
          <select value={source} onChange={(e) => setSource(e.target.value)} className="w-full border rounded px-3 py-2 text-sm">
            <option value="pixiv">Pixiv</option><option value="x">X / Twitter</option><option value="iwara">Iwara</option><option value="local">Local</option><option value="manual">Manual</option>
          </select>
        </div>
        <div><label className="block text-sm font-medium mb-1">Source Creator ID *</label><input value={sourceCreatorId} onChange={(e) => setSourceCreatorId(e.target.value)} className="w-full border rounded px-3 py-2 text-sm" placeholder="e.g. 123456" /></div>
      </div>
      <div><label className="block text-sm font-medium mb-1">Source URL</label><input value={sourceUrl} onChange={(e) => setSourceUrl(e.target.value)} className="w-full border rounded px-3 py-2 text-sm" placeholder="https://..." /></div>
      <div><label className="block text-sm font-medium mb-1">Display Name</label><input value={displayName} onChange={(e) => setDisplayName(e.target.value)} className="w-full border rounded px-3 py-2 text-sm" /></div>
      <div className="flex justify-end gap-3 pt-2">
        <button onClick={onClose} className="px-4 py-2 text-sm border rounded hover:bg-gray-50">Cancel</button>
        <button onClick={() => create.mutate()} disabled={!sourceCreatorId || create.isPending} className="px-4 py-2 text-sm bg-slate-900 text-white rounded hover:bg-slate-800 disabled:opacity-50">{create.isPending ? "Adding..." : "Add Source"}</button>
      </div>
    </div>
  );
}

export default function CreatorDetailPage() {
  const params = useParams(); const router = useRouter(); const qc = useQueryClient();
  const id = params.id as string;

  const creator = useQuery({ queryKey: queryKeys.creators.detail(id), queryFn: () => api.getCreator(id) });
  const links = useQuery({ queryKey: queryKeys.creators.links(id), queryFn: () => api.listCreatorLinks(id) });
  const [showAddLink, setShowAddLink] = useState(false);
  const [showAddSource, setShowAddSource] = useState(false);
  const [editing, setEditing] = useState(false);
  const [editName, setEditName] = useState(""); const [editDisplay, setEditDisplay] = useState(""); const [editDesc, setEditDesc] = useState("");

  const openEdit = () => {
    if (!creator.data) return;
    setEditName(creator.data.name); setEditDisplay(creator.data.display_name || ""); setEditDesc(creator.data.description || ""); setEditing(true);
  };
  const update = useMutation({
    mutationFn: () => api.updateCreator(id, { name: editName, display_name: editDisplay || undefined, description: editDesc || undefined }),
    onSuccess: () => { qc.invalidateQueries({ queryKey: queryKeys.creators.detail(id) }); setEditing(false); },
  });

  const verifyLink = useMutation({
    mutationFn: (linkId: string) => api.updateCreatorLink(id, linkId, { is_verified: true, confidence: 1.0 }),
    onSuccess: () => links.refetch(),
  });

  if (creator.isLoading) return <main className="max-w-4xl mx-auto p-6"><div className="animate-pulse space-y-4"><div className="h-8 bg-gray-200 rounded w-1/4" /><div className="h-4 bg-gray-200 rounded w-1/2" /><div className="h-32 bg-gray-200 rounded" /></div></main>;
  if (creator.error) return <main className="max-w-4xl mx-auto p-6"><div className="p-4 bg-red-50 border border-red-200 rounded-lg text-red-700">{(creator.error as Error).message}</div></main>;
  if (!creator.data) return null;

  const c = creator.data;

  return (
    <main className="max-w-4xl mx-auto p-6">
      <PageHeader title={c.display_name || c.name}>
        <div className="flex gap-2">
          <button onClick={() => router.push(`/admin/creators/${id}/mapping`)} className="px-3 py-2 text-sm border rounded hover:bg-gray-50">Manage Mapping</button>
          <button onClick={openEdit} className="px-3 py-2 text-sm bg-slate-900 text-white rounded hover:bg-slate-800">Edit</button>
        </div>
      </PageHeader>

      <div className="grid grid-cols-1 md:grid-cols-3 gap-6 mb-8">
        <div className="col-span-2 space-y-4">
          <div className="bg-white rounded-lg shadow p-4">
            <h3 className="font-medium mb-2">Details</h3>
            <dl className="text-sm space-y-2">
              <div className="flex gap-2"><dt className="text-gray-500 w-24">Name:</dt><dd>{c.name}</dd></div>
              {c.display_name && <div className="flex gap-2"><dt className="text-gray-500 w-24">Display:</dt><dd>{c.display_name}</dd></div>}
              {c.description && <div className="flex gap-2"><dt className="text-gray-500 w-24">Description:</dt><dd className="whitespace-pre-wrap">{c.description}</dd></div>}
              <div className="flex gap-2"><dt className="text-gray-500 w-24">Status:</dt><dd><StatusBadge status={c.is_active ? "up" : "down"} /></dd></div>
              <div className="flex gap-2"><dt className="text-gray-500 w-24">Created:</dt><dd className="text-xs">{new Date(c.created_at).toLocaleString()}</dd></div>
            </dl>
          </div>

          <div className="bg-white rounded-lg shadow p-4">
            <div className="flex items-center justify-between mb-3"><h3 className="font-medium">Links ({links.data?.length || 0})</h3><button onClick={() => setShowAddLink(true)} className="text-xs px-3 py-1 bg-slate-900 text-white rounded hover:bg-slate-800">+ Add Link</button></div>
            {links.data && links.data.length > 0 ? (
              <div className="space-y-2">
                {links.data.map((l: CreatorLinkType) => (
                  <div key={l.id} className="flex items-center justify-between border-b pb-2 text-sm">
                    <div className="flex items-center gap-2">
                      <span className="text-xs bg-gray-100 px-2 py-0.5 rounded">{l.link_type}</span>
                      <a href={l.url} target="_blank" rel="noopener noreferrer" className="text-blue-600 hover:underline truncate max-w-xs">{l.url}</a>
                      {l.source && <SourceBadge source={l.source} />}
                    </div>
                    <div className="flex items-center gap-2">
                      <span className="text-xs text-gray-400">confidence: {l.confidence.toFixed(1)}</span>
                      {l.is_verified ? <StatusBadge status="up" /> : <button onClick={() => verifyLink.mutate(l.id)} className="text-xs text-blue-600 hover:underline">Verify</button>}
                    </div>
                  </div>
                ))}
              </div>
            ) : <p className="text-sm text-gray-400">No links yet.</p>}
          </div>
        </div>

        <div className="bg-white rounded-lg shadow p-4 h-fit">
          <div className="flex items-center justify-between mb-3"><h3 className="font-medium">Source Accounts</h3><button onClick={() => setShowAddSource(true)} className="text-xs px-3 py-1 bg-slate-900 text-white rounded hover:bg-slate-800">+ Add</button></div>
          <EmptyState icon=" " title="Source management" description="Add source accounts to link this creator across platforms. Or use the mapping page for advanced management." action={<button onClick={() => router.push(`/admin/creators/${id}/mapping`)} className="text-xs text-blue-600 hover:underline">Open Mapping Page</button>} />
        </div>
      </div>

      <Modal open={showAddLink} onClose={() => setShowAddLink(false)} title="Add Creator Link"><AddLinkForm creatorId={id} onClose={() => setShowAddLink(false)} /></Modal>
      <Modal open={showAddSource} onClose={() => setShowAddSource(false)} title="Add Source Account"><AddSourceForm creatorId={id} onClose={() => setShowAddSource(false)} /></Modal>
      <Modal open={editing} onClose={() => setEditing(false)} title="Edit Creator">
        <div className="space-y-4">
          <div><label className="block text-sm font-medium mb-1">Name</label><input value={editName} onChange={(e) => setEditName(e.target.value)} className="w-full border rounded px-3 py-2 text-sm" /></div>
          <div><label className="block text-sm font-medium mb-1">Display Name</label><input value={editDisplay} onChange={(e) => setEditDisplay(e.target.value)} className="w-full border rounded px-3 py-2 text-sm" /></div>
          <div><label className="block text-sm font-medium mb-1">Description</label><textarea value={editDesc} onChange={(e) => setEditDesc(e.target.value)} className="w-full border rounded px-3 py-2 text-sm" rows={3} /></div>
          <div className="flex justify-end gap-3 pt-2">
            <button onClick={() => setEditing(false)} className="px-4 py-2 text-sm border rounded hover:bg-gray-50">Cancel</button>
            <button onClick={() => update.mutate()} disabled={update.isPending} className="px-4 py-2 text-sm bg-slate-900 text-white rounded hover:bg-slate-800">Save</button>
          </div>
        </div>
      </Modal>
    </main>
  );
}

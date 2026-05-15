"use client";
import { useState } from "react";
import { useParams, useRouter } from "next/navigation";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api, queryKeys, CreatorLink as CreatorLinkType } from "@/lib/api";
import { PageHeader, StatusBadge, SourceBadge, Modal, ConfirmDialog } from "@/components";

function AddLinkForm({ creatorId, onClose }: { creatorId: string; onClose: () => void }) {
  const [url, setUrl] = useState(""); const [linkType, setLinkType] = useState("website"); const [source, setSource] = useState(""); const [confidence, setConfidence] = useState(1.0);
  const qc = useQueryClient();
  const create = useMutation({
    mutationFn: () => api.createCreatorLink(creatorId, { url, link_type: linkType, source: source || undefined, confidence }),
    onSuccess: () => { qc.invalidateQueries({ queryKey: queryKeys.creators.links(creatorId) }); onClose(); },
  });
  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 gap-4">
        <div><label className="block text-sm font-medium mb-1">Link Type *</label>
          <select value={linkType} onChange={(e) => setLinkType(e.target.value)} className="w-full border rounded px-3 py-2 text-sm">
            <option value="website">Website</option><option value="pixiv">Pixiv Profile</option><option value="x">X / Twitter</option><option value="iwara">Iwara Profile</option><option value="danbooru">Danbooru Artist</option><option value="other">Other</option>
          </select>
        </div>
        <div><label className="block text-sm font-medium mb-1">Source Platform</label><input value={source} onChange={(e) => setSource(e.target.value)} className="w-full border rounded px-3 py-2 text-sm" placeholder="e.g. pixiv" /></div>
      </div>
      <div><label className="block text-sm font-medium mb-1">URL *</label><input value={url} onChange={(e) => setUrl(e.target.value)} className="w-full border rounded px-3 py-2 text-sm" placeholder="https://..." /></div>
      <div><label className="block text-sm font-medium mb-1">Confidence ({confidence.toFixed(1)})</label><input type="range" min="0" max="1" step="0.1" value={confidence} onChange={(e) => setConfidence(parseFloat(e.target.value))} className="w-full" /><div className="flex justify-between text-xs text-gray-400"><span>0.0 (Suggested)</span><span>1.0 (Verified)</span></div></div>
      <div className="flex justify-end gap-3 pt-2">
        <button onClick={onClose} className="px-4 py-2 text-sm border rounded hover:bg-gray-50">Cancel</button>
        <button onClick={() => create.mutate()} disabled={!url || create.isPending} className="px-4 py-2 text-sm bg-slate-900 text-white rounded hover:bg-slate-800 disabled:opacity-50">{create.isPending ? "Adding..." : "Add Link"}</button>
      </div>
    </div>
  );
}

export default function MappingPage() {
  const params = useParams(); const router = useRouter(); const qc = useQueryClient();
  const id = params.id as string;

  const creator = useQuery({ queryKey: queryKeys.creators.detail(id), queryFn: () => api.getCreator(id) });
  const links = useQuery({ queryKey: queryKeys.creators.links(id), queryFn: () => api.listCreatorLinks(id) });
  const [showAdd, setShowAdd] = useState(false);
  const [dialog, setDialog] = useState<{ action: "verify" | "unverify"; linkId: string } | null>(null);

  const verifyLink = useMutation({
    mutationFn: (linkId: string) => api.updateCreatorLink(id, linkId, { is_verified: true, confidence: 1.0 }),
    onSuccess: () => { links.refetch(); setDialog(null); },
  });

  const unverifyLink = useMutation({
    mutationFn: (linkId: string) => api.updateCreatorLink(id, linkId, { is_verified: false, confidence: 0.5 }),
    onSuccess: () => { links.refetch(); setDialog(null); },
  });

  const verified = links.data?.filter((l: CreatorLinkType) => l.is_verified) || [];
  const suggested = links.data?.filter((l: CreatorLinkType) => !l.is_verified) || [];

  if (creator.isLoading) return <main className="max-w-4xl mx-auto p-6"><div className="animate-pulse"><div className="h-8 bg-gray-200 rounded w-1/4 mb-4" /><div className="h-32 bg-gray-200 rounded" /></div></main>;

  return (
    <main className="max-w-4xl mx-auto p-6">
      <PageHeader title={`Mapping: ${creator.data?.display_name || creator.data?.name || "Creator"}`} description="Manage multi-source identity mapping">
        <div className="flex gap-2">
          <button onClick={() => router.push(`/admin/creators/${id}`)} className="px-3 py-2 text-sm border rounded hover:bg-gray-50">Back to Creator</button>
          <button onClick={() => setShowAdd(true)} className="px-3 py-2 text-sm bg-slate-900 text-white rounded hover:bg-slate-800">+ Add Link</button>
        </div>
      </PageHeader>

      <div className="bg-blue-50 border border-blue-200 rounded-lg p-4 text-sm text-blue-800 mb-6">
        Links with confidence 1.0 are verified and used as the primary source of truth. Low-confidence links are suggestions from Danbooru or URL extraction that need admin review.
      </div>

      <section className="mb-8">
        <h2 className="font-semibold mb-3">Verified Links ({verified.length})</h2>
        <div className="space-y-2">
          {verified.map((l: CreatorLinkType) => (
            <div key={l.id} className="bg-white rounded-lg shadow p-3 flex items-center justify-between text-sm">
              <div className="flex items-center gap-3">
                <span className="text-xs bg-gray-100 px-2 py-0.5 rounded">{l.link_type}</span>
                <a href={l.url} target="_blank" rel="noopener noreferrer" className="text-blue-600 hover:underline truncate max-w-sm">{l.url}</a>
                {l.source && <SourceBadge source={l.source} />}
              </div>
              <div className="flex items-center gap-2">
                <span className="text-xs text-green-600">Verified</span>
                <button onClick={() => setDialog({ action: "unverify", linkId: l.id })} className="text-xs text-red-500 hover:underline">Unverify</button>
              </div>
            </div>
          ))}
          {!verified.length && <p className="text-sm text-gray-400">No verified links. Add and verify links to establish identity mapping.</p>}
        </div>
      </section>

      <section className="mb-8">
        <h2 className="font-semibold mb-3">Suggested Links ({suggested.length})</h2>
        <div className="space-y-2">
          {suggested.map((l: CreatorLinkType) => (
            <div key={l.id} className="bg-white rounded-lg shadow p-3 flex items-center justify-between text-sm">
              <div className="flex items-center gap-3">
                <span className="text-xs bg-yellow-100 px-2 py-0.5 rounded">{l.link_type}</span>
                <a href={l.url} target="_blank" rel="noopener noreferrer" className="text-blue-600 hover:underline truncate max-w-sm">{l.url}</a>
                {l.source && <SourceBadge source={l.source} />}
                <span className={`text-xs ${l.confidence >= 0.7 ? "text-green-600" : l.confidence >= 0.4 ? "text-yellow-600" : "text-red-600"}`}>Confidence: {l.confidence.toFixed(1)}</span>
              </div>
              <button onClick={() => setDialog({ action: "verify", linkId: l.id })} className="text-xs px-3 py-1 bg-green-100 text-green-700 rounded hover:bg-green-200">Approve</button>
            </div>
          ))}
          {!suggested.length && <p className="text-sm text-gray-400">No suggested links pending review.</p>}
        </div>
      </section>

      <section>
        <h2 className="font-semibold mb-3">Danbooru Reference</h2>
        <div className="bg-yellow-50 border border-yellow-200 rounded-lg p-4 text-sm">
          <p className="font-medium text-yellow-800 mb-2">Danbooru is a reference provider only.</p>
          <p className="text-yellow-700 mb-3">Import Danbooru artist data to suggest creator links and identity mappings. Danbooru is not treated as a complete union of all works.</p>
          <button onClick={() => router.push("/admin/reference/danbooru")} className="text-xs px-3 py-1.5 bg-yellow-600 text-white rounded hover:bg-yellow-700">Open Danbooru Reference</button>
        </div>
      </section>

      <Modal open={showAdd} onClose={() => setShowAdd(false)} title="Add Identity Link"><AddLinkForm creatorId={id} onClose={() => setShowAdd(false)} /></Modal>
      {dialog?.action === "verify" && <ConfirmDialog open title="Approve Link" message="Verify this link and set confidence to 1.0?" onConfirm={() => verifyLink.mutate(dialog.linkId)} onCancel={() => setDialog(null)} />}
      {dialog?.action === "unverify" && <ConfirmDialog open title="Unverify Link" message="Unverify this link and set confidence to 0.5?" onConfirm={() => unverifyLink.mutate(dialog.linkId)} onCancel={() => setDialog(null)} />}
    </main>
  );
}

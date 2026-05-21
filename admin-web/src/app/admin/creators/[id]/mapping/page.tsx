"use client";
import { useState } from "react";
import { useParams, useRouter } from "next/navigation";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api, queryKeys, CreatorLink as CreatorLinkType } from "@/lib/api";
import { PageHeader, StatusBadge, SourceBadge, Modal, ConfirmDialog } from "@/components";
import { useT } from "@/lib/i18n";

function AddLinkForm({ creatorId, onClose }: { creatorId: string; onClose: () => void }) {
  const t = useT();
  const [url, setUrl] = useState(""); const [linkType, setLinkType] = useState("website"); const [source, setSource] = useState(""); const [confidence, setConfidence] = useState(1.0);
  const qc = useQueryClient();
  const create = useMutation({
    mutationFn: () => api.createCreatorLink(creatorId, { url, link_type: linkType, source: source || undefined, confidence }),
    onSuccess: () => { qc.invalidateQueries({ queryKey: queryKeys.creators.links(creatorId) }); onClose(); },
  });
  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 gap-4">
        <div><label className="block text-sm font-medium mb-1">{t("mapping.link_type_label")}</label>
          <select value={linkType} onChange={(e) => setLinkType(e.target.value)} className="w-full border rounded px-3 py-2 text-sm">
            <option value="website">{t("mapping.type_website")}</option><option value="pixiv">{t("mapping.type_pixiv_profile")}</option><option value="x">{t("mapping.type_x_profile")}</option><option value="iwara">{t("mapping.type_iwara_profile")}</option><option value="danbooru">{t("mapping.type_danbooru_artist")}</option><option value="other">{t("mapping.type_other")}</option>
          </select>
        </div>
        <div><label className="block text-sm font-medium mb-1">{t("mapping.source_platform_label")}</label><input value={source} onChange={(e) => setSource(e.target.value)} className="w-full border rounded px-3 py-2 text-sm" placeholder={t("creator_detail.source_placeholder")} /></div>
      </div>
      <div><label className="block text-sm font-medium mb-1">{t("mapping.url_label")}</label><input value={url} onChange={(e) => setUrl(e.target.value)} className="w-full border rounded px-3 py-2 text-sm" placeholder={t("creator_detail.url_placeholder")} /></div>
      <div><label className="block text-sm font-medium mb-1">{t("mapping.confidence_field")} ({confidence.toFixed(1)})</label><input type="range" min="0" max="1" step="0.1" value={confidence} onChange={(e) => setConfidence(parseFloat(e.target.value))} className="w-full" /><div className="flex justify-between text-xs text-gray-400 dark:text-gray-500"><span>{t("mapping.confidence_suggested")}</span><span>{t("mapping.confidence_verified")}</span></div></div>
      <div className="flex justify-end gap-3 pt-2">
        <button onClick={onClose} className="px-4 py-2 text-sm border rounded hover:bg-gray-50 dark:hover:bg-slate-700 dark:bg-slate-800/50">{t("mapping.cancel")}</button>
        <button onClick={() => create.mutate()} disabled={!url || create.isPending} className="px-4 py-2 text-sm bg-slate-900 dark:bg-slate-700 text-white rounded hover:bg-slate-800 dark:hover:bg-slate-600 disabled:opacity-50">{create.isPending ? t("mapping.adding") : t("mapping.add_link_btn")}</button>
      </div>
      {create.error && <p className="text-red-600 text-sm">{(create.error as Error).message}</p>}
    </div>
  );
}

export default function MappingPage() {
  const t = useT();
  const params = useParams(); const router = useRouter(); const qc = useQueryClient();
  const id = params.id as string;

  const creator = useQuery({ queryKey: queryKeys.creators.detail(id), queryFn: () => api.getCreator(id) });
  const links = useQuery({ queryKey: queryKeys.creators.links(id), queryFn: () => api.listCreatorLinks(id) });
  const [showAdd, setShowAdd] = useState(false);
  const [dialog, setDialog] = useState<{ action: "verify" | "unverify"; linkId: string } | null>(null);

  const verifyLink = useMutation({
    mutationFn: async (linkId: string) => {
      await api.updateCreatorLink(id, linkId, { is_verified: true, confidence: 1.0 });
      // If it's a downloadable source, auto-create subscription source
      const link = links.data?.find((l: CreatorLinkType) => l.id === linkId);
      if (link && ["pixiv", "iwara"].includes(link.link_type)) {
        const subs = await api.listSubscriptions();
        let sub = subs.find((s) => s.creator_id === id);
        if (!sub) {
          sub = await api.createSubscription({ creator_id: id, name: undefined });
        }
        try {
          await api.createSubscriptionSource(sub.id, { source: link.link_type, source_url: link.url, is_enabled: true });
        } catch {
          // Source may already exist — ignore
        }
      }
    },
    onSuccess: () => { links.refetch(); qc.invalidateQueries({ queryKey: queryKeys.subscriptions.all }); setDialog(null); },
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
      <PageHeader title={t("mapping.title").replace("{name}", creator.data?.display_name || creator.data?.name || "Creator")} description={t("mapping.desc")}>
        <div className="flex gap-2">
          <button onClick={() => router.push(`/admin/creators/${id}`)} className="px-3 py-2 text-sm border rounded hover:bg-gray-50 dark:hover:bg-slate-700 dark:bg-slate-800/50">{t("mapping.back_to_creator")}</button>
          <button onClick={() => setShowAdd(true)} className="px-3 py-2 text-sm bg-slate-900 dark:bg-slate-700 text-white rounded hover:bg-slate-800 dark:hover:bg-slate-600">{t("mapping.add_link")}</button>
        </div>
      </PageHeader>

      <div className="bg-blue-50 dark:bg-blue-900/30 border border-blue-200 dark:border-blue-800 rounded-lg p-4 text-sm text-blue-800 dark:text-blue-300 mb-6">
        {t("mapping.info_banner")}
      </div>

      <section className="mb-8">
        <h2 className="font-semibold mb-3">{t("mapping.verified_links").replace("{count}", String(verified.length))}</h2>
        <div className="space-y-2">
          {verified.map((l: CreatorLinkType) => (
            <div key={l.id} className="bg-white dark:bg-slate-800 rounded-lg shadow p-3 flex items-center justify-between text-sm">
              <div className="flex items-center gap-3">
                <SourceBadge source={l.link_type} />
                <a href={l.url} target="_blank" rel="noopener noreferrer" className="text-blue-600 hover:underline truncate max-w-sm">{l.url}</a>
                {l.source && <SourceBadge source={l.source} />}
              </div>
              <div className="flex items-center gap-2">
                <span className="text-xs text-green-600">{t("mapping.verified")}</span>
                <button onClick={() => setDialog({ action: "unverify", linkId: l.id })} className="text-xs text-red-500 hover:underline">{t("mapping.unverify")}</button>
              </div>
            </div>
          ))}
          {!verified.length && <p className="text-sm text-gray-400 dark:text-gray-500">{t("mapping.no_verified")}</p>}
        </div>
      </section>

      <section className="mb-8">
        <h2 className="font-semibold mb-3">{t("mapping.suggested_links").replace("{count}", String(suggested.length))}</h2>
        <div className="space-y-2">
          {suggested.map((l: CreatorLinkType) => (
            <div key={l.id} className="bg-white dark:bg-slate-800 rounded-lg shadow p-3 flex items-center justify-between text-sm">
              <div className="flex items-center gap-3">
                <span className="text-xs bg-yellow-100 px-2 py-0.5 rounded">{l.link_type}</span>
                <a href={l.url} target="_blank" rel="noopener noreferrer" className="text-blue-600 hover:underline truncate max-w-sm">{l.url}</a>
                {l.source && <SourceBadge source={l.source} />}
                <span className={`text-xs ${l.confidence >= 0.7 ? "text-green-600" : l.confidence >= 0.4 ? "text-yellow-600" : "text-red-600"}`}>{t("mapping.confidence_label")} {l.confidence.toFixed(1)}</span>
              </div>
              <button onClick={() => setDialog({ action: "verify", linkId: l.id })} className="text-xs px-3 py-1 bg-green-100 dark:bg-green-900/30 text-green-700 dark:text-green-400 rounded hover:bg-green-200">{t("mapping.approve")}</button>
            </div>
          ))}
          {!suggested.length && <p className="text-sm text-gray-400 dark:text-gray-500">{t("mapping.no_suggested")}</p>}
        </div>
      </section>

      <section>
        <h2 className="font-semibold mb-3">{t("mapping.danbooru_section")}</h2>
        <div className="bg-yellow-50 dark:bg-yellow-900/30 border border-yellow-200 dark:border-yellow-800 rounded-lg p-4 text-sm">
          <p className="font-medium text-yellow-800 dark:text-yellow-300 mb-2">{t("mapping.danbooru_info")}</p>
          <p className="text-yellow-700 mb-3">{t("mapping.danbooru_desc")}</p>
          <button onClick={() => router.push("/admin/reference/danbooru")} className="text-xs px-3 py-1.5 bg-yellow-600 text-white rounded hover:bg-yellow-700">{t("mapping.open_danbooru")}</button>
        </div>
      </section>

      <Modal open={showAdd} onClose={() => setShowAdd(false)} title={t("mapping.add_link_title")}><AddLinkForm creatorId={id} onClose={() => setShowAdd(false)} /></Modal>
      {dialog?.action === "verify" && <ConfirmDialog open title={t("mapping.approve_title")} message={t("mapping.approve_msg")} onConfirm={() => verifyLink.mutate(dialog.linkId)} onCancel={() => setDialog(null)} isPending={verifyLink.isPending} error={(verifyLink.error as Error)?.message} />}
      {dialog?.action === "unverify" && <ConfirmDialog open title={t("mapping.unverify_title")} message={t("mapping.unverify_msg")} onConfirm={() => unverifyLink.mutate(dialog.linkId)} onCancel={() => setDialog(null)} isPending={unverifyLink.isPending} error={(unverifyLink.error as Error)?.message} />}
    </main>
  );
}

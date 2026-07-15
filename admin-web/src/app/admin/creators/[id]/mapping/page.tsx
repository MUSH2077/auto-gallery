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
          <select value={linkType} onChange={(e) => setLinkType(e.target.value)} className="select w-full">
            <option value="website">{t("mapping.type_website")}</option><option value="pixiv">{t("mapping.type_pixiv_profile")}</option><option value="x">{t("mapping.type_x_profile")}</option><option value="iwara">{t("mapping.type_iwara_profile")}</option><option value="danbooru">{t("mapping.type_danbooru_artist")}</option><option value="other">{t("mapping.type_other")}</option>
          </select>
        </div>
        <div><label className="block text-sm font-medium mb-1">{t("mapping.source_platform_label")}</label><input value={source} onChange={(e) => setSource(e.target.value)} className="input w-full" placeholder={t("creator_detail.source_placeholder")} /></div>
      </div>
      <div><label className="block text-sm font-medium mb-1">{t("mapping.url_label")}</label><input value={url} onChange={(e) => setUrl(e.target.value)} className="input w-full" placeholder={t("creator_detail.url_placeholder")} /></div>
      <div><label className="block text-sm font-medium mb-1">{t("mapping.confidence_field")} ({confidence.toFixed(1)})</label><input type="range" min="0" max="1" step="0.1" value={confidence} onChange={(e) => setConfidence(parseFloat(e.target.value))} className="w-full" /><div className="flex justify-between text-xs text-muted"><span>{t("mapping.confidence_suggested")}</span><span>{t("mapping.confidence_verified")}</span></div></div>
      <div className="flex justify-end gap-3 pt-2">
        <button onClick={onClose} className="btn-ghost">{t("mapping.cancel")}</button>
        <button onClick={() => create.mutate()} disabled={!url || create.isPending} className="btn-primary">{create.isPending ? t("mapping.adding") : t("mapping.add_link_btn")}</button>
      </div>
      {create.error && <p className="text-danger text-sm">{(create.error as Error).message}</p>}
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

  if (creator.isLoading) return <main className="max-w-4xl mx-auto p-6"><div className="animate-pulse"><div className="mb-4 h-8 w-1/4 rounded-md bg-subtle dark:bg-subtle" /><div className="h-32 rounded-md bg-subtle dark:bg-subtle" /></div></main>;

  return (
    <main className="page-transition max-w-4xl mx-auto p-6">
      <PageHeader title={t("mapping.title").replace("{name}", creator.data?.display_name || creator.data?.name || "Creator")} description={t("mapping.desc")}>
        <div className="flex gap-2">
          <button onClick={() => router.push(`/admin/creators/${id}`)} className="btn-ghost">{t("mapping.back_to_creator")}</button>
          <button onClick={() => setShowAdd(true)} className="btn-primary">{t("mapping.add_link")}</button>
        </div>
      </PageHeader>

      <div className="mb-6 rounded-md border border-accent-subtle bg-accent-subtle p-4 text-sm text-accent dark:border-accent/30 dark:bg-accent/15 dark:text-accent">
        {t("mapping.info_banner")}
      </div>

      <section className="mb-8">
        <h2 className="font-semibold mb-3">{t("mapping.verified_links").replace("{count}", String(verified.length))}</h2>
        <div className="space-y-2">
          {verified.map((l: CreatorLinkType) => (
            <div key={l.id} className="card flex items-center justify-between p-3 text-sm">
              <div className="flex items-center gap-3">
                <SourceBadge source={l.link_type} />
                <a href={l.url} target="_blank" rel="noopener noreferrer" className="text-accent hover:underline truncate max-w-sm">{l.url}</a>
                {l.source && <SourceBadge source={l.source} />}
              </div>
              <div className="flex items-center gap-2">
                <span className="text-xs text-success">{t("mapping.verified")}</span>
                <button onClick={() => setDialog({ action: "unverify", linkId: l.id })} className="text-xs text-danger hover:underline">{t("mapping.unverify")}</button>
              </div>
            </div>
          ))}
          {!verified.length && <p className="text-sm text-muted">{t("mapping.no_verified")}</p>}
        </div>
      </section>

      <section className="mb-8">
        <h2 className="font-semibold mb-3">{t("mapping.suggested_links").replace("{count}", String(suggested.length))}</h2>
        <div className="space-y-2">
          {suggested.map((l: CreatorLinkType) => (
            <div key={l.id} className="card flex items-center justify-between p-3 text-sm">
              <div className="flex items-center gap-3">
                <span className="badge border-warning-subtle bg-warning-subtle text-warning dark:border-warning/30 dark:bg-warning/15 dark:text-warning">{l.link_type}</span>
                <a href={l.url} target="_blank" rel="noopener noreferrer" className="text-accent hover:underline truncate max-w-sm">{l.url}</a>
                {l.source && <SourceBadge source={l.source} />}
                <span className={`text-xs ${l.confidence >= 0.7 ? "text-success" : l.confidence >= 0.4 ? "text-warning" : "text-danger"}`}>{t("mapping.confidence_label")} {l.confidence.toFixed(1)}</span>
              </div>
              <button onClick={() => setDialog({ action: "verify", linkId: l.id })} className="btn-ghost px-3 py-1 text-xs text-success dark:text-success">{t("mapping.approve")}</button>
            </div>
          ))}
          {!suggested.length && <p className="text-sm text-muted">{t("mapping.no_suggested")}</p>}
        </div>
      </section>

      <section>
        <h2 className="font-semibold mb-3">{t("mapping.danbooru_section")}</h2>
        <div className="rounded-md border border-warning-subtle bg-warning-subtle p-4 text-sm dark:border-warning/30 dark:bg-warning/15">
          <p className="font-medium text-warning mb-2">{t("mapping.danbooru_info")}</p>
          <p className="text-warning mb-3">{t("mapping.danbooru_desc")}</p>
          <button onClick={() => router.push("/admin/reference/danbooru")} className="btn-primary px-3 py-1.5 text-xs">{t("mapping.open_danbooru")}</button>
        </div>
      </section>

      <Modal open={showAdd} onClose={() => setShowAdd(false)} title={t("mapping.add_link_title")}><AddLinkForm creatorId={id} onClose={() => setShowAdd(false)} /></Modal>
      {dialog?.action === "verify" && <ConfirmDialog open title={t("mapping.approve_title")} message={t("mapping.approve_msg")} onConfirm={() => verifyLink.mutate(dialog.linkId)} onCancel={() => setDialog(null)} isPending={verifyLink.isPending} error={(verifyLink.error as Error)?.message} />}
      {dialog?.action === "unverify" && <ConfirmDialog open title={t("mapping.unverify_title")} message={t("mapping.unverify_msg")} onConfirm={() => unverifyLink.mutate(dialog.linkId)} onCancel={() => setDialog(null)} isPending={unverifyLink.isPending} error={(unverifyLink.error as Error)?.message} />}
    </main>
  );
}

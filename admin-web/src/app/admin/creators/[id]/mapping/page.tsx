"use client";
import { useEffect, useId, useState } from "react";
import { useParams, useRouter } from "next/navigation";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api, queryKeys, CreatorLink as CreatorLinkType } from "@/lib/api";
import { BusinessConfirm, BusinessModal, BusinessPage } from "@/features/gallery-ui/BusinessPage";
import { BusinessPermissionGuard as PermissionGuard } from "@/features/gallery-ui/BusinessPage";
import SourceBadge from "@/components/SourceBadge";
import { ToolbarSelect } from "@/features/gallery-ui/Controls";
import StatusBadge from "@/components/StatusBadge";
import { useT } from "@/lib/i18n";
import { adminRoutes } from "@/lib/adminRoutes";
import { usePermissions } from "@/lib/usePermissions";

type SetupFailure = { link: CreatorLinkType; subscriptionId?: string; stage: "subscription" | "source"; message: string };

function AddLinkForm({ creatorId, onClose }: { creatorId: string; onClose: () => void }) {
  const t = useT();
  const formId = useId();
  const [url, setUrl] = useState(""); const [linkType, setLinkType] = useState("website"); const [source, setSource] = useState(""); const [confidence, setConfidence] = useState(1.0);
  const qc = useQueryClient();
  const create = useMutation({
    mutationFn: () => api.createCreatorLink(creatorId, { url, link_type: linkType, source: source || undefined, confidence }),
    onSuccess: () => { qc.invalidateQueries({ queryKey: queryKeys.creators.links(creatorId) }); onClose(); },
  });
  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 gap-4">
        <div><span className="block text-sm font-medium mb-1">{t("mapping.link_type_label")}</span>
          <ToolbarSelect label={t("mapping.link_type_label")} value={linkType} onValueChange={setLinkType} options={[
            { value: "website", label: t("mapping.type_website") },
            { value: "pixiv", label: t("mapping.type_pixiv_profile") },
            { value: "x", label: t("mapping.type_x_profile") },
            { value: "iwara", label: t("mapping.type_iwara_profile") },
            { value: "danbooru", label: t("mapping.type_danbooru_artist") },
            { value: "other", label: t("mapping.type_other") },
          ]} />
        </div>
        <div><label htmlFor={`${formId}-source`} className="block text-sm font-medium mb-1">{t("mapping.source_platform_label")}</label><input id={`${formId}-source`} value={source} onChange={(e) => setSource(e.target.value)} className="input w-full" placeholder={t("creator_detail.source_placeholder")} /></div>
      </div>
      <div><label htmlFor={`${formId}-url`} className="block text-sm font-medium mb-1">{t("mapping.url_label")}</label><input id={`${formId}-url`} value={url} onChange={(e) => setUrl(e.target.value)} className="input w-full" placeholder={t("creator_detail.url_placeholder")} /></div>
      <div><label htmlFor={`${formId}-confidence`} className="block text-sm font-medium mb-1">{t("mapping.confidence_field")} ({confidence.toFixed(1)})</label><input id={`${formId}-confidence`} type="range" min="0" max="1" step="0.1" value={confidence} onChange={(e) => setConfidence(parseFloat(e.target.value))} className="w-full" /><div className="flex justify-between text-xs text-muted"><span>{t("mapping.confidence_suggested")}</span><span>{t("mapping.confidence_verified")}</span></div></div>
      <div className="flex justify-end gap-3 pt-2">
        <button onClick={onClose} className="btn-ghost">{t("mapping.cancel")}</button>
        <button onClick={() => create.mutate()} disabled={!url || create.isPending} className="btn-primary">{create.isPending ? t("mapping.adding") : t("mapping.add_link_btn")}</button>
      </div>
      {create.error && <p className="text-danger text-sm">{(create.error as Error).message}</p>}
    </div>
  );
}

function MappingContent() {
  const t = useT();
  const params = useParams(); const router = useRouter(); const qc = useQueryClient();
  const id = params.id as string;
  const { user, has } = usePermissions();
  const canCurate = has("curation");
  const canSetupRepositories = has("subscriptions");

  const creator = useQuery({ queryKey: queryKeys.creators.detail(id), queryFn: () => api.getCreator(id) });
  const links = useQuery({ queryKey: queryKeys.creators.links(id), queryFn: () => api.listCreatorLinks(id) });
  const [showAdd, setShowAdd] = useState(false);
  const [dialog, setDialog] = useState<{ action: "verify" | "unverify"; linkId: string } | null>(null);
  const [setupFailures, setSetupFailures] = useState<Record<string, SetupFailure>>({});
  const recoveryKey = user?.id ? `auto-gallery-setup-recovery:${user.id}:${id}` : null;

  useEffect(() => {
    if (!recoveryKey) return;
    try {
      const parsed = JSON.parse(localStorage.getItem(recoveryKey) || "{}") as Record<string, SetupFailure>;
      const valid = Object.fromEntries(Object.entries(parsed).filter(([, entry]) => entry
        && typeof entry === "object"
        && typeof entry.link?.id === "string"
        && typeof entry.link?.url === "string"
        && (entry.stage === "subscription" || entry.stage === "source")
        && typeof entry.message === "string"));
      setSetupFailures(valid);
    } catch {
      setSetupFailures({});
    }
  }, [recoveryKey]);

  const updateSetupFailure = (linkId: string, failure: SetupFailure | null) => {
    setSetupFailures((current) => {
      const next = { ...current };
      if (failure) next[linkId] = failure;
      else delete next[linkId];
      if (recoveryKey) {
        try {
          if (Object.keys(next).length) localStorage.setItem(recoveryKey, JSON.stringify(next));
          else localStorage.removeItem(recoveryKey);
        } catch {}
      }
      return next;
    });
  };

  const setupRepository = async (link: CreatorLinkType, subscriptionId?: string) => {
    let subId = subscriptionId;
    if (!subId) {
      try {
        const subs = await api.listSubscriptions();
        let sub = subs.find((item) => item.creator_id === id);
        if (!sub) sub = await api.createSubscription({ creator_id: id, name: undefined });
        subId = sub.id;
      } catch (error) {
        return { link, stage: "subscription" as const, message: (error as Error).message };
      }
    }
    try {
      await api.createSubscriptionSource(subId, { source: link.link_type, source_url: link.url, is_enabled: true });
      return null;
    } catch (error) {
      return { link, subscriptionId: subId, stage: "source" as const, message: (error as Error).message };
    }
  };

  const verifyLink = useMutation({
    mutationFn: async (linkId: string) => {
      const link = links.data?.find((l: CreatorLinkType) => l.id === linkId);
      if (link && ["pixiv", "iwara"].includes(link.link_type) && !canSetupRepositories) {
        throw new Error(t("mapping.repository_setup_requires_subscriptions"));
      }
      await api.updateCreatorLink(id, linkId, { is_verified: true, confidence: 1.0 });
      await links.refetch();
      if (link && ["pixiv", "iwara"].includes(link.link_type)) {
        return { linkId, failure: await setupRepository(link) };
      }
      return { linkId, failure: null };
    },
    onSuccess: (failure) => {
      void qc.invalidateQueries({ queryKey: queryKeys.creators.links(id) });
      void qc.invalidateQueries({ queryKey: queryKeys.subscriptions.all });
      updateSetupFailure(failure.linkId, failure.failure);
      setDialog(null);
    },
  });

  const retrySetup = useMutation({
    mutationFn: async (linkId: string) => ({ linkId, failure: setupFailures[linkId] ? await setupRepository(setupFailures[linkId].link, setupFailures[linkId].subscriptionId) : null }),
    onSuccess: ({ linkId, failure }) => {
      updateSetupFailure(linkId, failure);
      void qc.invalidateQueries({ queryKey: queryKeys.subscriptions.all });
    },
  });

  const unverifyLink = useMutation({
    mutationFn: (linkId: string) => api.updateCreatorLink(id, linkId, { is_verified: false, confidence: 0.5 }),
    onSuccess: () => { links.refetch(); setDialog(null); },
  });

  const verified = links.data?.filter((l: CreatorLinkType) => l.is_verified) || [];
  const suggested = links.data?.filter((l: CreatorLinkType) => !l.is_verified) || [];

  if (creator.isLoading) return <BusinessPage><div className="animate-pulse"><div className="mb-4 h-8 w-1/4 rounded-md bg-subtle dark:bg-subtle" /><div className="h-32 rounded-md bg-subtle dark:bg-subtle" /></div></BusinessPage>;

  return (
      <BusinessPage
        title={t("mapping.title").replace("{name}", creator.data?.display_name || creator.data?.name || "Creator")}
        description={t("mapping.desc")}
        className="gallery-creators-page gallery-creator-mapping"
        actions={(
          <>
            <button onClick={() => router.push(`/admin/creators/${id}`)} className="btn-ghost">{t("mapping.back_to_creator")}</button>
            {canCurate && <button onClick={() => setShowAdd(true)} className="btn-primary">{t("mapping.add_link")}</button>}
          </>
        )}
      >

      <div className="mb-6 rounded-md border border-accent-subtle bg-accent-subtle p-4 text-sm text-accent dark:border-accent/30 dark:bg-accent/15 dark:text-accent">
        {t("mapping.info_banner")}
      </div>

      {Object.entries(setupFailures).map(([linkId, failure]) => (
        <div key={linkId} role="alert" className="mb-6 rounded-md border border-danger/30 bg-danger-subtle p-4 text-sm text-danger">
          <p>{t("mapping.partial_setup", { url: failure.link.url, stage: t(`mapping.stage_${failure.stage}`) })}: {failure.message}</p>
          {canCurate && canSetupRepositories ? (
            <button className="btn-ghost mt-2" disabled={retrySetup.isPending} onClick={() => retrySetup.mutate(linkId)}>
              {retrySetup.isPending && retrySetup.variables === linkId ? t("common.processing") : t("mapping.retry_setup")}
            </button>
          ) : canCurate ? (
            <p className="mt-2 text-xs">{t("mapping.repository_setup_requires_subscriptions")}</p>
          ) : null}
        </div>
      ))}

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
                {canCurate && <button onClick={() => setDialog({ action: "unverify", linkId: l.id })} className="text-xs text-danger hover:underline">{t("mapping.unverify")}</button>}
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
                <span className={`text-sm ${l.confidence >= 0.7 ? "text-success" : l.confidence >= 0.4 ? "text-warning" : "text-danger"}`}>{t("mapping.confidence_label")} {l.confidence.toFixed(1)}</span>
              </div>
              {canCurate && (
                <div className="text-right">
                  <button
                    onClick={() => setDialog({ action: "verify", linkId: l.id })}
                    disabled={["pixiv", "iwara"].includes(l.link_type) && !canSetupRepositories}
                    className="btn-ghost px-3 py-1 text-xs text-success disabled:cursor-not-allowed disabled:opacity-50 dark:text-success"
                  >
                    {t("mapping.approve")}
                  </button>
                  {["pixiv", "iwara"].includes(l.link_type) && !canSetupRepositories && (
                    <p className="mt-1 max-w-56 text-xs text-muted">{t("mapping.repository_setup_requires_subscriptions")}</p>
                  )}
                </div>
              )}
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
          <button onClick={() => router.push(adminRoutes.danbooru)} className="btn-primary px-3 py-1.5 text-xs">{t("mapping.open_danbooru")}</button>
        </div>
      </section>

      <BusinessModal open={showAdd} onClose={() => setShowAdd(false)} title={t("mapping.add_link_title")}><AddLinkForm creatorId={id} onClose={() => setShowAdd(false)} /></BusinessModal>
      {dialog?.action === "verify" && <BusinessConfirm open title={t("mapping.approve_title")} message={t("mapping.approve_msg")} onConfirm={() => verifyLink.mutate(dialog.linkId)} onCancel={() => setDialog(null)} isPending={verifyLink.isPending} error={(verifyLink.error as Error)?.message} />}
      {dialog?.action === "unverify" && <BusinessConfirm open title={t("mapping.unverify_title")} message={t("mapping.unverify_msg")} onConfirm={() => unverifyLink.mutate(dialog.linkId)} onCancel={() => setDialog(null)} isPending={unverifyLink.isPending} error={(unverifyLink.error as Error)?.message} />}
    </BusinessPage>
  );
}

export default function MappingPage() {
  return <PermissionGuard module="library"><MappingContent /></PermissionGuard>;
}

"use client";
import { useState } from "react";
import { useParams, useRouter } from "next/navigation";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api, queryKeys, CreatorLink as CreatorLinkType, SourceCreator as SourceCreatorType } from "@/lib/api";
import { PageHeader, StatusBadge, SourceBadge, EmptyState, Modal, ConfirmDialog } from "@/components";
import { useT } from "@/lib/i18n";

function AddLinkForm({ creatorId, onClose }: { creatorId: string; onClose: () => void }) {
  const t = useT();
  const [url, setUrl] = useState(""); const [linkType, setLinkType] = useState("website"); const [source, setSource] = useState("");
  const qc = useQueryClient();
  const create = useMutation({
    mutationFn: () => api.createCreatorLink(creatorId, { url, link_type: linkType, source: source || undefined }),
    onSuccess: () => { qc.invalidateQueries({ queryKey: queryKeys.creators.links(creatorId) }); onClose(); },
  });
  return (
    <div className="space-y-4">
      <div><label className="block text-sm font-medium mb-1">{t("creator_detail.url_label")}</label><input value={url} onChange={(e) => setUrl(e.target.value)} className="w-full border rounded px-3 py-2 text-sm" placeholder={t("creator_detail.url_placeholder")} /></div>
      <div className="grid grid-cols-2 gap-4">
        <div><label className="block text-sm font-medium mb-1">{t("creator_detail.type_label")}</label>
          <select value={linkType} onChange={(e) => setLinkType(e.target.value)} className="w-full border rounded px-3 py-2 text-sm">
            <option value="website">{t("creator_detail.type_website")}</option><option value="pixiv">{t("creator_detail.type_pixiv")}</option><option value="x">{t("creator_detail.type_x")}</option><option value="iwara">{t("creator_detail.type_iwara")}</option><option value="danbooru">{t("creator_detail.type_danbooru")}</option><option value="other">{t("creator_detail.type_other")}</option>
          </select>
        </div>
        <div><label className="block text-sm font-medium mb-1">{t("creator_detail.source_label")}</label><input value={source} onChange={(e) => setSource(e.target.value)} className="w-full border rounded px-3 py-2 text-sm" placeholder={t("creator_detail.source_placeholder")} /></div>
      </div>
      <div className="flex justify-end gap-3 pt-2">
        <button onClick={onClose} className="px-4 py-2 text-sm border rounded hover:bg-gray-50 dark:hover:bg-slate-700 dark:bg-slate-800/50">{t("creator_detail.cancel")}</button>
        <button onClick={() => create.mutate()} disabled={!url || create.isPending} className="px-4 py-2 text-sm bg-slate-900 dark:bg-slate-700 text-white rounded hover:bg-slate-800 dark:hover:bg-slate-600 disabled:opacity-50">{create.isPending ? t("creator_detail.adding") : t("creator_detail.add_link_btn")}</button>
      </div>
      {create.error && <p className="text-red-600 text-sm">{(create.error as Error).message}</p>}
    </div>
  );
}

function SubscriptionPanel({ creatorId }: { creatorId: string }) {
  const t = useT();
  const router = useRouter();
  const qc = useQueryClient();
  const subs = useQuery({ queryKey: queryKeys.subscriptions.all, queryFn: () => api.listSubscriptions() });
  const createSub = useMutation({
    mutationFn: () => api.createSubscription({ creator_id: creatorId, name: undefined }),
    onSuccess: () => { qc.invalidateQueries({ queryKey: queryKeys.subscriptions.all }); },
  });

  const sub = subs.data?.find((s) => s.creator_id === creatorId);
  const sources = useQuery({
    queryKey: queryKeys.subscriptions.sources(sub?.id || ""),
    queryFn: () => api.listSubscriptionSources(sub!.id),
    enabled: !!sub,
  });

  if (subs.isLoading) return <div className="animate-pulse"><div className="h-8 bg-gray-200 rounded" /></div>;
  if (!sub) {
    return (
      <div className="text-xs text-gray-500 dark:text-gray-400">
        <p className="mb-2">{t("creator_detail.no_subscription")}</p>
        <button onClick={() => createSub.mutate()} disabled={createSub.isPending}
          className="px-3 py-1 bg-slate-900 dark:bg-slate-700 text-white rounded text-xs hover:bg-slate-800 dark:hover:bg-slate-600">
          {createSub.isPending ? t("creator_detail.creating_sub") : t("creator_detail.create_subscription")}
        </button>
      </div>
    );
  }

  return (
    <div className="text-xs space-y-2">
      <div className="flex justify-between">
        <span className="text-gray-500 dark:text-gray-400">{t("creator_detail.sub_status")}</span>
        <span className={sub.sync_enabled ? "text-green-600" : "text-gray-400"}>
          {sub.sync_enabled ? t("creator_detail.auto_sync_on") : t("creator_detail.manual_only")} · {sub.sync_interval_hours}h
        </span>
      </div>
      {sub.last_synced_at && (
        <div className="flex justify-between">
          <span className="text-gray-500 dark:text-gray-400">{t("creator_detail.last_synced")}</span>
          <span>{new Date(sub.last_synced_at).toLocaleString()}</span>
        </div>
      )}
      {sources.data && sources.data.length > 0 && (
        <div className="mt-2 space-y-1">
          <p className="text-gray-500 dark:text-gray-400 font-medium">{t("creator_detail.sources_count").replace("{count}", String(sources.data.length))}</p>
          {sources.data.map((ss: any) => (
            <div key={ss.id} className="flex items-center justify-between border-t pt-1">
              <div className="flex items-center gap-2">
                <SourceBadge source={ss.source} />
                <span className="font-mono text-gray-400 dark:text-gray-500">{ss.source_creator_id || "—"}</span>
                <span className={ss.is_enabled ? "text-green-500" : "text-gray-400"}>
                  {ss.is_enabled ? "●" : "○"}
                </span>
              </div>
              <button onClick={() => router.push(`/admin/subscriptions/${sub.id}`)}
                className="text-blue-600 hover:underline">{t("creator_detail.manage")}</button>
            </div>
          ))}
        </div>
      )}
      {(!sources.data || sources.data.length === 0) && (
        <p className="text-gray-400 dark:text-gray-500">{t("creator_detail.no_sources")}</p>
      )}
    </div>
  );
}

function AddSourceForm({ creatorId, onClose }: { creatorId: string; onClose: () => void }) {
  const t = useT();
  const [source, setSource] = useState("pixiv"); const [sourceCreatorId, setSourceCreatorId] = useState(""); const [sourceUrl, setSourceUrl] = useState(""); const [displayName, setDisplayName] = useState("");
  const qc = useQueryClient();
  const create = useMutation({
    mutationFn: () => api.createSourceCreator(creatorId, { source, source_creator_id: sourceCreatorId, source_url: sourceUrl || undefined, display_name: displayName || undefined }),
    onSuccess: () => { qc.invalidateQueries({ queryKey: queryKeys.creators.detail(creatorId) }); onClose(); },
  });
  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 gap-4">
        <div><label className="block text-sm font-medium mb-1">{t("creator_detail.source_field")}</label>
          <select value={source} onChange={(e) => setSource(e.target.value)} className="w-full border rounded px-3 py-2 text-sm">
            <option value="pixiv">{t("creator_detail.source_pixiv")}</option><option value="x">{t("creator_detail.source_x")}</option><option value="iwara">{t("creator_detail.source_iwara")}</option><option value="local">{t("creator_detail.source_local")}</option><option value="manual">{t("creator_detail.source_manual")}</option>
          </select>
        </div>
        <div><label className="block text-sm font-medium mb-1">{t("creator_detail.source_creator_id")}</label><input value={sourceCreatorId} onChange={(e) => setSourceCreatorId(e.target.value)} className="w-full border rounded px-3 py-2 text-sm" placeholder={t("creator_detail.source_id_placeholder")} /></div>
      </div>
      <div><label className="block text-sm font-medium mb-1">{t("creator_detail.source_url")}</label><input value={sourceUrl} onChange={(e) => setSourceUrl(e.target.value)} className="w-full border rounded px-3 py-2 text-sm" placeholder={t("creator_detail.url_placeholder")} /></div>
      <div><label className="block text-sm font-medium mb-1">{t("creator_detail.source_display_name")}</label><input value={displayName} onChange={(e) => setDisplayName(e.target.value)} className="w-full border rounded px-3 py-2 text-sm" /></div>
      <div className="flex justify-end gap-3 pt-2">
        <button onClick={onClose} className="px-4 py-2 text-sm border rounded hover:bg-gray-50 dark:hover:bg-slate-700 dark:bg-slate-800/50">{t("creator_detail.cancel")}</button>
        <button onClick={() => create.mutate()} disabled={!sourceCreatorId || create.isPending} className="px-4 py-2 text-sm bg-slate-900 dark:bg-slate-700 text-white rounded hover:bg-slate-800 dark:hover:bg-slate-600 disabled:opacity-50">{create.isPending ? t("creator_detail.adding") : t("creator_detail.add_source_btn")}</button>
      </div>
      {create.error && <p className="text-red-600 text-sm">{(create.error as Error).message}</p>}
    </div>
  );
}

function parseDanbooruAliases(description: string | undefined | null): string[] {
  if (!description) return [];
  const match = description.match(/^Danbooru aliases:\s*(.+)$/m);
  if (!match) return [];
  return match[1].split(",").map((s) => s.trim()).filter(Boolean);
}

function DanbooruAliasesPanel({
  creatorId, danbooru_artist_id, description, currentDisplayName, onDisplayNameUpdated,
}: {
  creatorId: string;
  danbooru_artist_id: number;
  description?: string | null;
  currentDisplayName: string;
  onDisplayNameUpdated: () => void;
}) {
  const t = useT();
  const router = useRouter();
  const qc = useQueryClient();
  const aliases = parseDanbooruAliases(description);

  const updateDisplayName = useMutation({
    mutationFn: (name: string) => api.updateCreator(creatorId, { display_name: name }),
    onSuccess: () => onDisplayNameUpdated(),
  });

  return (
    <div className="bg-white dark:bg-slate-800 rounded-lg shadow p-4 h-fit">
      <h3 className="font-medium mb-2">{t("creator_detail.danbooru_ref")}</h3>
      <div className="text-xs space-y-1">
        <div className="flex justify-between">
          <span className="text-gray-500 dark:text-gray-400">{t("creator_detail.artist_id")}</span>
          <a href={`https://danbooru.donmai.us/artists/${danbooru_artist_id}`} target="_blank" rel="noopener noreferrer"
            className="text-blue-600 hover:underline font-mono">#{danbooru_artist_id}</a>
        </div>
      </div>
      {aliases.length > 0 && (
        <div className="mt-3">
          <p className="text-xs text-gray-500 dark:text-gray-400 mb-2">{t("creator_detail.danbooru_aliases_hint")}</p>
          <div className="flex flex-wrap gap-1.5">
            {aliases.map((alias) => (
              <button
                key={alias}
                onClick={() => updateDisplayName.mutate(alias)}
                disabled={updateDisplayName.isPending}
                title={t("creator_detail.set_display_name_as").replace("{name}", alias)}
                className={`px-2 py-0.5 rounded text-xs border transition-colors ${
                  alias === currentDisplayName
                    ? "bg-blue-600 text-white border-blue-600"
                    : "bg-gray-50 dark:bg-slate-700 text-gray-700 dark:text-gray-200 border-gray-200 dark:border-slate-600 hover:bg-blue-50 dark:hover:bg-blue-900/30 hover:border-blue-400 hover:text-blue-700"
                } disabled:opacity-50`}
              >
                {alias}
              </button>
            ))}
          </div>
          {updateDisplayName.error && (
            <p className="text-red-600 text-xs mt-1">{(updateDisplayName.error as Error).message}</p>
          )}
        </div>
      )}
      <button onClick={() => router.push(`/admin/creators/${creatorId}/mapping`)}
        className="mt-3 text-xs text-blue-600 hover:underline w-full text-center block">
        {t("creator_detail.view_mapping")}
      </button>
    </div>
  );
}

export default function CreatorDetailPage() {
  const t = useT();
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

  const toggleFavorite = useMutation({
    mutationFn: (creatorId: string) => api.toggleCreatorFavorite(creatorId),
    onSuccess: () => qc.invalidateQueries({ queryKey: queryKeys.creators.detail(id as string) }),
  });

  const verifyLink = useMutation({
    mutationFn: (linkId: string) => api.updateCreatorLink(id, linkId, { is_verified: true, confidence: 1.0 }),
    onSuccess: () => links.refetch(),
  });

  if (creator.isLoading) return <main className="max-w-4xl mx-auto p-6"><div className="animate-pulse space-y-4"><div className="h-8 bg-gray-200 rounded w-1/4" /><div className="h-4 bg-gray-200 rounded w-1/2" /><div className="h-32 bg-gray-200 rounded" /></div></main>;
  if (creator.error) return <main className="max-w-4xl mx-auto p-6"><div className="p-4 bg-red-50 border border-red-200 rounded-lg text-red-700 dark:text-red-400">{(creator.error as Error).message}</div></main>;
  if (!creator.data) return null;

  const c = creator.data;

  return (
    <main className="max-w-4xl mx-auto p-6">
      <PageHeader title={c.display_name || c.name}>
        <div className="flex gap-2">
          <button onClick={() => toggleFavorite.mutate(id as string)}
            className={`text-xl px-2 ${creator.data?.is_favorite ? "text-yellow-500" : "text-gray-300 hover:text-yellow-400"}`}
            title={creator.data?.is_favorite ? "Unfavorite" : "Favorite"}>
            {creator.data?.is_favorite ? "★" : "☆"}
          </button>
          <button onClick={() => router.push(`/admin/creators/${id}/mapping`)} className="px-3 py-2 text-sm border rounded hover:bg-gray-50 dark:hover:bg-slate-700 dark:bg-slate-800/50">{t("creator_detail.manage_mapping")}</button>
          <button onClick={openEdit} className="px-3 py-2 text-sm bg-slate-900 dark:bg-slate-700 text-white rounded hover:bg-slate-800 dark:hover:bg-slate-600">{t("creator_detail.edit")}</button>
        </div>
      </PageHeader>

      <div className="grid grid-cols-1 md:grid-cols-3 gap-6 mb-8">
        <div className="col-span-2 space-y-4">
          <div className="bg-white dark:bg-slate-800 rounded-lg shadow p-4">
            <h3 className="font-medium mb-2">{t("creator_detail.details")}</h3>
            <dl className="text-sm space-y-2">
              <div className="flex gap-2"><dt className="text-gray-500 dark:text-gray-400 w-24">{t("creator_detail.name")}</dt><dd>{c.name}</dd></div>
              {c.display_name && <div className="flex gap-2"><dt className="text-gray-500 dark:text-gray-400 w-24">{t("creator_detail.display")}</dt><dd>{c.display_name}</dd></div>}
              {c.description && <div className="flex gap-2"><dt className="text-gray-500 dark:text-gray-400 w-24">{t("creator_detail.description")}</dt><dd className="whitespace-pre-wrap">{c.description}</dd></div>}
              <div className="flex gap-2"><dt className="text-gray-500 dark:text-gray-400 w-24">{t("creator_detail.status")}</dt><dd><StatusBadge status={c.is_active ? "up" : "down"} /></dd></div>
              <div className="flex gap-2"><dt className="text-gray-500 dark:text-gray-400 w-24">{t("creator_detail.created")}</dt><dd className="text-xs">{new Date(c.created_at).toLocaleString()}</dd></div>
            </dl>
          </div>

          <div className="bg-white dark:bg-slate-800 rounded-lg shadow p-4">
            <div className="flex items-center justify-between mb-3"><h3 className="font-medium">{t("creator_detail.links").replace("{count}", String(links.data?.length || 0))}</h3><button onClick={() => setShowAddLink(true)} className="text-xs px-3 py-1 bg-slate-900 dark:bg-slate-700 text-white rounded hover:bg-slate-800 dark:hover:bg-slate-600">{t("creator_detail.add_link")}</button></div>
            {links.data && links.data.length > 0 ? (
              <div className="space-y-2">
                {links.data.map((l: CreatorLinkType) => (
                  <div key={l.id} className="flex items-center justify-between border-b dark:border-slate-700 pb-2 text-sm">
                    <div className="flex items-center gap-2">
                      <SourceBadge source={l.link_type} />
                      <a href={l.url} target="_blank" rel="noopener noreferrer" className="text-blue-600 hover:underline truncate max-w-xs">{l.url}</a>
                    </div>
                    <div className="flex items-center gap-2">
                      <span className="text-xs text-gray-400 dark:text-gray-500">{t("creator_detail.confidence")} {l.confidence.toFixed(1)}</span>
                      {l.is_verified ? <StatusBadge status="up" /> : <button onClick={() => verifyLink.mutate(l.id)} disabled={verifyLink.isPending} className="text-xs text-blue-600 hover:underline disabled:opacity-50">{verifyLink.isPending ? "..." : t("creator_detail.verify")}</button>}
                    </div>
                  </div>
                ))}
              </div>
            ) : <p className="text-sm text-gray-400 dark:text-gray-500">{t("creator_detail.no_links")}</p>}
            {verifyLink.error && <p className="text-red-600 text-sm mt-2">{(verifyLink.error as Error).message}</p>}
          </div>
        </div>

        <div className="bg-white dark:bg-slate-800 rounded-lg shadow p-4 h-fit">
          <div className="flex items-center justify-between mb-3"><h3 className="font-medium">{t("creator_detail.subscription")}</h3></div>
          <SubscriptionPanel creatorId={id} />
        </div>

        {/* Danbooru Reference */}
        {c.danbooru_artist_id && (
          <DanbooruAliasesPanel
            creatorId={id}
            danbooru_artist_id={c.danbooru_artist_id}
            description={c.description}
            currentDisplayName={c.display_name || c.name}
            onDisplayNameUpdated={() => qc.invalidateQueries({ queryKey: queryKeys.creators.detail(id) })}
          />
        )}
      </div>

      {/* Works by this Creator */}
      <CreatorWorksSection creatorId={id} creatorName={creator.data?.display_name || creator.data?.name || ""} />

      <Modal open={showAddLink} onClose={() => setShowAddLink(false)} title={t("creator_detail.add_link_title")}><AddLinkForm creatorId={id} onClose={() => setShowAddLink(false)} /></Modal>
      <Modal open={showAddSource} onClose={() => setShowAddSource(false)} title={t("creator_detail.add_source_title")}><AddSourceForm creatorId={id} onClose={() => setShowAddSource(false)} /></Modal>
      <Modal open={editing} onClose={() => setEditing(false)} title={t("creator_detail.edit_title")}>
        <div className="space-y-4">
          <div><label className="block text-sm font-medium mb-1">{t("creator_detail.name_field")}</label><input value={editName} onChange={(e) => setEditName(e.target.value)} className="w-full border rounded px-3 py-2 text-sm" /></div>
          <div><label className="block text-sm font-medium mb-1">{t("creator_detail.display_name_field")}</label><input value={editDisplay} onChange={(e) => setEditDisplay(e.target.value)} className="w-full border rounded px-3 py-2 text-sm" /></div>
          <div><label className="block text-sm font-medium mb-1">{t("creator_detail.description_field")}</label><textarea value={editDesc} onChange={(e) => setEditDesc(e.target.value)} className="w-full border rounded px-3 py-2 text-sm" rows={3} /></div>
          <div className="flex justify-end gap-3 pt-2">
            <button onClick={() => setEditing(false)} className="px-4 py-2 text-sm border rounded hover:bg-gray-50 dark:hover:bg-slate-700 dark:bg-slate-800/50">{t("creator_detail.cancel")}</button>
            <button onClick={() => update.mutate()} disabled={update.isPending} className="px-4 py-2 text-sm bg-slate-900 dark:bg-slate-700 text-white rounded hover:bg-slate-800 dark:hover:bg-slate-600">{t("creator_detail.save")}</button>
          </div>
          {update.error && <p className="text-red-600 text-sm">{(update.error as Error).message}</p>}
        </div>
      </Modal>
    </main>
  );
}


function CreatorWorksSection({ creatorId, creatorName }: { creatorId: string; creatorName: string }) {
  const t = useT();
  const router = useRouter();
  const works = useQuery({
    queryKey: ["creator-works", creatorId],
    queryFn: () => api.listWorks(0, 12, { creator_id: creatorId, sort_by: "posted_at", sort_order: "desc" }),
  });

  if (works.isLoading) return null;
  if (!works.data?.length) return null;

  return (
    <section className="mb-8">
      <div className="flex items-center justify-between mb-3">
        <h2 className="text-lg font-semibold dark:text-white">{t("creator_detail.works_section").replace("{count}", String(works.data.length))}</h2>
        <button onClick={() => router.push(`/admin/works?creator=${creatorId}`)}
          className="text-sm text-blue-600 hover:underline">{t("creator_detail.view_all_works")}</button>
      </div>
      <div className="grid grid-cols-3 md:grid-cols-4 lg:grid-cols-6 gap-3">
        {works.data.map((w) => (
          <div key={w.id} className="bg-white dark:bg-slate-800 rounded-lg shadow-sm overflow-hidden cursor-pointer hover:shadow-md transition-shadow" onClick={() => router.push(`/admin/works/${w.id}`)}>
            <div className="aspect-square bg-gray-100 dark:bg-slate-700 flex items-center justify-center text-gray-400 text-xs overflow-hidden relative">
              {w.thumbnail_asset_id ? (
                <img src={api.mediaUrl(w.thumbnail_asset_id, "thumb")} alt={w.title || ""} className="w-full h-full object-cover" loading="lazy" />
              ) : (
                <span>{t("creator_detail.na")}</span>
              )}
              {w.asset_count > 1 && (
                <span className="absolute top-0.5 right-0.5 bg-black/70 text-white text-[10px] px-1 py-0.5 rounded">{w.asset_count}p</span>
              )}
            </div>
            <div className="p-2">
              <div className="text-xs font-medium truncate dark:text-white">{w.title || t("creator_detail.untitled")}</div>
              {w.source && <SourceBadge source={w.source} />}
            </div>
          </div>
        ))}
      </div>
    </section>
  );
}

"use client";
import { useState } from "react";
import { useT } from "@/lib/i18n";
import { useParams, useRouter } from "next/navigation";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api, queryKeys, SubscriptionSource as SS, ProviderInfo } from "@/lib/api";
import { PageHeader, StatusBadge, SourceBadge, Modal, ConfirmDialog, ErrorState, EmptyState } from "@/components";

function AddSourceForm({ subId, onClose }: { subId: string; onClose: () => void }) {
  const [source, setSource] = useState("pixiv"); const [sourceUrl, setSourceUrl] = useState(""); const [sourceCreatorId, setSourceCreatorId] = useState("");
  const t = useT();
  const qc = useQueryClient();

  const urlHint = source === "pixiv" ? "https://www.pixiv.net/users/{id}" : source === "x" ? "https://x.com/{handle}" : source === "iwara" ? "https://www.iwara.tv/profile/{id}" : source === "danbooru" ? "https://danbooru.donmai.us/posts?tags={tag}" : "";

  const handleIdChange = (id: string) => {
    setSourceCreatorId(id);
    if (id && source === "pixiv") setSourceUrl(`https://www.pixiv.net/users/${id}`);
    else if (id && source === "x") setSourceUrl(`https://x.com/${id}`);
    else if (id && source === "iwara") setSourceUrl(`https://www.iwara.tv/profile/${id}`);
    else if (id && source === "danbooru") setSourceUrl(`https://danbooru.donmai.us/posts?tags=${encodeURIComponent(id)}`);
  };

  const handleSourceChange = (s: string) => {
    setSource(s);
    if (sourceCreatorId && s === "pixiv") setSourceUrl(`https://www.pixiv.net/users/${sourceCreatorId}`);
    else if (sourceCreatorId && s === "x") setSourceUrl(`https://x.com/${sourceCreatorId}`);
    else if (sourceCreatorId && s === "iwara") setSourceUrl(`https://www.iwara.tv/profile/${sourceCreatorId}`);
    else if (sourceCreatorId && s === "danbooru") setSourceUrl(`https://danbooru.donmai.us/posts?tags=${encodeURIComponent(sourceCreatorId)}`);
  };

  const create = useMutation({
    mutationFn: () => api.createSubscriptionSource(subId, { source, source_url: sourceUrl || undefined, source_creator_id: sourceCreatorId || undefined, is_enabled: true }),
    onSuccess: () => { qc.invalidateQueries({ queryKey: queryKeys.subscriptions.sources(subId) }); onClose(); },
  });
  const sources = useQuery({ queryKey: queryKeys.sources, queryFn: api.sources });

  return (
    <div className="space-y-4">
      <div>
        <label className="block text-sm font-medium mb-1">{t("subscription_detail.source_field")}</label>
        <select value={source} onChange={(e) => handleSourceChange(e.target.value)} className="w-full border rounded px-3 py-2 text-sm">
          {sources.data?.sources?.filter((s: ProviderInfo) => s.capabilities.can_download || s.capabilities.can_import_local).map((s: ProviderInfo) => <option key={s.source_name} value={s.source_name}>{s.display_name} ({s.source_name})</option>)}
        </select>
        <p className="text-xs text-gray-400 dark:text-gray-500 mt-1">{t("subscription_detail.multi_source_hint")}</p>
      </div>
      <div>
        <label className="block text-sm font-medium mb-1">{t("subscription_detail.source_creator_id")}</label>
        <input value={sourceCreatorId} onChange={(e) => handleIdChange(e.target.value)} className="w-full border rounded px-3 py-2 text-sm" placeholder={t("subscription_detail.source_creator_id_placeholder")} />
        <p className="text-xs text-gray-400 dark:text-gray-500 mt-1">{t("subscription_detail.source_creator_id_hint")}</p>
      </div>
      <div>
        <label className="block text-sm font-medium mb-1">{t("subscription_detail.source_url_field")}</label>
        <input value={sourceUrl} onChange={(e) => setSourceUrl(e.target.value)} className="w-full border rounded px-3 py-2 text-sm font-mono" placeholder={urlHint} />
        <p className="text-xs text-gray-400 dark:text-gray-500 mt-1">{t("subscription_detail.source_url_hint")} {urlHint}</p>
      </div>
      <div className="flex justify-end gap-3 pt-2">
        <button onClick={onClose} className="px-4 py-2 text-sm border rounded hover:bg-gray-50 dark:hover:bg-slate-700 dark:bg-slate-800/50">{t("subscription_detail.cancel")}</button>
        <button onClick={() => create.mutate()} disabled={create.isPending} className="px-4 py-2 text-sm bg-slate-900 dark:bg-slate-700 text-white rounded hover:bg-slate-800 dark:hover:bg-slate-600 disabled:opacity-50">{create.isPending ? t("subscription_detail.adding") : t("subscription_detail.add_source_btn")}</button>
      </div>
      {create.error && <p className="text-red-600 text-sm">{(create.error as Error).message}</p>}
    </div>
  );
}

export default function SubscriptionDetailPage() {
  const params = useParams(); const router = useRouter(); const qc = useQueryClient();
  const t = useT();
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
    mutationFn: (ssId: string) => {
      const ss = sources.data?.find((s: SS) => s.id === ssId);
      if (!ss) throw new Error(t("subscription_detail.source_not_found"));
      return api.createDownloadJob({ subscription_id: id, subscription_source_id: ssId, source: ss.source, source_url: ss.source_url || "" });
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: queryKeys.downloadJobs.all });
      qc.invalidateQueries({ queryKey: queryKeys.subscriptions.sources(id) });
    },
  });

  const getCreatorName = (creatorId: string) => {
    const c = creators.data?.find((c) => c.id === creatorId);
    return c ? (c.display_name || c.name) : creatorId.slice(0, 8);
  };

  if (sub.isLoading) return <main className="max-w-4xl mx-auto p-6"><div className="animate-pulse space-y-4"><div className="h-8 bg-gray-200 rounded w-1/4" /><div className="h-32 bg-gray-200 rounded" /></div></main>;
  if (sub.error) return <main className="max-w-4xl mx-auto p-6"><ErrorState message={(sub.error as Error).message} onRetry={() => sub.refetch()} /></main>;
  if (!sub.data) return null;
  const s = sub.data;

  return (
    <main className="max-w-4xl mx-auto p-6">
      <PageHeader title={s.name || (s.creator_display_name || s.creator_name || getCreatorName(s.creator_id))} description={s.creator_display_name || s.creator_name ? `${t("subscription_detail.creator")} ${s.creator_display_name || s.creator_name}` : undefined}>
        <div className="flex gap-2">
          <button onClick={() => { setEditName(s.name || ""); setEditing(true); }} className="px-3 py-2 text-sm bg-slate-900 dark:bg-slate-700 text-white rounded hover:bg-slate-800 dark:hover:bg-slate-600">{t("subscription_detail.edit")}</button>
        </div>
      </PageHeader>

      <div className="grid grid-cols-1 md:grid-cols-3 gap-6 mb-8">
        <div className="col-span-2">
          <div className="bg-white dark:bg-slate-800 rounded-lg shadow p-4 mb-4">
            <h3 className="font-medium mb-3">{t("subscription_detail.details")}</h3>
            <dl className="text-sm space-y-2">
              <div className="flex gap-2"><dt className="text-gray-500 dark:text-gray-400 w-28">{t("subscription_detail.creator")}</dt><dd className="cursor-pointer text-blue-600 hover:underline" onClick={() => router.push(`/admin/creators/${s.creator_id}`)}>{s.creator_display_name || s.creator_name || getCreatorName(s.creator_id)}</dd></div>
              <div className="flex gap-2"><dt className="text-gray-500 dark:text-gray-400 w-28">{t("subscription_detail.status")}</dt><dd><StatusBadge status={s.is_active ? "up" : "down"} /></dd></div>
              <div className="flex gap-2"><dt className="text-gray-500 dark:text-gray-400 w-28">{t("subscription_detail.auto_sync")}</dt><dd>{s.sync_enabled ? <span className="text-green-600">{t("subscription_detail.sync_enabled")}</span> : <span className="text-gray-400 dark:text-gray-500">{t("subscription_detail.sync_disabled")}</span>}</dd></div>
              <div className="flex gap-2"><dt className="text-gray-500 dark:text-gray-400 w-28">{t("subscription_detail.last_synced")}</dt><dd className="text-xs">{s.last_synced_at ? new Date(s.last_synced_at).toLocaleString() : t("subscription_detail.never_synced")}</dd></div>
              <div className="flex gap-2"><dt className="text-gray-500 dark:text-gray-400 w-28">{t("subscription_detail.created")}</dt><dd className="text-xs">{new Date(s.created_at).toLocaleString()}</dd></div>
            </dl>
          </div>

          <div className="bg-white dark:bg-slate-800 rounded-lg shadow p-4">
            <div className="flex items-center justify-between mb-3">
              <h3 className="font-medium">{t("subscription_detail.sources_title").replace("{count}", String(sources.data?.length || 0))}</h3>
              <button onClick={() => setShowAddSource(true)} className="text-xs px-3 py-1 bg-slate-900 dark:bg-slate-700 text-white rounded hover:bg-slate-800 dark:hover:bg-slate-600">{t("subscription_detail.add_source")}</button>
            </div>
            {sources.data && sources.data.length > 0 ? (
              <div className="space-y-2">
                {sources.data.map((ss: SS) => (
                  <div key={ss.id} className="border rounded-lg p-3 text-sm">
                    <div className="flex items-center justify-between mb-2">
                      <div className="flex items-center gap-2">
                        <SourceBadge source={ss.source} />
                        <span className="font-medium">{ss.source_creator_id || ss.source_url?.split("/").pop() || "—"}</span>
                      </div>
                      <div className="flex items-center gap-2">
                        <button onClick={() => setToggleId(ss.id)}
                          className={`text-xs px-2 py-0.5 rounded ${ss.is_enabled ? "bg-green-100 text-green-700" : "bg-gray-100 text-gray-500"}`}>
                          {ss.is_enabled ? t("subscription_detail.sync_enabled") : t("subscription_detail.sync_disabled")}
                        </button>
                        <button onClick={() => startSync.mutate(ss.id)} disabled={startSync.isPending}
                          className="text-xs px-2 py-0.5 bg-blue-100 text-blue-700 rounded hover:bg-blue-200 disabled:opacity-50">
                          {startSync.isPending ? t("subscription_detail.syncing") : t("subscription_detail.sync_now")}
                        </button>
                        <button onClick={() => setDeleteSsId(ss.id)} className="text-xs text-red-500 hover:text-red-700 dark:text-red-400">{t("subscription_detail.remove_source")}</button>
                      </div>
                    </div>
                    <div className="text-xs text-gray-500 dark:text-gray-400 space-y-1">
                      {ss.source_url && <div>{t("subscription_detail.source_url")} <span className="text-blue-600">{ss.source_url}</span></div>}
                      <div className="flex gap-4">
                        <span>{t("subscription_detail.auth_healthy")} <StatusBadge status={ss.auth_healthy ? "up" : "down"} /></span>
                        {ss.last_successful_auth && <span>{t("subscription_detail.last_auth")} {new Date(ss.last_successful_auth).toLocaleDateString()}</span>}
                      </div>
                    </div>
                  </div>
                ))}
              </div>
            ) : (
              <EmptyState title={t("subscription_detail.no_sources")} description={t("subscription_detail.no_sources_desc")} />
            )}
            {startSync.error && <p className="text-red-600 text-sm mt-2">{(startSync.error as Error).message}</p>}
          </div>
        </div>

        <div className="bg-blue-50 dark:bg-blue-900/30 border border-blue-200 dark:border-blue-800 rounded-lg p-4 h-fit text-sm">
          <h4 className="font-medium text-blue-800 dark:text-blue-300 mb-2">{t("subscription_detail.multi_source_title")}</h4>
          <p className="text-blue-700">{t("subscription_detail.multi_source_desc")}</p>
        </div>
      </div>

      <Modal open={editing} onClose={() => setEditing(false)} title={t("subscription_detail.edit_title")}>
        <div className="space-y-4">
          <div><label className="block text-sm font-medium mb-1">{t("subscription_detail.name_field")}</label><input value={editName} onChange={(e) => setEditName(e.target.value)} className="w-full border rounded px-3 py-2 text-sm" /></div>
          <div className="flex justify-end gap-3 pt-2">
            <button onClick={() => setEditing(false)} className="px-4 py-2 text-sm border rounded hover:bg-gray-50 dark:hover:bg-slate-700 dark:bg-slate-800/50">{t("subscription_detail.cancel")}</button>
            <button onClick={() => update.mutate({ name: editName || undefined })} disabled={update.isPending} className="px-4 py-2 text-sm bg-slate-900 dark:bg-slate-700 text-white rounded hover:bg-slate-800 dark:hover:bg-slate-600">{t("subscription_detail.save")}</button>
          </div>
          {update.error && <p className="text-red-600 text-sm">{(update.error as Error).message}</p>}
        </div>
      </Modal>

      <Modal open={showAddSource} onClose={() => setShowAddSource(false)} title={t("subscription_detail.add_source_title")}><AddSourceForm subId={id} onClose={() => setShowAddSource(false)} /></Modal>
      {toggleId && <ConfirmDialog open title={sources.data?.find((ss: SS) => ss.id === toggleId)?.is_enabled ? t("subscription_detail.disable_source_title") : t("subscription_detail.enable_source_title")} message={t("subscription_detail.toggle_source_msg")} onConfirm={() => { const ss = sources.data?.find((s: SS) => s.id === toggleId); if (ss) toggleSource.mutate({ ssId: toggleId, enabled: !ss.is_enabled }); }} onCancel={() => setToggleId(null)} isPending={toggleSource.isPending} error={(toggleSource.error as Error)?.message} />}
      {deleteSsId && <ConfirmDialog open title={t("subscription_detail.delete_source_title")} message={t("subscription_detail.delete_source_msg")} onConfirm={() => deleteSource.mutate(deleteSsId)} onCancel={() => setDeleteSsId(null)} isPending={deleteSource.isPending} error={(deleteSource.error as Error)?.message} />}
    </main>
  );
}

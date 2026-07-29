"use client";
import Link from "next/link";
import { useMemo, useState } from "react";
import { useT } from "@/lib/i18n";
import { useParams, useRouter } from "next/navigation";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api, CreatorRepository, queryKeys, SubscriptionSource as SS, ProviderInfo } from "@/lib/api";
import { PageHeader, PageShell, StatusBadge, Modal, ConfirmDialog, ErrorState, EmptyState, RepositoryCard } from "@/components";
import { useToast } from "@/components/Toast";
import { useI18nFormat } from "@/lib/i18n-format";

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
        <select value={source} onChange={(e) => handleSourceChange(e.target.value)} className="select w-full">
          {sources.data?.sources?.filter((s: ProviderInfo) => s.capabilities.can_download || s.capabilities.can_import_local).map((s: ProviderInfo) => <option key={s.source_name} value={s.source_name}>{s.display_name} ({s.source_name})</option>)}
        </select>
        <p className="mt-1 text-xs text-muted">{t("subscription_detail.multi_source_hint")}</p>
      </div>
      <div>
        <label className="block text-sm font-medium mb-1">{t("subscription_detail.source_creator_id")}</label>
        <input value={sourceCreatorId} onChange={(e) => handleIdChange(e.target.value)} className="input w-full" placeholder={t("subscription_detail.source_creator_id_placeholder")} />
        <p className="mt-1 text-xs text-muted">{t("subscription_detail.source_creator_id_hint")}</p>
      </div>
      <div>
        <label className="block text-sm font-medium mb-1">{t("subscription_detail.source_url_field")}</label>
        <input value={sourceUrl} onChange={(e) => setSourceUrl(e.target.value)} className="input w-full font-mono" placeholder={urlHint} />
        <p className="mt-1 text-xs text-muted">{t("subscription_detail.source_url_hint")} {urlHint}</p>
      </div>
      <div className="flex justify-end gap-3 pt-2">
        <button onClick={onClose} className="btn-ghost">{t("subscription_detail.cancel")}</button>
        <button onClick={() => create.mutate()} disabled={create.isPending} className="btn-primary">{create.isPending ? t("subscription_detail.adding") : t("subscription_detail.add_source_btn")}</button>
      </div>
      {create.error && <p className="text-sm text-danger dark:text-danger">{(create.error as Error).message}</p>}
    </div>
  );
}

export default function SubscriptionDetailPage() {
  const params = useParams(); const router = useRouter(); const qc = useQueryClient();
  const t = useT();
  const toast = useToast();
  const fmt = useI18nFormat();
  const id = params.id as string;

  const sub = useQuery({ queryKey: queryKeys.subscriptions.detail(id), queryFn: () => api.getSubscription(id) });
  const sources = useQuery({ queryKey: queryKeys.subscriptions.sources(id), queryFn: () => api.listSubscriptionSources(id) });
  const jobs = useQuery({ queryKey: [...queryKeys.downloadJobs.all, "subscription", id], queryFn: () => api.listDownloadJobs({ subscription_id: id, limit: 50 }), refetchInterval: 12000 });
  const decisions = useQuery({ queryKey: [...queryKeys.schedulerDecisions, "subscription", id], queryFn: api.schedulerDecisions, refetchInterval: 15000 });
  const providerInfos = useQuery({ queryKey: queryKeys.sources, queryFn: api.sources });
  const creators = useQuery({ queryKey: queryKeys.creators.all, queryFn: () => api.listCreators() });
  const [showAddSource, setShowAddSource] = useState(false);
  const [editing, setEditing] = useState(false); const [editName, setEditName] = useState("");
  const [editMode, setEditMode] = useState(""); const [editInterval, setEditInterval] = useState(0);
  const [editTimes, setEditTimes] = useState("");
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
      return api.syncRepository(ssId);
    },
    onSuccess: (result) => {
      qc.invalidateQueries({ queryKey: queryKeys.downloadJobs.all });
      qc.invalidateQueries({ queryKey: queryKeys.tasks.all });
      qc.invalidateQueries({ queryKey: queryKeys.subscriptions.sources(id) });
      qc.invalidateQueries({ queryKey: queryKeys.schedulerDecisions });
      if (result.status === "enqueued") toast.success(result.message || t("repo_detail.sync_queued"));
      else {
        const reason = typeof result.reason === "object" ? result.reason?.message : result.reason;
        toast.warning(reason || result.message || t("subscriptions.sync_no_jobs"));
      }
    },
    onError: (e: Error) => toast.error(e.message),
  });
  const decisionBySource = useMemo(
    () => new Map((decisions.data?.items || []).filter((item) => item.subscription_id === id).map((item) => [item.source_id, item])),
    [decisions.data?.items, id],
  );
  const detailStats = useMemo(() => {
    const sourceRows = sources.data || [];
    const decisionRows = [...decisionBySource.values()];
    const jobRows = jobs.data || [];
    return {
      total: sourceRows.length,
      enabled: sourceRows.filter((item) => item.is_enabled).length,
      due: decisionRows.filter((item) => item.due).length,
      blocked: decisionRows.filter((item) => ["auth_unhealthy", "url_invalid", "unknown_provider", "provider_not_downloadable"].includes(item.reason)).length,
      running: jobRows.filter((job) => ["enqueued", "pending", "downloading", "downloaded", "importing"].includes(job.status)).length,
      failed: jobRows.filter((job) => ["failed", "stale"].includes(job.status)).length,
      nextDueAt: decisionRows.map((item) => item.next_due_at).filter(Boolean).sort()[0] || null,
    };
  }, [decisionBySource, jobs.data, sources.data]);

  const getCreatorName = (creatorId: string) => {
    const c = creators.data?.items.find((c) => c.id === creatorId);
    return c ? (c.display_name || c.name) : creatorId.slice(0, 8);
  };

  if (sub.isLoading) return <PageShell><div className="animate-pulse space-y-4"><div className="h-8 w-1/4 rounded bg-subtle dark:bg-subtle" /><div className="h-32 rounded bg-subtle dark:bg-subtle" /></div></PageShell>;
  if (sub.error) return <PageShell><ErrorState message={(sub.error as Error).message} onRetry={() => sub.refetch()} /></PageShell>;
  if (!sub.data) return null;
  const s = sub.data;
  const providerMap = new Map((providerInfos.data?.sources || []).map((p: ProviderInfo) => [p.source_name, p]));
  const toRepo = (ss: SS): CreatorRepository => {
    const provider = providerMap.get(ss.source);
    return {
      id: ss.id,
      subscription_id: ss.subscription_id,
      source: ss.source,
      source_display_name: provider?.display_name || ss.source,
      source_creator_id: ss.source_creator_id,
      source_url: ss.source_url,
      is_enabled: ss.is_enabled,
      auth_healthy: ss.auth_healthy,
      last_successful_auth: ss.last_successful_auth,
      last_synced_at: ss.last_synced_at,
      can_download: !!provider?.capabilities.can_download,
      supports_gallerydl: !!provider?.capabilities.supports_gallerydl,
      url_valid: !!ss.source_url,
      is_repository: !!provider?.capabilities.can_download && !!ss.source_url,
      latest_job: null,
      created_at: ss.created_at,
      updated_at: ss.updated_at,
    };
  };

  return (
    <PageShell>
      <Link href="/admin/subscriptions" className="inline-flex min-h-11 items-center gap-1 text-sm text-accent hover:underline dark:text-accent">{t("subscription_detail.back")}</Link>
      <PageHeader title={s.name || (s.creator_display_name || s.creator_name || getCreatorName(s.creator_id))} description={s.creator_display_name || s.creator_name ? `${t("subscription_detail.creator")} ${s.creator_display_name || s.creator_name}` : undefined}>
        <div className="flex gap-2">
          <button onClick={() => { setEditName(s.name || ""); setEditMode(s.schedule_mode || ""); setEditInterval(s.sync_interval_hours || 24); setEditTimes(s.scheduled_times || ""); setEditing(true); }} className="btn-primary">{t("subscription_detail.edit")}</button>
        </div>
      </PageHeader>

      <div className="mb-6 grid grid-cols-2 gap-3 md:grid-cols-4 xl:grid-cols-6">
        <div className="card p-3"><div className="text-lg font-semibold text-fg">{detailStats.enabled}/{detailStats.total}</div><div className="text-xs uppercase text-muted">{t("subscriptions.col_sources")}</div></div>
        <div className="card p-3"><div className="text-lg font-semibold text-accent">{detailStats.due}</div><div className="text-xs uppercase text-muted">{t("scheduler.filter_due")}</div></div>
        <div className="card p-3"><div className={`text-lg font-semibold ${detailStats.blocked ? "text-danger" : "text-fg"}`}>{detailStats.blocked}</div><div className="text-xs uppercase text-muted">{t("scheduler.filter_blocked")}</div></div>
        <div className="card p-3"><div className="text-lg font-semibold text-fg">{detailStats.running}</div><div className="text-xs uppercase text-muted">{t("subscriptions.running")}</div></div>
        <div className="card p-3"><div className={`text-lg font-semibold ${detailStats.failed ? "text-danger" : "text-fg"}`}>{detailStats.failed}</div><div className="text-xs uppercase text-muted">{t("subscriptions.failed")}</div></div>
        <div className="card p-3"><div className="truncate text-sm font-semibold text-fg">{fmt.dateTime(detailStats.nextDueAt)}</div><div className="text-xs uppercase text-muted">{t("subscriptions.next_due_short")}</div></div>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-3 gap-6 mb-8">
        <div className="col-span-2">
          <div className="card p-4 mb-4">
            <h3 className="font-medium mb-3">{t("subscription_detail.details")}</h3>
            <dl className="text-sm space-y-2">
              <div className="flex gap-2">
                <dt className="w-28 text-muted">{t("subscription_detail.creator")}</dt>
                <dd>
                  <Link className="text-accent hover:underline dark:text-accent" href={`/admin/creators/${s.creator_id}`}>
                    {s.creator_display_name || s.creator_name || getCreatorName(s.creator_id)}
                  </Link>
                </dd>
              </div>
              <div className="flex gap-2"><dt className="w-28 text-muted">{t("subscription_detail.status")}</dt><dd><StatusBadge status={s.is_active ? "up" : "down"} /></dd></div>
              <div className="flex gap-2"><dt className="w-28 text-muted">{t("subscription_detail.auto_sync")}</dt><dd>{s.sync_enabled ? <span className="text-success dark:text-success">{t("subscription_detail.sync_enabled")}</span> : <span className="text-placeholder dark:text-muted">{t("subscription_detail.sync_disabled")}</span>}</dd></div>
              <div className="flex gap-2"><dt className="w-28 text-muted">{t("subscription_detail.sync_strategy")}</dt><dd className="text-xs">
                {!s.schedule_mode || s.schedule_mode === "inherit" ? (
                  <span className="text-muted">{t("subscription_detail.strategy_inherit")}</span>
                ) : s.schedule_mode === "manual" ? (
                  <span className="text-orange-600">{t("subscription_detail.strategy_manual")}</span>
                ) : s.schedule_mode === "fixed_time" ? (
                  <span className="text-purple-600">{t("subscription_detail.strategy_fixed_time")}{s.scheduled_times ? " · " + s.scheduled_times : ""}</span>
                ) : (
                  <span className="text-blue-600">{t("subscription_detail.strategy_interval")} · {s.sync_interval_hours}h</span>
                )}
              </dd></div>
              <div className="flex gap-2"><dt className="w-28 text-muted">{t("subscription_detail.last_synced")}</dt><dd className="text-xs">{s.last_synced_at ? fmt.dateTime(s.last_synced_at) : t("subscription_detail.never_synced")}</dd></div>
              <div className="flex gap-2"><dt className="w-28 text-muted">{t("subscription_detail.created")}</dt><dd className="text-xs">{fmt.dateTime(s.created_at)}</dd></div>
            </dl>
          </div>

          <div className="card p-4">
            <div className="flex items-center justify-between mb-3">
              <h3 className="font-medium">{t("subscription_detail.sources_title").replace("{count}", String(sources.data?.length || 0))}</h3>
              <button onClick={() => setShowAddSource(true)} className="btn-primary">{t("subscription_detail.add_source")}</button>
            </div>
            {sources.data && sources.data.length > 0 ? (
              <div className="space-y-2">
                {sources.data.map((ss: SS) => (
                  <RepositoryCard key={ss.id} repo={toRepo(ss)}
                    onSync={() => startSync.mutate(ss.id)}
                    onToggle={() => setToggleId(ss.id)}
                    onDelete={() => setDeleteSsId(ss.id)}
                    syncPending={startSync.isPending}
                    togglePending={toggleSource.isPending}
                    decision={decisionBySource.get(ss.id)} />
                ))}
              </div>
            ) : (
              <EmptyState title={t("subscription_detail.no_sources")} description={t("subscription_detail.no_sources_desc")} />
            )}
            {startSync.error && <p className="text-red-600 text-sm mt-2">{(startSync.error as Error).message}</p>}
          </div>
        </div>

        <div className="card h-fit p-4 text-sm">
          <h4 className="font-medium mb-2">{t("subscription_detail.multi_source_title")}</h4>
          <p className="text-muted">{t("subscription_detail.multi_source_desc")}</p>
          <div className="mt-4 space-y-2 border-t border-border pt-4 text-xs dark:border-border">
            <div className="flex justify-between"><span>{t("subscription_detail.repositories")}</span><span className="font-semibold">{sources.data?.length || 0}</span></div>
            <div className="flex justify-between"><span>{t("subscription_detail.enabled")}</span><span className="font-semibold">{sources.data?.filter((x) => x.is_enabled).length || 0}</span></div>
            <div className="flex justify-between"><span>{t("subscription_detail.last_sync_short")}</span><span className="font-semibold">{s.last_synced_at ? fmt.date(s.last_synced_at) : t("subscription_detail.never_synced")}</span></div>
          </div>
        </div>
      </div>

      <Modal open={editing} onClose={() => setEditing(false)} title={t("subscription_detail.edit_title")}>
        <div className="space-y-4">
          <div><label className="block text-sm font-medium mb-1">{t("subscription_detail.name_field")}</label><input value={editName} onChange={(e) => setEditName(e.target.value)} className="input w-full" /></div>
          <div>
            <label className="block text-sm font-medium mb-1">{t("subscription_detail.sync_strategy")}</label>
            <select value={editMode} onChange={(e) => setEditMode(e.target.value)}
              className="select w-full">
              <option value="">{t("subscription_detail.strategy_inherit")}</option>
              <option value="interval">{t("subscription_detail.strategy_interval")}</option>
              <option value="fixed_time">{t("subscription_detail.strategy_fixed_time")}</option>
              <option value="manual">{t("subscription_detail.strategy_manual")}</option>
            </select>
            <p className="mt-1 text-xs text-muted">{t("subscription_detail.strategy_desc")}</p>
          </div>
          {editMode === "interval" && (
            <div>
              <label className="block text-sm font-medium mb-1">{t("subdefaults.sync_interval")}</label>
              <div className="flex w-fit items-center gap-1">
                <input type="number" min={1} max={168} value={editInterval}
                  onChange={(e) => setEditInterval(parseInt(e.target.value) || 24)}
                  className="input w-16 px-2 py-1.5 text-center font-mono" />
                <span className="pr-2 text-xs text-muted">{t("subscription_detail.hours", { count: editInterval })}</span>
              </div>
            </div>
          )}
          {editMode === "fixed_time" && (
            <div>
              <label className="block text-sm font-medium mb-1">{t("subdefaults.scheduled_times")}</label>
              <input value={editTimes} onChange={(e) => setEditTimes(e.target.value)}
                placeholder="03:00, 21:00"
                className="input w-full font-mono" />
              <p className="mt-1 text-xs text-muted">{t("subdefaults.scheduled_times.example")}</p>
            </div>
          )}
          <div className="flex justify-end gap-3 pt-2">
            <button onClick={() => setEditing(false)} className="btn-ghost">{t("subscription_detail.cancel")}</button>
            <button onClick={() => update.mutate({
              name: editName || undefined,
              schedule_mode: editMode || null,
              sync_interval_hours: editMode === "interval" ? editInterval : undefined,
              scheduled_times: editMode === "fixed_time" ? (editTimes || null) : undefined,
            })} disabled={update.isPending} className="btn-primary">{t("subscription_detail.save")}</button>
          </div>
          {update.error && <p className="text-sm text-danger dark:text-danger">{(update.error as Error).message}</p>}
        </div>
      </Modal>

      <Modal open={showAddSource} onClose={() => setShowAddSource(false)} title={t("subscription_detail.add_source_title")}><AddSourceForm subId={id} onClose={() => setShowAddSource(false)} /></Modal>
      {toggleId && <ConfirmDialog open title={sources.data?.find((ss: SS) => ss.id === toggleId)?.is_enabled ? t("subscription_detail.disable_source_title") : t("subscription_detail.enable_source_title")} message={t("subscription_detail.toggle_source_msg")} onConfirm={() => { const ss = sources.data?.find((s: SS) => s.id === toggleId); if (ss) toggleSource.mutate({ ssId: toggleId, enabled: !ss.is_enabled }); }} onCancel={() => setToggleId(null)} isPending={toggleSource.isPending} error={(toggleSource.error as Error)?.message} />}
      {deleteSsId && <ConfirmDialog open title={t("subscription_detail.delete_source_title")} message={t("subscription_detail.delete_source_msg")} onConfirm={() => deleteSource.mutate(deleteSsId)} onCancel={() => setDeleteSsId(null)} isPending={deleteSource.isPending} error={(deleteSource.error as Error)?.message} />}
    </PageShell>
  );
}

"use client";
import { useState, useMemo, useEffect, Suspense } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api, queryKeys } from "@/lib/api";
import { useStaggeredEntrance } from "@/lib/motion";
import { PageHeader, PageSection, EmptyState, ErrorState, ConfirmDialog, Modal, FilterBar, SelectionBar, PageShell, PermissionGuard, EntityList, EntityRow, RowActionMenu } from "@/components";
import { useNotifications } from "@/components/NotificationCenter";
import { useRouter, useSearchParams, usePathname } from "next/navigation";
import { useT } from "@/lib/i18n";
import { useI18nFormat } from "@/lib/i18n-format";
import { useToast } from "@/components/Toast";
import { usePermissions } from "@/lib/usePermissions";

type FilterMode = "all" | "active" | "inactive" | "has_danbooru" | "has_subscription" | "no_subscription" | "favorites";

function CreateForm({ isPending, error, onSubmit, onClose }: {
  isPending: boolean; error: Error | null;
  onSubmit: (data: { name: string; display_name?: string; description?: string }) => void;
  onClose: () => void;
}) {
  const t = useT();
  const toast = useToast();
  const [name, setName] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [description, setDescription] = useState("");
  const [urlInput, setUrlInput] = useState("");

  // Auto-detect name from pasted URL
  const handleUrlPaste = (val: string) => {
    setUrlInput(val);
    if (!name) {
      // Extract username from common URL patterns
      let detected = "";
      const m = val.match(/(?:pixiv\.net\/(?:en\/)?users\/|x\.com\/|twitter\.com\/|iwara\.tv\/users?\/|danbooru\.donmai\.us\/artists\/|weibo\.com\/(?:u\/|n\/|p\/)?|lofter\.com\/people\/|bilibili\.com\/)([\w.-]+)/);
      if (m) detected = m[1];
      // Also try pixiv artist ID
      if (!detected) {
        const m2 = val.match(/pixiv\.net\/(?:en\/)?users\/(\d+)/);
        if (m2) detected = "pixiv_" + m2[1];
      }
      if (detected && detected !== "home" && detected !== "n") setName(detected);
    }
  };

  return (
    <div className="space-y-4">
      <div>
        <label className="block text-sm font-medium mb-1">{t("creators.source_url_label") || "来源 URL"}</label>
        <input value={urlInput} onChange={(e) => handleUrlPaste(e.target.value)}
          className="input w-full font-mono"
          placeholder="https://www.pixiv.net/users/123456 或 https://x.com/username" />
        <p className="mt-1 text-xs text-muted">粘贴 URL 自动提取创作者名。支持 Pixiv / X / Iwara / Danbooru / Weibo / Lofter / Bilibili。</p>
      </div>
      <div className="grid grid-cols-2 gap-4">
        <div>
          <label className="block text-sm font-medium mb-1">{t("creators.name_label")} <span className="text-red-400">*</span></label>
          <input value={name} onChange={(e) => setName(e.target.value)} className="input w-full" placeholder={t("creators.name_placeholder")} />
        </div>
        <div>
          <label className="block text-sm font-medium mb-1">{t("creators.display_name_label")}</label>
          <input value={displayName} onChange={(e) => setDisplayName(e.target.value)} className="input w-full" placeholder="可选显示名" />
        </div>
      </div>
      <div><label className="block text-sm font-medium mb-1">{t("creators.description_label")}</label><textarea value={description} onChange={(e) => setDescription(e.target.value)} className="textarea w-full" rows={2} /></div>
      <div className="flex justify-end gap-3 pt-2">
        <button onClick={onClose} className="btn-ghost">{t("creators.cancel")}</button>
        <button onClick={() => onSubmit({ name, display_name: displayName || undefined, description: description || undefined })} disabled={!name || isPending}
          className="btn-primary">
          {isPending ? t("creators.creating") : t("creators.create")}
        </button>
      </div>
      {error && <p className="text-sm text-danger dark:text-danger">{error.message}</p>}
    </div>
  );
}

function buildFilters(mode: FilterMode, search: string) {
  const f: Record<string, string | boolean | undefined> = {};
  if (search) f.search = search;
  switch (mode) {
    case "active": f.is_active = true; break;
    case "inactive": f.is_active = false; break;
    case "has_danbooru": f.has_danbooru = true; break;
    case "has_subscription": f.has_subscription = true; break;
    case "no_subscription": f.has_subscription = false; break;
    case "favorites": f.is_favorite = true; break;
  }
  return f;
}

function CreatorsContent() {
  const t = useT();
  const fmt = useI18nFormat();
  const toast = useToast();
  const router = useRouter();
  const qc = useQueryClient();
  const notify = useNotifications();
  const sp = useSearchParams();
  const pathname = usePathname();
  const { has } = usePermissions();
  const canCurate = has("curation");

  // Filter state derived from URL
  const search = sp.get("q") ?? "";
  const filter = (sp.get("filter") as FilterMode) ?? "all";
  const page = Number(sp.get("p") ?? "0");

  const [showCreate, setShowCreate] = useState(false);
  const [deleteId, setDeleteId] = useState<string | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [confirmBatchDel, setConfirmBatchDel] = useState(false);
  const limit = 25;

  // Local input for search field — debounced 300ms before writing to URL
  const [inputVal, setInputVal] = useState(search);
  useEffect(() => { setInputVal(search); }, [search]);
  useEffect(() => {
    if (inputVal === search) return;
    const timer = setTimeout(() => {
      const p = new URLSearchParams(sp.toString());
      if (inputVal) p.set("q", inputVal); else p.delete("q");
      p.delete("p");
      router.replace(`${pathname}?${p.toString()}`, { scroll: false });
    }, 300);
    return () => clearTimeout(timer);
  }, [inputVal]); // eslint-disable-line react-hooks/exhaustive-deps

  function updateParams(updates: Record<string, string | null>, resetPage = true) {
    const p = new URLSearchParams(sp.toString());
    for (const [k, v] of Object.entries(updates)) {
      if (v === null || v === "") p.delete(k); else p.set(k, v);
    }
    if (resetPage) p.delete("p");
    router.replace(`${pathname}?${p.toString()}`, { scroll: false });
  }

  const FILTERS: { key: FilterMode; label: string }[] = useMemo(() => [
    { key: "all", label: t("creators.filter_all") },
    { key: "active", label: t("creators.filter_active") },
    { key: "inactive", label: t("creators.filter_inactive") },
    { key: "has_danbooru", label: t("creators.filter_danbooru") },
    { key: "has_subscription", label: t("creators.filter_subscribed") },
    { key: "no_subscription", label: t("creators.filter_no_sub") },
    { key: "favorites", label: t("creators.filter_favorites") },
  ], [t]);

  const filters = useMemo(() => buildFilters(filter, search), [filter, search]);

  const creatorCount = useQuery({ queryKey: queryKeys.creators.count, queryFn: () => api.countCreators() });
  const creators = useQuery({
    queryKey: queryKeys.creators.list(page, limit, filters),
    queryFn: () => api.listCreators(page * limit, limit, filters as any),
    placeholderData: (previousData) => previousData,
  });
  const creatorItems = creators.data?.items || [];
  const creatorEntrance = useStaggeredEntrance(creatorItems.map((creator) => creator.id));

  useEffect(() => {
    if (notify.operationJob?.kind !== "danbooru-import-all" || notify.operationJob.status !== "completed") return;
    creators.refetch();
    creatorCount.refetch();
  }, [notify.operationJob?.jobId, notify.operationJob?.status]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (notify.batchJob?.status !== "completed") return;
    creators.refetch();
    creatorCount.refetch();
  }, [notify.batchJob?.jobId, notify.batchJob?.status]); // eslint-disable-line react-hooks/exhaustive-deps

  const refreshCreatorViews = () => {
    qc.invalidateQueries({ queryKey: queryKeys.creators.all });
    qc.invalidateQueries({ queryKey: queryKeys.subscriptions.all });
  };

  const create = useMutation({
    mutationFn: (data: { name: string; display_name?: string; description?: string }) => api.createCreator(data),
    onSuccess: () => { setShowCreate(false); refreshCreatorViews(); toast.success({ message: t("notification.created") }); },
    onError: (e: Error) => toast.error({ message: e.message }),
  });

  const del = useMutation({
    mutationFn: (id: string) => api.deleteCreator(id),
    onSuccess: () => { setDeleteId(null); refreshCreatorViews(); toast.success({ message: t("notification.deleted") }); },
  });

  const batchDel = useMutation({
    mutationFn: (ids: string[]) => api.batchDeleteCreators(ids),
    onSuccess: () => { setSelected(new Set()); setConfirmBatchDel(false); refreshCreatorViews(); toast.success({ message: t("notification.deleted") }); },
  });

  useEffect(() => {
    if (!creators.data?.items) return;
    const visibleIds = new Set(creators.data.items.map((creator) => creator.id));
    setSelected((prev) => {
      const next = new Set([...prev].filter((id) => visibleIds.has(id)));
      return next.size === prev.size ? prev : next;
    });
  }, [creators.data]);

  const toggleSelect = (id: string) => {
    const next = new Set(selected);
    next.has(id) ? next.delete(id) : next.add(id);
    setSelected(next);
  };
  const selectAll = () => {
    if (selected.size === (creators.data?.items.length || 0)) setSelected(new Set());
    else setSelected(new Set((creators.data?.items || []).map((c) => c.id)));
  };

  const toggleFavorite = useMutation({
    mutationFn: (id: string) => api.toggleCreatorFavorite(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: queryKeys.creators.all }),
  });

  const handleFilterChange = (mode: FilterMode) => {
    updateParams({ filter: mode === "all" ? null : mode });
  };

  const handleSearchChange = (value: string) => {
    setInputVal(value);
  };

  return (
    <PageShell size="normal">
      <PageHeader title={t("creators.title")} description={t("creators.count", "0 creators").replace("{count}", String(creatorCount.data?.count ?? 0))}>
        <div className="flex gap-2">
          <button onClick={() => router.push("/admin/creators/duplicates")} className="btn-ghost">{t("creators.duplicates")}</button>
          {canCurate && <button onClick={() => setShowCreate(true)} className="btn-primary">{t("creators.new")}</button>}
        </div>
      </PageHeader>

      {/* Toolbar */}
      <FilterBar>
        <input value={inputVal} onChange={(e) => handleSearchChange(e.target.value)} placeholder={t("creators.search")} className="input w-56 py-1.5" />
        <div className="segmented-control">
          {FILTERS.map((f) => (
            <button key={f.key} onClick={() => handleFilterChange(f.key)}
              className={`segment ${filter === f.key ? "segment-active" : ""}`}>
              {f.label}
            </button>
          ))}
        </div>
      </FilterBar>

      <PageSection className="mt-5">
      {canCurate && (
        <SelectionBar
          count={selected.size}
          label={t("creators.delete_selected").replace("{count}", String(selected.size))}
          clearLabel={t("common.clear")}
          onClear={() => setSelected(new Set())}
        >
          <button onClick={() => setConfirmBatchDel(true)} className="btn-danger text-xs">
            {t("creators.delete_selected").replace("{count}", String(selected.size))}
          </button>
        </SelectionBar>
      )}

      {/* Select all */}
      {canCurate && creators.data && creators.data.items.length > 0 && (
        <label className="mb-2 flex cursor-pointer items-center gap-2 text-xs text-muted">
          <input type="checkbox" aria-label={t("creators.select_all")} checked={selected.size === creators.data.items.length && creators.data.items.length > 0} onChange={selectAll} className="rounded" />
          {t("creators.select_all")}
        </label>
      )}

      {/* Content */}
      {creators.isLoading && <div className="space-y-2">{Array.from({ length: 5 }).map((_, i) => <div key={i} className="h-16 rounded-md bg-subtle dark:bg-subtle animate-pulse" />)}</div>}
      {creators.error && <ErrorState message={(creators.error as Error).message} onRetry={() => creators.refetch()} />}
      {creators.data && !creators.data.items.length && (
        <EmptyState
          title={search || filter !== "all" ? t("works.no_works_filter") : t("creators.no_creators")}
          description={search || filter !== "all" ? undefined : t("creators.no_creators_desc")}
          action={(canCurate && !search && filter === "all") ? <button onClick={() => setShowCreate(true)} className="btn-primary">{t("creators.create_creator")}</button> : undefined}
        />
      )}

      {creators.data && creators.data.items.length > 0 && (
        <EntityList label={t("creators.title")}>
          {creators.data.items.map((c, i) => (
            <EntityRow
              key={c.id}
              label={t("common.open_item", { name: c.display_name || c.name })}
              selected={selected.has(c.id)}
              entrance={creatorEntrance(c.id, i)}
              onOpen={() => router.push(`/admin/creators/${c.id}`)}
            >
              {canCurate && (
                <input
                  type="checkbox"
                  aria-label={t("common.select_item", { name: c.display_name || c.name })}
                  checked={selected.has(c.id)}
                  onChange={() => toggleSelect(c.id)}
                  className="shrink-0 rounded"
                  onClick={(event) => event.stopPropagation()}
                />
              )}
              <div className="entity-avatar">
                {(c.display_name || c.name).slice(0, 2).toUpperCase()}
              </div>
              <div className="entity-main">
                <div className="entity-title-line">
                  <span className="entity-title">{c.display_name || c.name}</span>
                  {c.display_name && <span className="truncate font-mono text-xs text-muted">{c.name}</span>}
                  <span
                    className={`h-1.5 w-1.5 shrink-0 rounded-full ${c.is_active ? "bg-success" : "bg-border"}`}
                    title={c.is_active ? t("creators.filter_active") : t("creators.filter_inactive")}
                  />
                  {(c as any).danbooru_artist_id && <span className="rounded-full bg-purple-100 px-2 py-0.5 font-mono text-[10px] text-purple-700 dark:bg-purple-900/30 dark:text-purple-400">D#{String((c as any).danbooru_artist_id)}</span>}
                  {(c.subscription_count ?? 0) > 0 && <span className="rounded-full bg-success-subtle px-2 py-0.5 text-[10px] text-success">{t("creators.sub_badge")}</span>}
                </div>
                {c.description && <p className="entity-supporting">{c.description}</p>}
                <div className="entity-meta">
                  <span>{t("creators.repository_count", { count: c.repository_count ?? 0 })}</span>
                  <span>{t("creators.source_count", { count: c.source_count ?? 0 })}</span>
                  <span>{t("creators.last_sync", { time: c.last_synced_at ? fmt.dateTime(c.last_synced_at) : t("common.never") })}</span>
                </div>
              </div>
              <div className="entity-actions" onClick={(event) => event.stopPropagation()}>
                {canCurate && (
                  <button
                    type="button"
                    onClick={() => toggleFavorite.mutate(c.id)}
                    className={`btn-icon text-lg ${c.is_favorite ? "text-warning" : "text-muted hover:text-warning"}`}
                    title={c.is_favorite ? t("common.unfavorite") : t("common.favorite")}
                    aria-label={c.is_favorite ? t("common.unfavorite") : t("common.favorite")}
                  >
                    {c.is_favorite ? "★" : "☆"}
                  </button>
                )}
                {canCurate && (
                  <RowActionMenu
                    label={t("common.more_actions")}
                    items={[
                      {
                        label: t("creators.del"),
                        tone: "danger",
                        onSelect: () => setDeleteId(c.id),
                      },
                    ]}
                  />
                )}
              </div>
            </EntityRow>
          ))}
        </EntityList>
      )}

      {/* Pagination */}
      {(creators.data?.items.length || 0) > 0 && (
        <div className="flex gap-2 justify-center mt-4">
          <button disabled={page === 0} onClick={() => updateParams({ p: page <= 1 ? null : String(page - 1) }, false)} className="btn-ghost disabled:opacity-30">{t("common.prev")}</button>
          <span className="px-3 py-1 text-sm text-muted">{t("common.page").replace("{page}", String(page + 1))}</span>
          <button onClick={() => updateParams({ p: String(page + 1) }, false)} disabled={!creators.data?.items || creators.data.items.length < limit} className="btn-ghost disabled:opacity-30">{t("common.next")}</button>
        </div>
      )}
      </PageSection>

      <Modal open={showCreate} onClose={() => setShowCreate(false)} title={t("creators.new_creator_title")}>
        <CreateForm isPending={create.isPending} error={create.error} onSubmit={(data) => create.mutate(data)} onClose={() => setShowCreate(false)} />
      </Modal>
      {deleteId && <ConfirmDialog open title={t("creators.delete_title")} message={t("creators.delete_msg")} onConfirm={() => del.mutate(deleteId)} onCancel={() => setDeleteId(null)} isPending={del.isPending} error={(del.error as Error)?.message} />}
      {confirmBatchDel && <ConfirmDialog open title={t("creators.batch_delete_title")} message={t("creators.batch_delete_msg").replace("{count}", String(selected.size))} onConfirm={() => batchDel.mutate([...selected])} onCancel={() => setConfirmBatchDel(false)} isPending={batchDel.isPending} error={(batchDel.error as Error)?.message} />}
    </PageShell>
  );
}

export default function CreatorsPage() {
  return (
    <PermissionGuard module="library">
      <Suspense>
        <CreatorsContent />
      </Suspense>
    </PermissionGuard>
  );
}

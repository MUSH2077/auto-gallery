"use client";
import { useState, useMemo } from "react";
import { useQuery, useMutation } from "@tanstack/react-query";
import { api, queryKeys } from "@/lib/api";
import { PageHeader, EmptyState, ErrorState, ConfirmDialog, Modal, SourceBadge } from "@/components";
import { useRouter } from "next/navigation";
import { useT } from "@/lib/i18n";

type FilterMode = "all" | "active" | "inactive" | "has_danbooru" | "has_subscription" | "no_subscription" | "favorites";

function CreateForm({ isPending, error, onSubmit, onClose }: {
  isPending: boolean; error: Error | null;
  onSubmit: (data: { name: string; display_name?: string; description?: string }) => void;
  onClose: () => void;
}) {
  const t = useT();
  const [name, setName] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [description, setDescription] = useState("");
  return (
    <div className="space-y-4">
      <div><label className="block text-sm font-medium mb-1">{t("creators.name_label")}</label><input value={name} onChange={(e) => setName(e.target.value)} className="w-full border rounded px-3 py-2 text-sm dark:bg-slate-700 dark:text-white" placeholder={t("creators.name_placeholder")} /></div>
      <div><label className="block text-sm font-medium mb-1">{t("creators.display_name_label")}</label><input value={displayName} onChange={(e) => setDisplayName(e.target.value)} className="w-full border rounded px-3 py-2 text-sm dark:bg-slate-700 dark:text-white" /></div>
      <div><label className="block text-sm font-medium mb-1">{t("creators.description_label")}</label><textarea value={description} onChange={(e) => setDescription(e.target.value)} className="w-full border rounded px-3 py-2 text-sm dark:bg-slate-700 dark:text-white" rows={3} /></div>
      <div className="flex justify-end gap-3 pt-2">
        <button onClick={onClose} className="px-4 py-2 text-sm border rounded hover:bg-gray-50 dark:hover:bg-slate-700 dark:text-gray-300">{t("creators.cancel")}</button>
        <button onClick={() => onSubmit({ name, display_name: displayName || undefined, description: description || undefined })} disabled={!name || isPending}
          className="px-4 py-2 text-sm bg-slate-900 dark:bg-slate-700 text-white rounded hover:bg-slate-800 dark:hover:bg-slate-600 disabled:opacity-50">
          {isPending ? t("creators.creating") : t("creators.create")}
        </button>
      </div>
      {error && <p className="text-red-600 text-sm">{error.message}</p>}
    </div>
  );
}

export default function CreatorsPage() {
  const t = useT();
  const router = useRouter();
  const [search, setSearch] = useState("");
  const [filter, setFilter] = useState<FilterMode>("all");
  const [showCreate, setShowCreate] = useState(false);
  const [deleteId, setDeleteId] = useState<string | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [confirmBatch, setConfirmBatch] = useState(false);

  const FILTERS: { key: FilterMode; label: string }[] = useMemo(() => [
    { key: "all", label: t("creators.filter_all") },
    { key: "active", label: t("creators.filter_active") },
    { key: "inactive", label: t("creators.filter_inactive") },
    { key: "has_danbooru", label: t("creators.filter_danbooru") },
    { key: "has_subscription", label: t("creators.filter_subscribed") },
    { key: "no_subscription", label: t("creators.filter_no_sub") },
    { key: "favorites", label: t("creators.filter_favorites") },
  ], [t]);

  const creators = useQuery({ queryKey: queryKeys.creators.all, queryFn: () => api.listCreators() });
  const subs = useQuery({ queryKey: queryKeys.subscriptions.all, queryFn: () => api.listSubscriptions() });

  const create = useMutation({
    mutationFn: (data: { name: string; display_name?: string; description?: string }) => api.createCreator(data),
    onSuccess: () => { setShowCreate(false); creators.refetch(); },
  });

  const del = useMutation({
    mutationFn: (id: string) => api.deleteCreator(id),
    onSuccess: () => { setDeleteId(null); creators.refetch(); },
  });

  const batchDel = useMutation({
    mutationFn: (ids: string[]) => api.batchDeleteCreators(ids),
    onSuccess: () => { setSelected(new Set()); setConfirmBatch(false); creators.refetch(); subs.refetch(); },
  });

  const toggleFavorite = useMutation({
    mutationFn: (id: string) => api.toggleCreatorFavorite(id),
    onSuccess: () => creators.refetch(),
  });

  const subscriptionMap = useMemo(() => {
    const m = new Map<string, string>();
    subs.data?.forEach((s) => m.set(s.creator_id, s.id));
    return m;
  }, [subs.data]);

  const filtered = useMemo(() => {
    let list = creators.data || [];
    if (search) {
      const q = search.toLowerCase();
      list = list.filter((c) => c.name.toLowerCase().includes(q) || (c.display_name || "").toLowerCase().includes(q));
    }
    switch (filter) {
      case "active": list = list.filter((c) => c.is_active); break;
      case "inactive": list = list.filter((c) => !c.is_active); break;
      case "has_danbooru": list = list.filter((c: any) => c.danbooru_artist_id); break;
      case "has_subscription": list = list.filter((c) => subscriptionMap.has(c.id)); break;
      case "no_subscription": list = list.filter((c) => !subscriptionMap.has(c.id)); break;
      case "favorites": list = list.filter((c) => c.is_favorite); break;
    }
    return list;
  }, [creators.data, search, filter, subscriptionMap]);

  const toggleSelect = (id: string) => {
    const next = new Set(selected);
    next.has(id) ? next.delete(id) : next.add(id);
    setSelected(next);
  };
  const selectAll = () => {
    if (selected.size === filtered.length) setSelected(new Set());
    else setSelected(new Set(filtered.map((c) => c.id)));
  };

  return (
    <main className="max-w-6xl mx-auto p-6">
      <PageHeader title={t("creators.title")} description={t("creators.count").replace("{filtered}", String(filtered.length)).replace("{total}", String(creators.data?.length || 0))}>
        <div className="flex gap-2">
          <button onClick={() => router.push("/admin/creators/duplicates")} className="px-4 py-2 border rounded text-sm hover:bg-gray-50 dark:hover:bg-slate-700 dark:text-gray-300 dark:border-slate-600">{t("creators.duplicates")}</button>
          <button onClick={() => setShowCreate(true)} className="px-4 py-2 bg-slate-900 dark:bg-slate-700 text-white rounded text-sm hover:bg-slate-800 dark:hover:bg-slate-600">{t("creators.new")}</button>
        </div>
      </PageHeader>

      {/* Toolbar */}
      <div className="flex items-center gap-3 mb-4 flex-wrap">
        <input value={search} onChange={(e) => setSearch(e.target.value)} placeholder={t("creators.search")} className="border rounded px-3 py-2 text-sm w-48 dark:bg-slate-700 dark:text-white dark:border-slate-600" />
        <div className="flex gap-1 bg-gray-100 dark:bg-slate-700 rounded p-0.5">
          {FILTERS.map((f) => (
            <button key={f.key} onClick={() => setFilter(f.key)}
              className={`px-3 py-1 text-xs rounded transition-colors ${filter === f.key ? "bg-white dark:bg-slate-600 shadow-sm font-medium" : "text-gray-500 hover:text-gray-700 dark:text-gray-400"}`}>
              {f.label}
            </button>
          ))}
        </div>
        <div className="flex-1" />
        {selected.size > 0 && (
          <button onClick={() => setConfirmBatch(true)} className="px-3 py-1.5 text-xs bg-red-600 text-white rounded hover:bg-red-700">
            {t("creators.delete_selected").replace("{count}", String(selected.size))}
          </button>
        )}
      </div>

      {/* Select all */}
      {filtered.length > 0 && (
        <label className="flex items-center gap-2 mb-2 text-xs text-gray-500 dark:text-gray-400 cursor-pointer">
          <input type="checkbox" checked={selected.size === filtered.length && filtered.length > 0} onChange={selectAll} className="rounded" />
          {t("creators.select_all")}
        </label>
      )}

      {/* Content */}
      {creators.isLoading && <div className="space-y-2">{Array.from({ length: 5 }).map((_, i) => <div key={i} className="h-16 bg-gray-100 dark:bg-slate-700 rounded animate-pulse" />)}</div>}
      {creators.error && <ErrorState message={(creators.error as Error).message} onRetry={() => creators.refetch()} />}
      {creators.data && !creators.data.length && <EmptyState title={t("creators.no_creators")} description={t("creators.no_creators_desc")} action={<button onClick={() => setShowCreate(true)} className="px-4 py-2 bg-slate-900 dark:bg-slate-700 text-white rounded text-sm">{t("creators.create_creator")}</button>} />}

      {filtered.length > 0 && (
        <div className="space-y-1">
          {filtered.map((c) => (
            <div key={c.id} className={`bg-white dark:bg-slate-800 rounded-lg shadow-sm p-3 flex items-center gap-3 transition-colors ${selected.has(c.id) ? "ring-2 ring-blue-500" : ""}`}>
              <input type="checkbox" checked={selected.has(c.id)} onChange={() => toggleSelect(c.id)} className="rounded shrink-0" onClick={(e) => e.stopPropagation()} />
              <div className="flex-1 min-w-0 cursor-pointer" onClick={() => router.push(`/admin/creators/${c.id}`)}>
                <div className="flex items-center gap-2">
                  <span className="font-medium text-sm truncate">{c.display_name || c.name}</span>
                  {c.display_name && <span className="text-xs text-gray-400 dark:text-gray-500 truncate">{c.name}</span>}
                  {c.is_active ? <span className="w-1.5 h-1.5 bg-green-500 rounded-full shrink-0" /> : <span className="w-1.5 h-1.5 bg-gray-300 rounded-full shrink-0" />}
                </div>
                {c.description && <p className="text-xs text-gray-500 dark:text-gray-400 mt-0.5 line-clamp-1">{c.description}</p>}
              </div>
              <div className="flex items-center gap-2 shrink-0 text-xs">
                {(c as any).danbooru_artist_id && <span className="px-1.5 py-0.5 bg-purple-100 dark:bg-purple-900/30 text-purple-700 dark:text-purple-400 rounded font-mono text-[10px]">D#{String((c as any).danbooru_artist_id)}</span>}
                {subscriptionMap.has(c.id) && <span className="px-1.5 py-0.5 bg-green-100 dark:bg-green-900/30 text-green-700 dark:text-green-400 rounded text-[10px]">{t("creators.sub_badge")}</span>}
                <button onClick={(e) => { e.stopPropagation(); toggleFavorite.mutate(c.id); }}
                  className={`text-lg ${c.is_favorite ? "text-yellow-500" : "text-gray-300 dark:text-gray-600 hover:text-yellow-400"}`}
                  title={c.is_favorite ? t("common.unfavorite") : t("common.favorite")}>
                  {c.is_favorite ? "★" : "☆"}
                </button>
                <button onClick={() => router.push(`/admin/creators/${c.id}`)} className="text-blue-600 hover:underline">{t("creators.view")}</button>
                <button onClick={(e) => { e.stopPropagation(); setDeleteId(c.id); }} className="text-red-500 hover:text-red-700 dark:text-red-400">{t("creators.del")}</button>
              </div>
            </div>
          ))}
        </div>
      )}

      <Modal open={showCreate} onClose={() => setShowCreate(false)} title={t("creators.new_creator_title")}>
        <CreateForm isPending={create.isPending} error={create.error} onSubmit={(data) => create.mutate(data)} onClose={() => setShowCreate(false)} />
      </Modal>
      {deleteId && <ConfirmDialog open title={t("creators.delete_title")} message={t("creators.delete_msg")} onConfirm={() => del.mutate(deleteId)} onCancel={() => setDeleteId(null)} isPending={del.isPending} error={(del.error as Error)?.message} />}
      {confirmBatch && <ConfirmDialog open title={t("creators.batch_delete_title")} message={t("creators.batch_delete_msg").replace("{count}", String(selected.size))} onConfirm={() => batchDel.mutate([...selected])} onCancel={() => setConfirmBatch(false)} isPending={batchDel.isPending} error={(batchDel.error as Error)?.message} />}
    </main>
  );
}

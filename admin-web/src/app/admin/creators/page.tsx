"use client";
import { useState, useMemo, useEffect, Suspense } from "react";
import { useQuery, useMutation } from "@tanstack/react-query";
import { api, queryKeys } from "@/lib/api";
import { PageHeader, EmptyState, ErrorState, ConfirmDialog, Modal } from "@/components";
import { useRouter, useSearchParams, usePathname } from "next/navigation";
import { useT } from "@/lib/i18n";
import { useToast } from "@/components/Toast";

type FilterMode = "all" | "active" | "inactive" | "has_danbooru" | "has_subscription" | "no_subscription" | "favorites";

function fmtLastSync(value?: string) {
  if (!value) return "never synced";
  const d = new Date(value);
  return Number.isNaN(d.getTime()) ? "sync unknown" : `last sync ${d.toLocaleDateString()}`;
}

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
          className="w-full border rounded px-3 py-2 text-sm dark:bg-slate-700 dark:text-white font-mono"
          placeholder="https://www.pixiv.net/users/123456 或 https://x.com/username" />
        <p className="text-xs text-gray-400 mt-1">粘贴 URL 自动提取创作者名。支持 Pixiv / X / Iwara / Danbooru / Weibo / Lofter / Bilibili。</p>
      </div>
      <div className="grid grid-cols-2 gap-4">
        <div>
          <label className="block text-sm font-medium mb-1">{t("creators.name_label")} <span className="text-red-400">*</span></label>
          <input value={name} onChange={(e) => setName(e.target.value)} className="w-full border rounded px-3 py-2 text-sm dark:bg-slate-700 dark:text-white" placeholder={t("creators.name_placeholder")} />
        </div>
        <div>
          <label className="block text-sm font-medium mb-1">{t("creators.display_name_label")}</label>
          <input value={displayName} onChange={(e) => setDisplayName(e.target.value)} className="w-full border rounded px-3 py-2 text-sm dark:bg-slate-700 dark:text-white" placeholder="可选显示名" />
        </div>
      </div>
      <div><label className="block text-sm font-medium mb-1">{t("creators.description_label")}</label><textarea value={description} onChange={(e) => setDescription(e.target.value)} className="w-full border rounded px-3 py-2 text-sm dark:bg-slate-700 dark:text-white" rows={2} /></div>
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
  const toast = useToast();
  const router = useRouter();
  const sp = useSearchParams();
  const pathname = usePathname();

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

  const creatorCount = useQuery({ queryKey: ["creators-count"], queryFn: () => api.countCreators() });
  const creators = useQuery({
    queryKey: [...queryKeys.creators.all, page, limit, filters],
    queryFn: () => api.listCreators(page * limit, limit, filters as any),
  });

  const create = useMutation({
    mutationFn: (data: { name: string; display_name?: string; description?: string }) => api.createCreator(data),
    onSuccess: () => { setShowCreate(false); creators.refetch(); toast.success({ message: t("notification.created") }); },
    onError: (e: Error) => toast.error({ message: e.message }),
  });

  const del = useMutation({
    mutationFn: (id: string) => api.deleteCreator(id),
    onSuccess: () => { setDeleteId(null); creators.refetch(); toast.success({ message: t("notification.deleted") }); },
  });

  const batchDel = useMutation({
    mutationFn: (ids: string[]) => api.batchDeleteCreators(ids),
    onSuccess: () => { setSelected(new Set()); setConfirmBatchDel(false); creators.refetch(); toast.success({ message: t("notification.deleted") }); },
  });

  const toggleSelect = (id: string) => {
    const next = new Set(selected);
    next.has(id) ? next.delete(id) : next.add(id);
    setSelected(next);
  };
  const selectAll = () => {
    if (selected.size === (creators.data?.length || 0)) setSelected(new Set());
    else setSelected(new Set((creators.data || []).map((c) => c.id)));
  };

  const toggleFavorite = useMutation({
    mutationFn: (id: string) => api.toggleCreatorFavorite(id),
    onSuccess: () => creators.refetch(),
  });

  const handleFilterChange = (mode: FilterMode) => {
    updateParams({ filter: mode === "all" ? null : mode });
  };

  const handleSearchChange = (value: string) => {
    setInputVal(value);
  };

  return (
    <main className="max-w-6xl mx-auto p-6">
      <PageHeader title={t("creators.title")} description={t("creators.count", "0 creators").replace("{count}", String(creatorCount.data?.count ?? 0))}>
        <div className="flex gap-2">
          <button onClick={() => router.push("/admin/creators/duplicates")} className="btn-ghost">{t("creators.duplicates")}</button>
          <button onClick={() => setShowCreate(true)} className="btn-primary">{t("creators.new")}</button>
        </div>
      </PageHeader>

      {/* Toolbar */}
      <div className="flex items-center gap-3 mb-4 flex-wrap">
        <input value={inputVal} onChange={(e) => handleSearchChange(e.target.value)} placeholder={t("creators.search")} className="w-56 rounded-md border border-[#d8dee4] px-3 py-1.5 text-sm dark:border-[#30363d] dark:bg-[#0d1117] dark:text-white" />
        <div className="flex gap-1 rounded-md border border-[#d8dee4] bg-[#f6f8fa] p-0.5 dark:border-[#30363d] dark:bg-[#161b22]">
          {FILTERS.map((f) => (
            <button key={f.key} onClick={() => handleFilterChange(f.key)}
              className={`rounded px-3 py-1 text-xs transition-colors ${filter === f.key ? "bg-white font-medium shadow-sm dark:bg-[#30363d]" : "text-[#57606a] hover:text-[#24292f] dark:text-[#8b949e] dark:hover:text-[#e6edf3]"}`}>
              {f.label}
            </button>
          ))}
        </div>
        <div className="flex-1" />
      </div>

      {selected.size > 0 && (
        <div className="sticky top-2 z-20 mb-4 flex items-center gap-3 rounded-md border border-[#d8dee4] bg-white px-4 py-2 shadow-sm dark:border-[#30363d] dark:bg-[#161b22]">
          <span className="text-sm font-medium">{selected.size} selected</span>
          <button onClick={() => setSelected(new Set())} className="btn-ghost">Clear</button>
          <span className="flex-1" />
          <button onClick={() => setConfirmBatchDel(true)} className="rounded-md border border-[#cf222e]/40 px-3 py-1.5 text-sm font-medium text-[#cf222e] hover:bg-[#ffebe9] dark:text-[#f85149] dark:hover:bg-[#f8514926]">
            {t("creators.delete_selected").replace("{count}", String(selected.size))}
          </button>
        </div>
      )}

      {/* Select all */}
      {creators.data && creators.data.length > 0 && (
        <label className="flex items-center gap-2 mb-2 text-xs text-gray-500 dark:text-gray-400 cursor-pointer">
          <input type="checkbox" checked={selected.size === creators.data.length && creators.data.length > 0} onChange={selectAll} className="rounded" />
          {t("creators.select_all")}
        </label>
      )}

      {/* Content */}
      {creators.isLoading && <div className="space-y-2">{Array.from({ length: 5 }).map((_, i) => <div key={i} className="h-16 bg-gray-100 dark:bg-slate-700 rounded animate-pulse" />)}</div>}
      {creators.error && <ErrorState message={(creators.error as Error).message} onRetry={() => creators.refetch()} />}
      {creators.data && !creators.data.length && (
        <EmptyState
          title={search || filter !== "all" ? t("works.no_works_filter") : t("creators.no_creators")}
          description={search || filter !== "all" ? undefined : t("creators.no_creators_desc")}
          action={(!search && filter === "all") ? <button onClick={() => setShowCreate(true)} className="px-4 py-2 bg-slate-900 dark:bg-slate-700 text-white rounded text-sm">{t("creators.create_creator")}</button> : undefined}
        />
      )}

      {creators.data && creators.data.length > 0 && (
        <div className="overflow-hidden rounded-md border border-[#d8dee4] bg-white dark:border-[#30363d] dark:bg-[#161b22]">
          {creators.data.map((c) => (
            <div key={c.id} className={`flex cursor-pointer items-center gap-3 border-b border-[#d8dee4] p-4 last:border-b-0 hover:bg-[#f6f8fa] dark:border-[#30363d] dark:hover:bg-[#21262d] ${selected.has(c.id) ? "bg-[#ddf4ff] dark:bg-[#1f6feb26]" : ""}`} onClick={() => router.push(`/admin/creators/${c.id}`)}>
              <input type="checkbox" checked={selected.has(c.id)} onChange={() => toggleSelect(c.id)} className="rounded shrink-0" onClick={(e) => e.stopPropagation()} />
              <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-full border border-[#d8dee4] bg-[#0969da] text-sm font-semibold text-white dark:border-[#30363d]">
                {(c.display_name || c.name).slice(0, 2).toUpperCase()}
              </div>
              <div className="flex-1 min-w-0">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="truncate text-sm font-semibold text-[#0969da] dark:text-[#58a6ff]">{c.display_name || c.name}</span>
                  {c.display_name && <span className="truncate font-mono text-xs text-[#57606a] dark:text-[#8b949e]">{c.name}</span>}
                  {c.is_active ? <span className="w-1.5 h-1.5 bg-green-500 rounded-full shrink-0" /> : <span className="w-1.5 h-1.5 bg-gray-300 rounded-full shrink-0" />}
                </div>
                {c.description && <p className="mt-1 line-clamp-1 text-xs text-[#57606a] dark:text-[#8b949e]">{c.description}</p>}
                <div className="mt-2 flex flex-wrap items-center gap-3 text-xs text-[#57606a] dark:text-[#8b949e]">
                  <span>{c.repository_count ?? 0} repositories</span>
                  <span>{c.source_count ?? 0} sources</span>
                  <span>{fmtLastSync(c.last_synced_at)}</span>
                </div>
              </div>
              <div className="flex items-center gap-2 shrink-0 text-xs" onClick={(e) => e.stopPropagation()}>
                {(c as any).danbooru_artist_id && <span className="rounded-full bg-purple-100 px-2 py-0.5 font-mono text-[10px] text-purple-700 dark:bg-purple-900/30 dark:text-purple-400">D#{String((c as any).danbooru_artist_id)}</span>}
                {(c.subscription_count ?? 0) > 0 && <span className="rounded-full bg-[#dafbe1] px-2 py-0.5 text-[10px] text-[#1a7f37] dark:bg-[#2ea04326] dark:text-[#3fb950]">{t("creators.sub_badge")}</span>}
                <button onClick={(e) => { e.stopPropagation(); toggleFavorite.mutate(c.id); }}
                  className={`text-lg ${c.is_favorite ? "text-yellow-500" : "text-gray-300 dark:text-gray-600 hover:text-yellow-400"}`}
                  title={c.is_favorite ? t("common.unfavorite") : t("common.favorite")}>
                  {c.is_favorite ? "★" : "☆"}
                </button>
                <button onClick={(e) => { e.stopPropagation(); setDeleteId(c.id); }} className="text-[#cf222e] hover:underline dark:text-[#f85149]">{t("creators.del")}</button>
              </div>
            </div>
          ))}
        </div>
      )}

      {/* Pagination */}
      {(creators.data?.length || 0) > 0 && (
        <div className="flex gap-2 justify-center mt-4">
          <button disabled={page === 0} onClick={() => updateParams({ p: page <= 1 ? null : String(page - 1) }, false)} className="px-3 py-1 text-sm border rounded disabled:opacity-30 dark:border-slate-600 dark:text-gray-300">{t("common.prev")}</button>
          <span className="px-3 py-1 text-sm text-gray-500 dark:text-gray-400">{t("common.page").replace("{page}", String(page + 1))}</span>
          <button onClick={() => updateParams({ p: String(page + 1) }, false)} disabled={!creators.data || creators.data.length < limit} className="px-3 py-1 text-sm border rounded disabled:opacity-30 dark:border-slate-600 dark:text-gray-300">{t("common.next")}</button>
        </div>
      )}

      <Modal open={showCreate} onClose={() => setShowCreate(false)} title={t("creators.new_creator_title")}>
        <CreateForm isPending={create.isPending} error={create.error} onSubmit={(data) => create.mutate(data)} onClose={() => setShowCreate(false)} />
      </Modal>
      {deleteId && <ConfirmDialog open title={t("creators.delete_title")} message={t("creators.delete_msg")} onConfirm={() => del.mutate(deleteId)} onCancel={() => setDeleteId(null)} isPending={del.isPending} error={(del.error as Error)?.message} />}
      {confirmBatchDel && <ConfirmDialog open title={t("creators.batch_delete_title")} message={t("creators.batch_delete_msg").replace("{count}", String(selected.size))} onConfirm={() => batchDel.mutate([...selected])} onCancel={() => setConfirmBatchDel(false)} isPending={batchDel.isPending} error={(batchDel.error as Error)?.message} />}
    </main>
  );
}

export default function CreatorsPage() {
  return (
    <Suspense>
      <CreatorsContent />
    </Suspense>
  );
}

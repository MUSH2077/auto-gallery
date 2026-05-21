"use client";
import { useState, useMemo } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api, queryKeys, Tag } from "@/lib/api";
import { useT } from "@/lib/i18n";
import { PageHeader, EmptyState, ErrorState, Modal, ConfirmDialog } from "@/components";

const CATEGORIES = ["general", "artist", "series", "character", "meta"];

function hashStr(s: string): number {
  let h = 0;
  for (let i = 0; i < s.length; i++) {
    h = ((h << 5) - h) + s.charCodeAt(i);
    h |= 0;
  }
  return Math.abs(h);
}

function bubbleStyle(tag: Tag, minCount: number, maxCount: number) {
  const range = maxCount > minCount ? maxCount - minCount : 1;
  const count = Math.max(tag.usage_count, minCount);
  const logMin = Math.log(minCount || 1);
  const logMax = Math.log(maxCount || 1);
  const logRange = logMax > logMin ? logMax - logMin : 1;
  const ratio = (Math.log(count) - logMin) / logRange;
  const fontSize = 0.75 + ratio * 1.75; // 0.75rem to 2.5rem
  const paddingX = 0.5 + ratio * 1.0;   // 0.5rem to 1.5rem
  const paddingY = 0.25 + ratio * 0.5;   // 0.25rem to 0.75rem

  const hue = hashStr(tag.category || tag.normalized_name) % 360;
  return {
    fontSize: `${fontSize.toFixed(2)}rem`,
    padding: `${paddingY.toFixed(2)}rem ${paddingX.toFixed(2)}rem`,
    backgroundColor: `hsl(${hue}, 55%, 92%)`,
    color: `hsl(${hue}, 40%, 25%)`,
    borderColor: `hsl(${hue}, 40%, 82%)`,
  };
}

function bubbleStyleDark(tag: Tag, minCount: number, maxCount: number) {
  const range = maxCount > minCount ? maxCount - minCount : 1;
  const count = Math.max(tag.usage_count, minCount);
  const logMin = Math.log(minCount || 1);
  const logMax = Math.log(maxCount || 1);
  const logRange = logMax > logMin ? logMax - logMin : 1;
  const ratio = (Math.log(count) - logMin) / logRange;
  const fontSize = 0.75 + ratio * 1.75;
  const paddingX = 0.5 + ratio * 1.0;
  const paddingY = 0.25 + ratio * 0.5;

  const hue = hashStr(tag.category || tag.normalized_name) % 360;
  return {
    fontSize: `${fontSize.toFixed(2)}rem`,
    padding: `${paddingY.toFixed(2)}rem ${paddingX.toFixed(2)}rem`,
    backgroundColor: `hsl(${hue}, 30%, 22%)`,
    color: `hsl(${hue}, 45%, 78%)`,
    borderColor: `hsl(${hue}, 25%, 32%)`,
  };
}

export default function TagsPage() {
  const t = useT();
  const qc = useQueryClient();
  const [search, setSearch] = useState("");
  const [showCreate, setShowCreate] = useState(false);
  const [editId, setEditId] = useState<string | null>(null);
  const [deleteId, setDeleteId] = useState<string | null>(null);
  const [formName, setFormName] = useState("");
  const [formCat, setFormCat] = useState("general");

  const tags = useQuery({ queryKey: queryKeys.tags.all, queryFn: () => api.listTags() });

  const create = useMutation({
    mutationFn: () => api.createTag({ normalized_name: formName.trim().toLowerCase(), category: formCat || undefined }),
    onSuccess: () => { qc.invalidateQueries({ queryKey: queryKeys.tags.all }); setShowCreate(false); setFormName(""); setFormCat("general"); },
  });

  const update = useMutation({
    mutationFn: () => api.updateTag(editId!, { normalized_name: formName.trim().toLowerCase() || undefined, category: formCat || undefined }),
    onSuccess: () => { qc.invalidateQueries({ queryKey: queryKeys.tags.all }); setEditId(null); },
  });

  const deleteTag = useMutation({
    mutationFn: () => api.deleteTag(deleteId!),
    onSuccess: () => { qc.invalidateQueries({ queryKey: queryKeys.tags.all }); setDeleteId(null); },
  });

  const openEdit = (tag: Tag) => {
    setEditId(tag.id);
    setFormName(tag.normalized_name);
    setFormCat(tag.category || "general");
  };

  const filtered = tags.data?.filter((t: Tag) =>
    !search || t.normalized_name.includes(search.toLowerCase())
  ) || [];

  const { minCount, maxCount } = useMemo(() => {
    if (!filtered.length) return { minCount: 0, maxCount: 0 };
    const counts = filtered.map(t => t.usage_count);
    return { minCount: Math.min(...counts), maxCount: Math.max(...counts) };
  }, [filtered]);

  return (
    <main className="max-w-5xl mx-auto p-6">
      <PageHeader title={t("tags.title")} description={t("tags.desc")}>
        <button onClick={() => { setFormName(""); setFormCat("general"); setShowCreate(true); }}
          className="px-4 py-2 bg-slate-900 dark:bg-slate-700 text-white rounded text-sm hover:bg-slate-800 dark:hover:bg-slate-600">{t("tags.new")}</button>
      </PageHeader>

      <div className="mb-6">
        <input value={search} onChange={(e) => setSearch(e.target.value)}
          placeholder={t("tags.search")} className="w-full max-w-xs border rounded px-3 py-2 text-sm dark:bg-slate-700 dark:text-white dark:border-slate-600" />
      </div>

      {tags.isLoading && (
        <div className="flex flex-wrap gap-2 items-center">
          {Array.from({ length: 20 }).map((_, i) => {
            const w = 60 + Math.random() * 120;
            const h = 24 + Math.random() * 24;
            return <div key={i} style={{ width: `${w}px`, height: `${h}px` }}
              className="bg-gray-100 dark:bg-slate-700 rounded-full animate-pulse" />;
          })}
        </div>
      )}
      {tags.error && <ErrorState message={(tags.error as Error).message} onRetry={() => tags.refetch()} />}
      {tags.data && !tags.data.length && <EmptyState title={t("tags.no_tags")} description={t("tags.no_tags_desc")} />}

      {tags.data && tags.data.length > 0 && (
        <div className="bg-white dark:bg-slate-800 rounded-lg shadow p-6">
          <div className="flex flex-wrap gap-2 items-center justify-center">
            {filtered.map((tag) => {
              const light = bubbleStyle(tag, minCount, maxCount);
              const dark = bubbleStyleDark(tag, minCount, maxCount);
              return (
                <div key={tag.id}
                  className="group inline-flex items-center gap-1 rounded-full border cursor-pointer hover:shadow-md transition-shadow"
                  style={{
                    fontSize: light.fontSize,
                    padding: light.padding,
                    backgroundColor: light.backgroundColor,
                    color: light.color,
                    borderColor: light.borderColor,
                  }}
                  onClick={() => openEdit(tag)}>
                  <span className="font-semibold truncate max-w-[16rem]">{tag.normalized_name}</span>
                  {tag.category && (
                    <span className="opacity-60" style={{ fontSize: `calc(${light.fontSize} * 0.7)` }}>
                      {tag.category}
                    </span>
                  )}
                  <span className="opacity-40 ml-0.5" style={{ fontSize: `calc(${light.fontSize} * 0.65)` }}>
                    {tag.usage_count}
                  </span>
                  <button onClick={(e) => { e.stopPropagation(); setDeleteId(tag.id); }}
                    className="ml-1 opacity-0 group-hover:opacity-100 hover:text-red-500 transition-opacity text-lg leading-none">&times;</button>
                </div>
              );
            })}
          </div>
          {search && !filtered.length && <p className="text-sm text-gray-400 dark:text-gray-500 mt-4 text-center">{t("tags.no_match").replace("{query}", search)}</p>}
          <p className="text-xs text-gray-400 dark:text-gray-500 mt-5 text-center">
            {search
              ? t("tags.matching").replace("{count}", String(filtered.length))
              : t("tags.total").replace("{count}", String(filtered.length))}
          </p>
        </div>
      )}

      <Modal open={showCreate} onClose={() => setShowCreate(false)} title={t("tags.create_title")}>
        <div className="space-y-4">
          <div><label className="block text-sm font-medium mb-1">{t("tags.name_label")}</label>
            <input value={formName} onChange={(e) => setFormName(e.target.value)}
              className="w-full border rounded px-3 py-2 text-sm dark:bg-slate-700 dark:text-white" placeholder={t("tags.name_placeholder")} /></div>
          <div><label className="block text-sm font-medium mb-1">{t("tags.category_label")}</label>
            <select value={formCat} onChange={(e) => setFormCat(e.target.value)} className="w-full border rounded px-3 py-2 text-sm dark:bg-slate-700 dark:text-white">
              {CATEGORIES.map((c) => <option key={c} value={c}>{c}</option>)}
            </select></div>
          <div className="flex justify-end gap-3 pt-2">
            <button onClick={() => setShowCreate(false)} className="px-4 py-2 text-sm border rounded hover:bg-gray-50 dark:hover:bg-slate-700 dark:text-gray-300">{t("tags.cancel")}</button>
            <button onClick={() => create.mutate()} disabled={!formName.trim() || create.isPending}
              className="px-4 py-2 text-sm bg-slate-900 dark:bg-slate-700 text-white rounded hover:bg-slate-800 dark:hover:bg-slate-600 disabled:opacity-50">
              {create.isPending ? t("tags.creating") : t("tags.create")}
            </button>
          </div>
          {create.error && <p className="text-red-600 text-sm">{(create.error as Error).message}</p>}
        </div>
      </Modal>

      <Modal open={!!editId} onClose={() => setEditId(null)} title={t("tags.edit_title")}>
        <div className="space-y-4">
          <div><label className="block text-sm font-medium mb-1">{t("tags.name_label")}</label>
            <input value={formName} onChange={(e) => setFormName(e.target.value)}
              className="w-full border rounded px-3 py-2 text-sm dark:bg-slate-700 dark:text-white" /></div>
          <div><label className="block text-sm font-medium mb-1">{t("tags.category_label")}</label>
            <select value={formCat} onChange={(e) => setFormCat(e.target.value)} className="w-full border rounded px-3 py-2 text-sm dark:bg-slate-700 dark:text-white">
              {CATEGORIES.map((c) => <option key={c} value={c}>{c}</option>)}
            </select></div>
          <div className="flex justify-end gap-3 pt-2">
            <button onClick={() => setEditId(null)} className="px-4 py-2 text-sm border rounded hover:bg-gray-50 dark:hover:bg-slate-700 dark:text-gray-300">{t("tags.cancel")}</button>
            <button onClick={() => update.mutate()} disabled={!formName.trim() || update.isPending}
              className="px-4 py-2 text-sm bg-slate-900 dark:bg-slate-700 text-white rounded hover:bg-slate-800 dark:hover:bg-slate-600 disabled:opacity-50">
              {update.isPending ? t("tags.saving") : t("tags.save")}
            </button>
          </div>
          {update.error && <p className="text-red-600 text-sm">{(update.error as Error).message}</p>}
        </div>
      </Modal>

      {deleteId && <ConfirmDialog open title={t("tags.delete_title")} message={t("tags.delete_msg")} onConfirm={() => deleteTag.mutate()} onCancel={() => setDeleteId(null)} isPending={deleteTag.isPending} error={(deleteTag.error as Error)?.message} />}
    </main>
  );
}

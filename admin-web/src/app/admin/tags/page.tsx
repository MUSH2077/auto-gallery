"use client";
import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api, queryKeys, Tag } from "@/lib/api";
import { PageHeader, EmptyState, ErrorState, Modal, ConfirmDialog } from "@/components";

const CATEGORIES = ["general", "artist", "series", "character", "meta"];

export default function TagsPage() {
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

  return (
    <main className="max-w-4xl mx-auto p-6">
      <PageHeader title="Tags" description={`Manage normalized tags across all sources`}>
        <button onClick={() => { setFormName(""); setFormCat("general"); setShowCreate(true); }}
          className="px-4 py-2 bg-slate-900 text-white rounded text-sm hover:bg-slate-800">+ New Tag</button>
      </PageHeader>

      <div className="mb-4">
        <input value={search} onChange={(e) => setSearch(e.target.value)}
          placeholder="Search tags..." className="w-full max-w-xs border rounded px-3 py-2 text-sm" />
      </div>

      {tags.isLoading && <div className="flex flex-wrap gap-2">{Array.from({ length: 20 }).map((_, i) => <div key={i} className="h-8 w-24 bg-gray-100 rounded-full animate-pulse" />)}</div>}
      {tags.error && <ErrorState message={(tags.error as Error).message} onRetry={() => tags.refetch()} />}
      {tags.data && !tags.data.length && <EmptyState title="No tags" description="Tags are created automatically during import. Create one manually to get started." />}

      {tags.data && tags.data.length > 0 && (
        <div className="bg-white rounded-lg shadow p-4">
          <div className="flex flex-wrap gap-2">
            {filtered.map((tag) => (
              <div key={tag.id}
                className="group flex items-center gap-1 px-3 py-1 bg-gray-100 rounded-full text-sm hover:bg-gray-200 cursor-pointer"
                onClick={() => openEdit(tag)}>
                <span className="font-medium">{tag.normalized_name}</span>
                {tag.category && <span className="text-xs text-gray-400">({tag.category})</span>}
                <button onClick={(e) => { e.stopPropagation(); setDeleteId(tag.id); }}
                  className="ml-1 opacity-0 group-hover:opacity-100 text-red-400 hover:text-red-600 text-xs">&times;</button>
              </div>
            ))}
          </div>
          {search && !filtered.length && <p className="text-sm text-gray-400 mt-3">No tags matching "{search}"</p>}
          <p className="text-xs text-gray-400 mt-4">{filtered.length} tag{filtered.length !== 1 ? "s" : ""}{search ? " matching" : " total"}</p>
        </div>
      )}

      <Modal open={showCreate} onClose={() => setShowCreate(false)} title="Create Tag">
        <div className="space-y-4">
          <div><label className="block text-sm font-medium mb-1">Name *</label>
            <input value={formName} onChange={(e) => setFormName(e.target.value)}
              className="w-full border rounded px-3 py-2 text-sm" placeholder="lowercase_tag_name" /></div>
          <div><label className="block text-sm font-medium mb-1">Category</label>
            <select value={formCat} onChange={(e) => setFormCat(e.target.value)} className="w-full border rounded px-3 py-2 text-sm">
              {CATEGORIES.map((c) => <option key={c} value={c}>{c}</option>)}
            </select></div>
          <div className="flex justify-end gap-3 pt-2">
            <button onClick={() => setShowCreate(false)} className="px-4 py-2 text-sm border rounded hover:bg-gray-50">Cancel</button>
            <button onClick={() => create.mutate()} disabled={!formName.trim() || create.isPending}
              className="px-4 py-2 text-sm bg-slate-900 text-white rounded hover:bg-slate-800 disabled:opacity-50">
              {create.isPending ? "Creating..." : "Create Tag"}
            </button>
          </div>
          {create.error && <p className="text-red-600 text-sm">{(create.error as Error).message}</p>}
        </div>
      </Modal>

      <Modal open={!!editId} onClose={() => setEditId(null)} title="Edit Tag">
        <div className="space-y-4">
          <div><label className="block text-sm font-medium mb-1">Name</label>
            <input value={formName} onChange={(e) => setFormName(e.target.value)}
              className="w-full border rounded px-3 py-2 text-sm" /></div>
          <div><label className="block text-sm font-medium mb-1">Category</label>
            <select value={formCat} onChange={(e) => setFormCat(e.target.value)} className="w-full border rounded px-3 py-2 text-sm">
              {CATEGORIES.map((c) => <option key={c} value={c}>{c}</option>)}
            </select></div>
          <div className="flex justify-end gap-3 pt-2">
            <button onClick={() => setEditId(null)} className="px-4 py-2 text-sm border rounded hover:bg-gray-50">Cancel</button>
            <button onClick={() => update.mutate()} disabled={!formName.trim() || update.isPending}
              className="px-4 py-2 text-sm bg-slate-900 text-white rounded hover:bg-slate-800 disabled:opacity-50">
              {update.isPending ? "Saving..." : "Save"}
            </button>
          </div>
          {update.error && <p className="text-red-600 text-sm">{(update.error as Error).message}</p>}
        </div>
      </Modal>

      {deleteId && <ConfirmDialog open title="Delete Tag" message="Delete this tag? It will be removed from all associated works." onConfirm={() => deleteTag.mutate()} onCancel={() => setDeleteId(null)} />}
    </main>
  );
}

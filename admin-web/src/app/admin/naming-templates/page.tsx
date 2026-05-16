"use client";
import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { PageHeader, EmptyState, ErrorState, Modal, ConfirmDialog } from "@/components";

function TemplateForm({ template, onClose, onSave, isPending }: {
  template?: { id?: string; name: string; source?: string; template: string; is_default: boolean };
  onClose: () => void; onSave: (d: Record<string, unknown>) => void; isPending: boolean;
}) {
  const [name, setName] = useState(template?.name || "");
  const [source, setSource] = useState(template?.source || "");
  const [tpl, setTpl] = useState(template?.template || "");
  const [isDefault, setIsDefault] = useState(template?.is_default || false);
  return (
    <div className="space-y-4">
      <div><label className="block text-sm font-medium mb-1">Name *</label><input value={name} onChange={(e) => setName(e.target.value)} className="w-full border rounded px-3 py-2 text-sm" placeholder="Pixiv Default" /></div>
      <div><label className="block text-sm font-medium mb-1">Source</label><input value={source} onChange={(e) => setSource(e.target.value)} className="w-full border rounded px-3 py-2 text-sm" placeholder="pixiv" /></div>
      <div><label className="block text-sm font-medium mb-1">Template *</label><textarea value={tpl} onChange={(e) => setTpl(e.target.value)} rows={3} className="w-full border rounded px-3 py-2 text-sm font-mono" placeholder="pixiv/{user[account]}/{id}" /></div>
      <div className="flex items-center gap-2"><input type="checkbox" checked={isDefault} onChange={(e) => setIsDefault(e.target.checked)} /><span className="text-sm">Set as default for this source</span></div>
      <div className="flex justify-end gap-3 pt-2">
        <button onClick={onClose} className="px-4 py-2 text-sm border rounded hover:bg-gray-50 dark:hover:bg-slate-700 dark:bg-slate-800/50">Cancel</button>
        <button onClick={() => onSave({ name, source: source || undefined, template: tpl, is_default: isDefault })} disabled={!name || !tpl || isPending} className="px-4 py-2 text-sm bg-slate-900 dark:bg-slate-700 text-white rounded hover:bg-slate-800 dark:hover:bg-slate-600 disabled:opacity-50">{isPending ? "Saving..." : "Save"}</button>
      </div>
    </div>
  );
}

export default function NamingTemplatesPage() {
  const qc = useQueryClient();
  const [showCreate, setShowCreate] = useState(false);
  const [editTpl, setEditTpl] = useState<Record<string, unknown> | null>(null);
  const [deleteId, setDeleteId] = useState<string | null>(null);
  const templates = useQuery({ queryKey: ["naming-templates"], queryFn: api.listNamingTemplates });
  const create = useMutation({ mutationFn: (d: Record<string, unknown>) => api.createNamingTemplate(d as any), onSuccess: () => { qc.invalidateQueries({ queryKey: ["naming-templates"] }); setShowCreate(false); } });
  const update = useMutation({ mutationFn: (d: Record<string, unknown>) => api.updateNamingTemplate(editTpl!.id as string, d), onSuccess: () => { qc.invalidateQueries({ queryKey: ["naming-templates"] }); setEditTpl(null); } });
  const del = useMutation({ mutationFn: (id: string) => api.deleteNamingTemplate(id), onSuccess: () => { qc.invalidateQueries({ queryKey: ["naming-templates"] }); setDeleteId(null); } });
  const tpls = templates.data || [];

  return (
    <main className="max-w-4xl mx-auto p-6">
      <PageHeader title="Naming Templates" description="Configure gallery-dl file organization patterns">
        <button onClick={() => setShowCreate(true)} className="px-4 py-2 bg-slate-900 dark:bg-slate-700 text-white rounded text-sm hover:bg-slate-800 dark:hover:bg-slate-600">+ New</button>
      </PageHeader>
      {templates.isLoading && <div className="space-y-2">{Array.from({ length: 3 }).map((_, i) => <div key={i} className="h-16 bg-gray-100 dark:bg-slate-700 rounded animate-pulse" />)}</div>}
      {templates.error && <ErrorState message={(templates.error as Error).message} />}
      {!templates.isLoading && tpls.length === 0 && <EmptyState title="No templates" description="Create naming templates to control gallery-dl output paths." />}
      {tpls.map((t: any) => (
        <div key={t.id} className="bg-white dark:bg-slate-800 rounded-lg shadow p-4 flex items-center justify-between mb-2">
          <div><div className="font-medium text-sm">{t.name}</div><div className="text-xs text-gray-500 dark:text-gray-400 font-mono mt-1">{t.template}</div>{t.source && <span className="text-xs bg-gray-100 dark:bg-slate-700 px-2 py-0.5 rounded mt-1 inline-block">{t.source}</span>}{t.is_default && <span className="text-xs bg-green-100 dark:bg-green-900/30 text-green-700 dark:text-green-400 px-2 py-0.5 rounded ml-1">default</span>}</div>
          <div className="flex gap-2"><button onClick={() => setEditTpl(t)} className="text-xs text-blue-600 hover:underline">Edit</button><button onClick={() => setDeleteId(t.id)} className="text-xs text-red-500 hover:underline">Delete</button></div>
        </div>
      ))}
      <Modal open={showCreate} onClose={() => setShowCreate(false)} title="New Template"><TemplateForm onClose={() => setShowCreate(false)} onSave={(d) => create.mutate(d)} isPending={create.isPending} /></Modal>
      <Modal open={!!editTpl} onClose={() => setEditTpl(null)} title="Edit Template"><TemplateForm template={editTpl as any} onClose={() => setEditTpl(null)} onSave={(d) => update.mutate(d)} isPending={update.isPending} /></Modal>
      {deleteId && <ConfirmDialog open title="Delete Template" message="Delete this naming template?" onConfirm={() => del.mutate(deleteId)} onCancel={() => setDeleteId(null)} isPending={del.isPending} error={(del.error as Error)?.message} />}
    </main>
  );
}

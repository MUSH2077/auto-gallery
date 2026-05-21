"use client";
import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { PageHeader, EmptyState, ErrorState, Modal, ConfirmDialog } from "@/components";
import { useT } from "@/lib/i18n";

function TemplateForm({ template, onClose, onSave, isPending }: {
  template?: { id?: string; name: string; source?: string; template: string; is_default: boolean };
  onClose: () => void; onSave: (d: Record<string, unknown>) => void; isPending: boolean;
}) {
  const t = useT();
  const [name, setName] = useState(template?.name || "");
  const [source, setSource] = useState(template?.source || "");
  const [tpl, setTpl] = useState(template?.template || "");
  const [isDefault, setIsDefault] = useState(template?.is_default || false);
  return (
    <div className="space-y-4">
      <div><label className="block text-sm font-medium mb-1">{t("naming.name_label")}</label><input value={name} onChange={(e) => setName(e.target.value)} className="w-full border rounded px-3 py-2 text-sm" placeholder={t("naming.name_placeholder")} /></div>
      <div><label className="block text-sm font-medium mb-1">{t("naming.source_label")}</label><input value={source} onChange={(e) => setSource(e.target.value)} className="w-full border rounded px-3 py-2 text-sm" placeholder={t("naming.source_placeholder")} /></div>
      <div><label className="block text-sm font-medium mb-1">{t("naming.template_label")}</label><textarea value={tpl} onChange={(e) => setTpl(e.target.value)} rows={3} className="w-full border rounded px-3 py-2 text-sm font-mono" placeholder={t("naming.template_placeholder")} /></div>
      <div className="flex items-center gap-2"><input type="checkbox" checked={isDefault} onChange={(e) => setIsDefault(e.target.checked)} /><span className="text-sm">{t("naming.set_default")}</span></div>
      <div className="flex justify-end gap-3 pt-2">
        <button onClick={onClose} className="px-4 py-2 text-sm border rounded hover:bg-gray-50 dark:hover:bg-slate-700 dark:bg-slate-800/50">{t("naming.cancel")}</button>
        <button onClick={() => onSave({ name, source: source || undefined, template: tpl, is_default: isDefault })} disabled={!name || !tpl || isPending} className="px-4 py-2 text-sm bg-slate-900 dark:bg-slate-700 text-white rounded hover:bg-slate-800 dark:hover:bg-slate-600 disabled:opacity-50">{isPending ? t("naming.saving") : t("naming.save")}</button>
      </div>
    </div>
  );
}

export default function NamingTemplatesPage() {
  const t = useT();
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
      <PageHeader title={t("naming.title")} description={t("naming.desc")}>
        <button onClick={() => setShowCreate(true)} className="px-4 py-2 bg-slate-900 dark:bg-slate-700 text-white rounded text-sm hover:bg-slate-800 dark:hover:bg-slate-600">{t("naming.new")}</button>
      </PageHeader>
      {templates.isLoading && <div className="space-y-2">{Array.from({ length: 3 }).map((_, i) => <div key={i} className="h-16 bg-gray-100 dark:bg-slate-700 rounded animate-pulse" />)}</div>}
      {templates.error && <ErrorState message={(templates.error as Error).message} />}
      {!templates.isLoading && tpls.length === 0 && <EmptyState title={t("naming.no_templates")} description={t("naming.no_templates_desc")} />}
      {tpls.map((tmpl: any) => (
        <div key={tmpl.id} className="bg-white dark:bg-slate-800 rounded-lg shadow p-4 flex items-center justify-between mb-2">
          <div><div className="font-medium text-sm">{tmpl.name}</div><div className="text-xs text-gray-500 dark:text-gray-400 font-mono mt-1">{tmpl.template}</div>{tmpl.source && <span className="text-xs bg-gray-100 dark:bg-slate-700 px-2 py-0.5 rounded mt-1 inline-block">{tmpl.source}</span>}{tmpl.is_default && <span className="text-xs bg-green-100 dark:bg-green-900/30 text-green-700 dark:text-green-400 px-2 py-0.5 rounded ml-1">{t("naming.default_badge")}</span>}</div>
          <div className="flex gap-2"><button onClick={() => setEditTpl(tmpl)} className="text-xs text-blue-600 hover:underline">{t("naming.edit")}</button><button onClick={() => setDeleteId(tmpl.id)} className="text-xs text-red-500 hover:underline">{t("naming.delete")}</button></div>
        </div>
      ))}
      <Modal open={showCreate} onClose={() => setShowCreate(false)} title={t("naming.new_title")}><TemplateForm onClose={() => setShowCreate(false)} onSave={(d) => create.mutate(d)} isPending={create.isPending} /></Modal>
      <Modal open={!!editTpl} onClose={() => setEditTpl(null)} title={t("naming.edit_title")}><TemplateForm template={editTpl as any} onClose={() => setEditTpl(null)} onSave={(d) => update.mutate(d)} isPending={update.isPending} /></Modal>
      {deleteId && <ConfirmDialog open title={t("naming.delete_title")} message={t("naming.delete_msg")} onConfirm={() => del.mutate(deleteId)} onCancel={() => setDeleteId(null)} isPending={del.isPending} error={(del.error as Error)?.message} />}
    </main>
  );
}

"use client";
import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api, queryKeys } from "@/lib/api";
import { PageHeader, PageShell, ConfirmDialog } from "@/components";
import { useToast } from "@/components/Toast";
import { useT } from "@/lib/i18n";
import Link from "next/link";

export default function DataManagementPage() {
  const toast = useToast();
  const t = useT();
  const qc = useQueryClient();
  const [confirmAction, setConfirmAction] = useState<string | null>(null);
  const [result, setResult] = useState<{ ok: boolean; msg: string } | null>(null);

  // After clearing/deleting data, completely remove cached queries so
  // navigating to another page never shows stale (already-deleted) data.
  // invalidateQueries alone keeps cached content (stale-while-revalidate),
  // causing "creators visible after clear but click=404" bugs.
  function clearCacheThenRefetch() {
    qc.removeQueries();           // wipe ALL cached query data
    qc.invalidateQueries();       // tell active components to refetch
  }

  const clearAll = useMutation({
    mutationFn: async () => {
      await api.clearEntity("all");
      await api.clearFailedJobs();
      return { message: t("datamgmt.clear_all_success") };
    },
    onSuccess: (d) => {
      toast.success({ message: d.message });
      clearCacheThenRefetch();
      setConfirmAction(null);
    },
    onError: (e) => {
      toast.error({ message: (e as Error).message });
      setConfirmAction(null);
    },
  });

  const clearWorks = useMutation({
    mutationFn: () => api.clearEntity("works"),
    onSuccess: (d) => { toast.success({ message: d.message }); clearCacheThenRefetch(); setConfirmAction(null); },
    onError: (e) => { toast.error({ message: (e as Error).message }); setConfirmAction(null); },
  });

  const clearCreators = useMutation({
    mutationFn: () => api.clearEntity("creators"),
    onSuccess: (d) => { toast.success({ message: d.message }); clearCacheThenRefetch(); setConfirmAction(null); },
    onError: (e) => { toast.error({ message: (e as Error).message }); setConfirmAction(null); },
  });

  const clearJobs = useMutation({
    mutationFn: async () => {
      await api.clearEntity("jobs");
      await api.clearFailedJobs();
      return { message: t("datamgmt.clear_jobs_success") };
    },
    onSuccess: (d) => { toast.success({ message: d.message }); clearCacheThenRefetch(); setConfirmAction(null); },
    onError: (e) => { toast.error({ message: (e as Error).message }); setConfirmAction(null); },
  });

  const clearTags = useMutation({
    mutationFn: () => api.clearEntity("tags"),
    onSuccess: (d) => { toast.success({ message: d.message }); clearCacheThenRefetch(); setConfirmAction(null); },
    onError: (e) => { toast.error({ message: (e as Error).message }); setConfirmAction(null); },
  });

  const clearLibrary = useMutation({
    mutationFn: () => api.clearEntity("library"),
    onSuccess: (d) => { toast.success({ message: d.message }); clearCacheThenRefetch(); setConfirmAction(null); },
    onError: (e) => { toast.error({ message: (e as Error).message }); setConfirmAction(null); },
  });

  const resetSettings = useMutation({
    mutationFn: () => api.resetSettings(),
    onSuccess: (d) => { toast.success({ message: d.message }); clearCacheThenRefetch(); setConfirmAction(null); },
    onError: (e) => { toast.error({ message: (e as Error).message }); setConfirmAction(null); },
  });

  const actions = [
    {
      key: "all",
      title: t("datamgmt.clear_all"),
      desc: t("datamgmt.clear_all.desc"),
      color: "red",
      mutation: clearAll,
    },
    {
      key: "works",
      title: t("datamgmt.clear_works"),
      desc: t("datamgmt.clear_works.desc"),
      color: "orange",
      mutation: clearWorks,
    },
    {
      key: "creators",
      title: t("datamgmt.clear_creators"),
      desc: t("datamgmt.clear_creators.desc"),
      color: "orange",
      mutation: clearCreators,
    },
    {
      key: "jobs",
      title: t("datamgmt.clear_jobs"),
      desc: t("datamgmt.clear_jobs.desc"),
      color: "yellow",
      mutation: clearJobs,
    },
    {
      key: "tags",
      title: t("datamgmt.clear_tags"),
      desc: t("datamgmt.clear_tags.desc"),
      color: "yellow",
      mutation: clearTags,
    },
    {
      key: "library",
      title: t("datamgmt.clear_library"),
      desc: t("datamgmt.clear_library.desc"),
      color: "yellow",
      mutation: clearLibrary,
    },
    {
      key: "settings",
      title: t("datamgmt.reset_settings"),
      desc: t("datamgmt.reset_settings.desc"),
      color: "blue",
      mutation: resetSettings,
    },
  ];

  return (
    <PageShell>
      <div className="flex items-center gap-4 mb-6">
        <Link href="/admin/settings" className="text-sm text-accent hover:underline">&larr; {t("datamgmt.back")}</Link>
      </div>
      <PageHeader title={t("datamgmt.title")} description={t("datamgmt.desc")} />

      {result && (
        <div className={`mb-4 p-3 rounded-lg text-sm ${result.ok ? "bg-success-subtle border border-success/30 text-success" : "bg-danger-subtle border border-danger/30 text-danger"}`}>
          {result.msg}
          <button onClick={() => setResult(null)} className="ml-3 text-xs underline">{t("datamgmt.dismiss")}</button>
        </div>
      )}

      <div className="space-y-3">
        {actions.map((a) => (
          <div key={a.key} className={`card p-4 flex items-center justify-between border-l-4 ${
            a.color === "red" ? "border-danger/30" : a.color === "orange" ? "border-orange-500" : a.color === "yellow" ? "border-warning/30" : "border-accent/30"
          }`}>
            <div>
              <h3 className="font-medium text-sm dark:text-white">{a.title}</h3>
              <p className="text-xs text-muted mt-1">{a.desc}</p>
            </div>
            <button
              onClick={() => setConfirmAction(a.key)}
              disabled={a.mutation.isPending}
              className={`ml-4 min-h-11 shrink-0 ${a.color === "blue" ? "btn-primary" : "btn-danger"}`}
            >
              {a.mutation.isPending ? t("datamgmt.processing") : a.key === "settings" ? t("datamgmt.reset") : t("datamgmt.clear")}
            </button>
          </div>
        ))}
      </div>

      {confirmAction && (
        <ConfirmDialog
          open
          title={t("datamgmt.confirm_title").replace("{action}", actions.find((a) => a.key === confirmAction)?.title || "")}
          message={t("datamgmt.confirm_msg").replace("{action}", confirmAction || "")}
          onConfirm={() => {
            const a = actions.find((a) => a.key === confirmAction);
            if (a) a.mutation.mutate();
          }}
          onCancel={() => setConfirmAction(null)}
          isPending={actions.find((a) => a.key === confirmAction)?.mutation.isPending || false}
        />
      )}
    </PageShell>
  );
}

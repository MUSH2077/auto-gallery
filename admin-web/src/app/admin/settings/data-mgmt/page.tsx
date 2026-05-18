"use client";
import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api, queryKeys } from "@/lib/api";
import { PageHeader, ConfirmDialog } from "@/components";
import Link from "next/link";

export default function DataManagementPage() {
  const qc = useQueryClient();
  const [confirmAction, setConfirmAction] = useState<string | null>(null);
  const [result, setResult] = useState<{ ok: boolean; msg: string } | null>(null);

  const clearAll = useMutation({
    mutationFn: () => api.clearEntity("all"),
    onSuccess: (d) => {
      setResult({ ok: true, msg: d.message });
      qc.invalidateQueries();
      setConfirmAction(null);
    },
    onError: (e) => {
      setResult({ ok: false, msg: (e as Error).message });
      setConfirmAction(null);
    },
  });

  const clearWorks = useMutation({
    mutationFn: () => api.clearEntity("works"),
    onSuccess: (d) => { setResult({ ok: true, msg: d.message }); qc.invalidateQueries(); setConfirmAction(null); },
    onError: (e) => { setResult({ ok: false, msg: (e as Error).message }); setConfirmAction(null); },
  });

  const clearCreators = useMutation({
    mutationFn: () => api.clearEntity("creators"),
    onSuccess: (d) => { setResult({ ok: true, msg: d.message }); qc.invalidateQueries(); setConfirmAction(null); },
    onError: (e) => { setResult({ ok: false, msg: (e as Error).message }); setConfirmAction(null); },
  });

  const clearDownloads = useMutation({
    mutationFn: () => api.clearEntity("downloads"),
    onSuccess: (d) => { setResult({ ok: true, msg: d.message }); qc.invalidateQueries(); setConfirmAction(null); },
    onError: (e) => { setResult({ ok: false, msg: (e as Error).message }); setConfirmAction(null); },
  });

  const resetSettings = useMutation({
    mutationFn: () => api.resetSettings(),
    onSuccess: (d) => { setResult({ ok: true, msg: d.message }); qc.invalidateQueries(); setConfirmAction(null); },
    onError: (e) => { setResult({ ok: false, msg: (e as Error).message }); setConfirmAction(null); },
  });

  const actions = [
    {
      key: "all",
      title: "Clear Everything",
      desc: "Delete ALL data + files: works, assets, downloads, imports, creators, subscriptions. Also clears /downloads and /library directories. This is irreversible.",
      color: "red",
      mutation: clearAll,
    },
    {
      key: "works",
      title: "Clear Works & Assets",
      desc: "Delete all works, assets, and related records. Also clears /downloads and /library directories. Keeps creators and subscriptions intact.",
      color: "orange",
      mutation: clearWorks,
    },
    {
      key: "creators",
      title: "Clear Creators & Subscriptions",
      desc: "Delete all creators, subscriptions, links, and source mappings. Keeps works and files intact.",
      color: "orange",
      mutation: clearCreators,
    },
    {
      key: "downloads",
      title: "Clear Download History",
      desc: "Delete download and import job history. Keeps works, assets, creators, and files intact.",
      color: "yellow",
      mutation: clearDownloads,
    },
    {
      key: "settings",
      title: "Reset Settings to Defaults",
      desc: "Reset all system settings (dedup, subscription defaults, download defaults, proxy) to factory defaults.",
      color: "blue",
      mutation: resetSettings,
    },
  ];

  return (
    <main className="max-w-4xl mx-auto p-6">
      <div className="flex items-center gap-4 mb-6">
        <Link href="/admin/settings" className="text-sm text-blue-600 hover:underline">&larr; Settings</Link>
      </div>
      <PageHeader title="Data Management" description="Clear data and reset settings. All destructive actions require confirmation." />

      {result && (
        <div className={`mb-4 p-3 rounded-lg text-sm ${result.ok ? "bg-green-50 dark:bg-green-900/30 border border-green-200 dark:border-green-800 text-green-700 dark:text-green-400" : "bg-red-50 dark:bg-red-900/30 border border-red-200 dark:border-red-800 text-red-700 dark:text-red-400"}`}>
          {result.msg}
          <button onClick={() => setResult(null)} className="ml-3 text-xs underline">Dismiss</button>
        </div>
      )}

      <div className="space-y-3">
        {actions.map((a) => (
          <div key={a.key} className={`bg-white dark:bg-slate-800 rounded-lg shadow p-4 flex items-center justify-between border-l-4 ${
            a.color === "red" ? "border-red-500" : a.color === "orange" ? "border-orange-500" : a.color === "yellow" ? "border-yellow-500" : "border-blue-500"
          }`}>
            <div>
              <h3 className="font-medium text-sm dark:text-white">{a.title}</h3>
              <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">{a.desc}</p>
            </div>
            <button
              onClick={() => setConfirmAction(a.key)}
              disabled={a.mutation.isPending}
              className={`px-4 py-2 text-sm text-white rounded shrink-0 ml-4 disabled:opacity-50 ${
                a.color === "red" ? "bg-red-600 hover:bg-red-700" :
                a.color === "orange" ? "bg-orange-600 hover:bg-orange-700" :
                a.color === "yellow" ? "bg-yellow-600 hover:bg-yellow-700" :
                "bg-blue-600 hover:bg-blue-700"
              }`}
            >
              {a.mutation.isPending ? "Processing..." : a.key === "settings" ? "Reset" : "Clear"}
            </button>
          </div>
        ))}
      </div>

      {confirmAction && (
        <ConfirmDialog
          open
          title={`Confirm ${actions.find((a) => a.key === confirmAction)?.title}`}
          message={`Are you sure you want to ${confirmAction === "settings" ? "reset" : "clear"} ${confirmAction}? This cannot be undone.`}
          onConfirm={() => {
            const a = actions.find((a) => a.key === confirmAction);
            if (a) a.mutation.mutate();
          }}
          onCancel={() => setConfirmAction(null)}
          isPending={actions.find((a) => a.key === confirmAction)?.mutation.isPending || false}
        />
      )}
    </main>
  );
}

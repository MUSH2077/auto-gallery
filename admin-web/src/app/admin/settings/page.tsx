"use client";
import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api, queryKeys } from "@/lib/api";
import { PageHeader, ErrorState, ConfirmDialog } from "@/components";
import { useT, useI18n } from "@/lib/i18n";
import Link from "next/link";

export default function SettingsPage() {
  const t = useT();
  const { lang, setLang } = useI18n();
  const qc = useQueryClient();
  const settings = useQuery({ queryKey: queryKeys.admin.settings, queryFn: api.getAdminSettings });
  const reindex = useMutation({
    mutationFn: api.reindexSearch,
    onSuccess: () => { qc.invalidateQueries({ queryKey: queryKeys.admin.settings }); setConfirmReindex(false); },
    onError: () => { setConfirmReindex(false); },
  });
  const [confirmReindex, setConfirmReindex] = useState(false);

  const cards = [
    { href: "/admin/settings/gallerydl", title: t("settings.gallerydl"), desc: t("settings.gallerydl.desc") },
    { href: "/admin/settings/dedup", title: t("settings.dedup"), desc: t("settings.dedup.desc") },
    { href: "/admin/settings/proxy", title: t("settings.proxy"), desc: t("settings.proxy.desc") },
    { href: "/admin/settings/auth-status", title: t("settings.auth"), desc: t("settings.auth.desc") },
    { href: "/admin/settings/subscription-defaults", title: t("settings.sub_defaults"), desc: t("settings.sub_defaults.desc") },
    { href: "/admin/settings/download-defaults", title: t("settings.dl_defaults"), desc: t("settings.dl_defaults.desc") },
    { href: "/admin/naming-templates", title: t("settings.naming"), desc: t("settings.naming.desc") },
    { href: "/admin/settings/logs", title: t("settings.logs"), desc: t("settings.logs.desc") },
    { href: "/admin/settings/data-mgmt", title: t("settings.data_mgmt"), desc: t("settings.data_mgmt.desc") },
  ];

  return (
    <main className="max-w-4xl mx-auto p-6">
      <PageHeader title={t("settings.title")} description={t("settings.desc_default")} />

      {/* Config Cards */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4 mb-8">
        {cards.map((c) => (
          <Link key={c.href} href={c.href}
            className="card rounded-lg shadow p-6 hover:shadow-md transition-shadow block">
            <h2 className="text-lg font-semibold mb-2 dark:text-white">{c.title}</h2>
            <p className="text-sm text-gray-500 dark:text-gray-400">{c.desc}</p>
          </Link>
        ))}
      </div>

      {/* Language */}
      <section className="mb-8">
        <h2 className="text-lg font-semibold mb-3 dark:text-white">{t("settings.language")}</h2>
        <div className="card rounded-lg shadow p-4">
          <div className="flex items-center justify-between">
            <div>
              <p className="text-sm font-medium dark:text-white">{t("settings.language")}</p>
              <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">{t("settings.language.desc")}</p>
            </div>
            <div className="flex gap-1 bg-gray-100 dark:bg-slate-700 rounded-lg p-0.5">
              <button
                onClick={() => setLang("zh")}
                className={`px-3 py-1.5 text-sm rounded-md transition-colors ${
                  lang === "zh"
                    ? "bg-white dark:bg-slate-600 shadow-sm font-medium text-slate-900 dark:text-white"
                    : "text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-200"
                }`}
              >
                中文
              </button>
              <button
                onClick={() => setLang("en")}
                className={`px-3 py-1.5 text-sm rounded-md transition-colors ${
                  lang === "en"
                    ? "bg-white dark:bg-slate-600 shadow-sm font-medium text-slate-900 dark:text-white"
                    : "text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-200"
                }`}
              >
                English
              </button>
            </div>
          </div>
        </div>
      </section>

      {/* Search Index */}
      <section className="mb-8">
        <h2 className="text-lg font-semibold mb-3 dark:text-white">{t("settings.search_index")}</h2>
        <div className="card rounded-lg shadow p-4">
          <div className="flex items-center justify-between">
            <div>
              <p className="text-sm font-medium dark:text-white">{t("settings.reindex_label")}</p>
              <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">{t("settings.search_index.desc")}</p>
            </div>
            <button onClick={() => setConfirmReindex(true)} disabled={reindex.isPending}
              className="px-4 py-2 bg-slate-900 dark:bg-slate-700 text-white rounded text-sm hover:bg-slate-800 dark:hover:bg-slate-600 disabled:opacity-50 shrink-0">
              {reindex.isPending ? t("settings.reindexing") : t("settings.reindex")}
            </button>
          </div>
        </div>
      </section>

      {/* Settings Summary */}
      <section className="mb-8">
        <h2 className="text-lg font-semibold mb-3 dark:text-white">{t("settings.current_config")}</h2>
        {settings.isError ? (
          <ErrorState message={settings.error?.message || t("common.error")} onRetry={() => settings.refetch()} />
        ) : !settings.data ? (
          <div className="card rounded-lg shadow p-4 animate-pulse"><div className="h-20 bg-gray-100 dark:bg-slate-700 rounded" /></div>
        ) : (
          <div className="card rounded-lg shadow p-4">
            <div className="grid grid-cols-2 gap-3 text-sm">
              {Object.entries(settings.data.dedup || {}).map(([key, value]) => (
                <div key={key} className="flex justify-between py-1 border-b dark:border-slate-700 last:border-0">
                  <span className="text-gray-500 dark:text-gray-400 capitalize">{key.replace(/_/g, " ")}</span>
                  <span className={`font-mono text-xs ${typeof value === "boolean" ? (value ? "text-green-700 dark:text-green-400" : "text-gray-400") : "text-blue-700 dark:text-blue-400"}`}>
                    {String(value)}
                  </span>
                </div>
              ))}
            </div>
          </div>
        )}
      </section>

      {/* System Information */}
      <section className="mb-8">
        <h2 className="text-lg font-semibold mb-3 dark:text-white">{t("settings.system_info")}</h2>
        <div className="card rounded-lg shadow p-4 text-sm space-y-2">
          <div className="flex justify-between"><span className="text-gray-500 dark:text-gray-400">{t("settings.backend_api")}</span><span className="font-mono text-xs dark:text-gray-300">{t("settings.backend_api_val")}</span></div>
          <div className="flex justify-between"><span className="text-gray-500 dark:text-gray-400">{t("settings.admin_web")}</span><span className="text-xs dark:text-gray-300">{t("settings.admin_web_val")}</span></div>
          <div className="flex justify-between"><span className="text-gray-500 dark:text-gray-400">{t("settings.auth_mode")}</span><span className="text-xs text-gray-400 dark:text-gray-500">{t("settings.auth_mode_val")}</span></div>
        </div>
      </section>

      {confirmReindex && <ConfirmDialog open title={t("settings.reindex_confirm_title")} message={t("settings.reindex_confirm_msg")}
        onConfirm={() => reindex.mutate()} onCancel={() => setConfirmReindex(false)}
        isPending={reindex.isPending} error={(reindex.error as Error)?.message} />}
    </main>
  );
}

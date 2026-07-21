"use client";
import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api, queryKeys } from "@/lib/api";
import { PageHeader, ErrorState, ConfirmDialog, PermissionGuard } from "@/components";
import { useT, useI18n } from "@/lib/i18n";
import { useToast } from "@/components/Toast";
import Link from "next/link";

export default function SettingsPage() {
  const toast = useToast();
  const t = useT();
  const { lang, setLang } = useI18n();
  const qc = useQueryClient();
  const settings = useQuery({ queryKey: queryKeys.admin.settings, queryFn: api.getAdminSettings });
  const reindex = useMutation({
    mutationFn: api.reindexSearch,
    onSuccess: () => { qc.invalidateQueries({ queryKey: queryKeys.admin.settings }); setConfirmReindex(false); toast.success({ message: "Reindex started" }); },
    onError: (e: Error) => { setConfirmReindex(false); toast.error({ message: e.message }); },
  });
  const [confirmReindex, setConfirmReindex] = useState(false);

  const cards = [
    { href: "/admin/settings/appearance", title: t("settings.appearance", "外观"), desc: t("settings.appearance.desc", "主题、调色板、作品预览与浏览密度。") },
    { href: "/admin/settings/showcase", title: t("showcase_settings.title"), desc: t("showcase_settings.desc") },
    { href: "/admin/settings/gallerydl", title: t("settings.gallerydl"), desc: t("settings.gallerydl.desc") },
    { href: "/admin/settings/scheduler-defaults", title: t("settings.scheduler_defaults"), desc: t("settings.scheduler_defaults.desc") },
    { href: "/admin/settings/subscription-defaults", title: t("settings.subscription_defaults", "订阅默认值"), desc: t("settings.subscription_defaults.desc", "默认同步间隔与调度器行为") },
    { href: "/admin/settings/download-defaults", title: t("settings.download_defaults", "下载任务默认值"), desc: t("settings.download_defaults.desc", "超时、重试、并行下载数等") },
    { href: "/admin/settings/dedup", title: t("settings.dedup"), desc: t("settings.dedup.desc") },
    { href: "/admin/settings/proxy", title: t("settings.proxy"), desc: t("settings.proxy.desc") },
    { href: "/admin/settings/auth-status", title: t("settings.auth"), desc: t("settings.auth.desc") },
    { href: "/admin/settings/data-mgmt", title: t("settings.data_mgmt"), desc: t("settings.data_mgmt.desc") },
    { href: "/admin/settings/logs", title: t("settings.logs"), desc: t("settings.logs.desc") },
    { href: "/admin/settings/backup", title: t("settings.backup"), desc: t("settings.backup.desc") },
  ];

  return (
    <PermissionGuard module="system">
    <main className="mx-auto max-w-4xl p-6 page-transition">
      <PageHeader title={t("settings.title")} description={t("settings.desc_default")} />

      {/* Config Cards */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4 mb-8">
        {cards.map((c) => (
          <Link key={c.href} href={c.href}
            className="card-interactive p-6 block">
            <h2 className="mb-2 text-base font-semibold text-fg">{c.title}</h2>
            <p className="text-sm text-muted">{c.desc}</p>
          </Link>
        ))}
      </div>

      {/* Language */}
      <section className="mb-8">
        <h2 className="section-title mb-3">{t("settings.language")}</h2>
        <div className="card p-4">
          <div className="flex items-center justify-between">
            <div>
              <p className="text-sm font-medium text-fg">{t("settings.language")}</p>
              <p className="mt-1 text-xs text-muted">{t("settings.language.desc")}</p>
            </div>
            <div className="segmented-control">
              <button
                onClick={() => setLang("zh")}
                className={`segment ${lang === "zh" ? "segment-active" : ""}`}
              >
                中文
              </button>
              <button
                onClick={() => setLang("en")}
                className={`segment ${lang === "en" ? "segment-active" : ""}`}
              >
                English
              </button>
            </div>
          </div>
        </div>
      </section>

      {/* Search Index */}
      <section className="mb-8">
        <h2 className="section-title mb-3">{t("settings.search_index")}</h2>
        <div className="card p-4">
          <div className="flex items-center justify-between">
            <div>
              <p className="text-sm font-medium text-fg">{t("settings.reindex_label")}</p>
              <p className="mt-1 text-xs text-muted">{t("settings.search_index.desc")}</p>
            </div>
            <button onClick={() => setConfirmReindex(true)} disabled={reindex.isPending}
              className="btn-primary shrink-0">
              {reindex.isPending ? t("settings.reindexing") : t("settings.reindex")}
            </button>
          </div>
        </div>
      </section>

      {/* Settings Summary */}
      <section className="mb-8">
        <h2 className="section-title mb-3">{t("settings.current_config")}</h2>
        {settings.isError ? (
          <ErrorState message={settings.error?.message || t("common.error")} onRetry={() => settings.refetch()} />
        ) : !settings.data ? (
          <div className="card p-4 animate-pulse"><div className="h-20 rounded bg-subtle dark:bg-subtle" /></div>
        ) : (
          <div className="card p-4">
            <div className="grid grid-cols-2 gap-3 text-sm">
              {Object.entries(settings.data.dedup || {}).map(([key, value]) => (
                <div key={key} className="flex justify-between border-b border-border py-1 last:border-0 dark:border-border">
                  <span className="capitalize text-muted">{key.replace(/_/g, " ")}</span>
                  <span className={`font-mono text-xs ${typeof value === "boolean" ? (value ? "text-success dark:text-success" : "text-placeholder dark:text-muted") : "text-accent"}`}>
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
        <h2 className="section-title mb-3">{t("settings.system_info")}</h2>
        <div className="card space-y-2 p-4 text-sm">
          <div className="flex justify-between"><span className="text-muted">{t("settings.backend_api")}</span><span className="font-mono text-xs text-fg">{t("settings.backend_api_val")}</span></div>
          <div className="flex justify-between"><span className="text-muted">{t("settings.admin_web")}</span><span className="text-xs text-fg">{t("settings.admin_web_val")}</span></div>
          <div className="flex justify-between"><span className="text-muted">{t("settings.auth_mode")}</span><span className="text-xs text-placeholder dark:text-muted">{t("settings.auth_mode_val")}</span></div>
        </div>
      </section>

      {confirmReindex && <ConfirmDialog open title={t("settings.reindex_confirm_title")} message={t("settings.reindex_confirm_msg")}
        onConfirm={() => reindex.mutate()} onCancel={() => setConfirmReindex(false)}
        isPending={reindex.isPending} error={(reindex.error as Error)?.message} />}
    </main>
    </PermissionGuard>
  );
}

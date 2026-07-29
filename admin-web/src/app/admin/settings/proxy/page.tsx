"use client";
import { useEffect, useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api, queryKeys, ProxySettings } from "@/lib/api";
import { PageHeader, PageShell, ErrorState } from "@/components";
import { useT } from "@/lib/i18n";
import { useToast } from "@/components/Toast";
import Link from "next/link";

function TestResults({ data, proxyEnabled }: { data: any | null; proxyEnabled: boolean }) {
  const t = useT();
  if (!data) return null;
  const { results, proxy_reachable, proxy_reachable_error } = data;
  return (
    <div className="mt-5">
      {proxy_reachable !== null && proxy_reachable !== undefined && (
        <div className={"mb-3 rounded-md border p-4 text-sm font-medium " + (proxy_reachable ? "border-success-subtle bg-success-subtle text-success dark:border-primary/30 dark:bg-primary/15 dark:text-success" : "border-danger/40 bg-danger-subtle text-danger dark:border-danger/40 dark:bg-danger/15 dark:text-danger")}>
          {proxy_reachable ? t("proxy.reachable") : t("proxy.unreachable") + " — " + proxy_reachable_error}
        </div>
      )}
      {proxy_reachable === false && (
        <div className="mb-3 rounded-md border border-warning-subtle bg-warning-subtle p-4 text-sm text-warning dark:border-warning/30 dark:bg-warning/15 dark:text-warning">
          {t("proxy.troubleshoot")}
        </div>
      )}
      <h4 className="font-semibold text-sm mb-3 dark:text-white">{t("proxy.test_results")}</h4>
      <div className="table-shell">
        <table className="w-full text-sm">
          <thead>
            <tr className="table-head">
              <th className="text-left px-4 py-2.5 font-medium">{t("proxy.col_site")}</th>
              <th className="text-center px-4 py-2.5 font-medium w-28">{t("proxy.col_direct")}</th>
              <th className="text-left px-4 py-2.5 font-medium w-48">{t("proxy.col_direct_error")}</th>
              {proxyEnabled && <th className="text-center px-4 py-2.5 font-medium w-28">{t("proxy.col_proxy")}</th>}
              {proxyEnabled && <th className="text-left px-4 py-2.5 font-medium w-48">{t("proxy.col_proxy_error")}</th>}
            </tr>
          </thead>
          <tbody>
            {results.map((r: any, i: number) => (
              <tr key={i} className="table-row">
                <td className="px-4 py-3">
                  <div className="font-medium dark:text-white">{r.name}</div>
                  <div className="font-mono text-xs text-muted">{r.url}</div>
                </td>
                <td className="text-center px-4 py-3">
                  {r.direct_ok
                    ? <span className="text-success text-xs font-medium">{r.direct_ms}ms {t("proxy.ok")}</span>
                    : <span className="text-danger text-xs font-medium">{t("proxy.fail")}</span>}
                </td>
                <td className="px-3 py-3">
                  {r.direct_error
                    ? <span className="text-xs text-danger font-mono" title={r.direct_error}>{r.direct_error.slice(0, 60)}</span>
                    : r.direct_ok ? <span className="text-xs text-muted">—</span> : <span className="text-xs text-muted">{t("proxy.unknown_error")}</span>}
                </td>
                {proxyEnabled && (
                  <td className="text-center px-4 py-3">
                    {r.proxy_ok === null
                      ? <span className="text-xs text-muted">—</span>
                      : r.proxy_ok
                        ? <span className="text-success text-xs font-medium">{r.proxy_ms}ms {t("proxy.ok")}</span>
                        : <span className="text-danger text-xs font-medium">{t("proxy.fail")}</span>}
                  </td>
                )}
                {proxyEnabled && (
                  <td className="px-3 py-3">
                    {r.proxy_error
                      ? <span className="text-xs text-danger font-mono" title={r.proxy_error}>{r.proxy_error.slice(0, 60)}</span>
                      : r.proxy_ok ? <span className="text-xs text-muted">—</span> : <span className="text-xs text-muted">{t("proxy.unknown_error")}</span>}
                  </td>
                )}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

export default function ProxySettingsPage() {
  const t = useT();
  const toast = useToast();
  const qc = useQueryClient();
  const settings = useQuery({ queryKey: queryKeys.admin.settings, queryFn: api.getAdminSettings });
  const [local, setLocal] = useState<ProxySettings | null>(null);

  useEffect(() => {
    if (settings.data?.proxy) {
      setLocal((current) => current ?? { ...settings.data!.proxy });
    }
  }, [settings.data]);

  const save = useMutation({
    mutationFn: (data: ProxySettings) => api.updateAdminSettings({ proxy: data }),
    onSuccess: () => { qc.invalidateQueries({ queryKey: queryKeys.admin.settings }); toast.success({ message: t("notification.saved") }); },
  });
  const testProxy = useMutation({ mutationFn: () => api.testProxy() });

  const current = local || settings.data?.proxy;
  if (settings.isError) return <PageShell><ErrorState message={settings.error?.message || t("proxy.failed")} onRetry={() => settings.refetch()} /></PageShell>;
  if (!settings.data) return <PageShell><div className="animate-pulse space-y-4"><div className="h-8 w-1/3 rounded-md bg-subtle dark:bg-subtle" /><div className="h-48 rounded-md bg-subtle dark:bg-subtle" /></div></PageShell>;
  const setStr = (key: keyof ProxySettings, val: string) => { if (current) setLocal({ ...current, [key]: val }); };

  return (
    <PageShell>
      <div className="flex items-center gap-4 mb-6">
        <Link href="/admin/settings" className="text-sm text-accent hover:underline">&larr; {t("proxy.back")}</Link>
      </div>
      <PageHeader title={t("proxy.title")} description={t("proxy.desc")} />

      {current && (
        <>
          <div className="card p-6 space-y-5">
            <div className="flex items-center justify-between pb-4 border-b border-border">
              <div>
                <span className="font-medium text-sm dark:text-white">{t("proxy.enable")}</span>
                <p className="text-xs text-muted mt-0.5">{t("proxy.enable.desc")}</p>
              </div>
              <button
                type="button"
                role="switch"
                aria-checked={current.enabled}
                aria-label={t("proxy.enable")}
                onClick={() => setLocal({ ...current, enabled: !current.enabled })}
                className="relative inline-flex h-11 w-12 shrink-0 items-center justify-center rounded-md"
              >
                <span className={"relative inline-flex h-6 w-11 items-center rounded-full transition-colors " + (current.enabled ? "bg-success" : "bg-subtle")}>
                  <span className={"inline-block h-4 w-4 transform rounded-full bg-white transition-transform " + (current.enabled ? "translate-x-6" : "translate-x-1")} />
                </span>
              </button>
            </div>

            <div className="space-y-4">
              {([
                ["http_proxy", t("proxy.http"), t("proxy.http.desc"), "http://192.0.2.10:7890"],
                ["https_proxy", t("proxy.https"), t("proxy.https.desc"), "http://192.0.2.10:7890"],
                ["no_proxy", t("proxy.no"), t("proxy.no.desc"), "localhost,127.0.0.1,::1"],
              ] as [keyof ProxySettings, string, string, string][]).map(([key, label, desc, ph]) => (
                <div key={key}>
                  <label htmlFor={`proxy-${key}`} className="block text-sm font-medium mb-1.5 dark:text-white">{label}</label>
                  <input id={`proxy-${key}`} type="text" value={(current[key] as string) || ""}
                    onChange={(e) => setStr(key, e.target.value)} placeholder={ph}
                    className="input w-full max-w-lg font-mono" />
                  <p className="text-xs text-muted mt-1">{desc}</p>
                </div>
              ))}
            </div>
          </div>

          <div className="mt-4 rounded-md border border-accent-subtle bg-accent-subtle p-4 text-sm text-accent dark:border-accent/30 dark:bg-accent/15 dark:text-accent">
            <strong>{t("proxy.scope")}</strong>
            <ul className="list-disc list-inside mt-1 space-y-1">
              <li>{t("proxy.scope.1")}</li>
              <li>{t("proxy.scope.2")}</li>
              <li>{t("proxy.scope.3")}</li>
            </ul>
          </div>

          <div className="mt-4 rounded-md border border-border bg-subtle p-5 dark:border-border dark:bg-canvas">
            <div className="flex items-center justify-between">
              <div>
                <span className="font-medium text-sm dark:text-white">{t("proxy.connectivity_test")}</span>
                <p className="text-xs text-muted mt-0.5">{t("proxy.connectivity_test.desc")}</p>
              </div>
              <button onClick={() => testProxy.mutate()} disabled={testProxy.isPending}
                className="btn-primary min-h-11 shrink-0 px-4 text-sm">
                {testProxy.isPending ? t("proxy.testing") : t("proxy.test_now")}
              </button>
            </div>
            {testProxy.error && <p className="text-danger text-xs mt-2">{(testProxy.error as Error).message}</p>}
            {testProxy.data && <TestResults data={testProxy.data} proxyEnabled={testProxy.data.proxy_enabled} />}
          </div>

          <div className="mt-4 flex justify-end items-center">
            
            
            <button onClick={() => save.mutate(current)} disabled={save.isPending}
              className="btn-primary px-6">
              {save.isPending ? t("common.saving") : t("proxy.save")}
            </button>
          </div>
        </>
      )}
    </PageShell>
  );
}

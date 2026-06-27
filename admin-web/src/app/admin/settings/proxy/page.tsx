"use client";
import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api, queryKeys, ProxySettings } from "@/lib/api";
import { PageHeader, ErrorState } from "@/components";
import { useT } from "@/lib/i18n";
import { useToast } from "@/components/Toast";
import Link from "next/link";

function TestResults({ data, proxyEnabled }: { data: any | null; proxyEnabled: boolean }) {
  const t = useT();
  const toast = useToast();
  if (!data) return null;
  const { results, proxy_reachable, proxy_reachable_error } = data;
  return (
    <div className="mt-5">
      {proxy_reachable !== null && proxy_reachable !== undefined && (
        <div className={"mb-3 rounded-md border p-4 text-sm font-medium " + (proxy_reachable ? "border-[#dafbe1] bg-[#dafbe1] text-success dark:border-primary/30 dark:bg-primary/15 dark:text-[#56d364]" : "border-[#ff8182]/40 bg-[#ffebe9] text-danger dark:border-danger/40 dark:bg-danger/15 dark:text-[#ff7b72]")}>
          {proxy_reachable ? t("proxy.reachable") : t("proxy.unreachable") + " — " + proxy_reachable_error}
        </div>
      )}
      {proxy_reachable === false && (
        <div className="mb-3 rounded-md border border-[#fff8c5] bg-[#fff8c5] p-4 text-sm text-warning dark:border-warning/30 dark:bg-warning/15 dark:text-[#f2cc60]">
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
                    ? <span className="text-green-600 dark:text-green-400 text-xs font-medium">{r.direct_ms}ms {t("proxy.ok")}</span>
                    : <span className="text-red-500 text-xs font-medium">{t("proxy.fail")}</span>}
                </td>
                <td className="px-3 py-3">
                  {r.direct_error
                    ? <span className="text-xs text-red-500 dark:text-red-400 font-mono" title={r.direct_error}>{r.direct_error.slice(0, 60)}</span>
                    : r.direct_ok ? <span className="text-xs text-muted">—</span> : <span className="text-xs text-muted">{t("proxy.unknown_error")}</span>}
                </td>
                {proxyEnabled && (
                  <td className="text-center px-4 py-3">
                    {r.proxy_ok === null
                      ? <span className="text-xs text-muted">—</span>
                      : r.proxy_ok
                        ? <span className="text-green-600 dark:text-green-400 text-xs font-medium">{r.proxy_ms}ms {t("proxy.ok")}</span>
                        : <span className="text-red-500 text-xs font-medium">{t("proxy.fail")}</span>}
                  </td>
                )}
                {proxyEnabled && (
                  <td className="px-3 py-3">
                    {r.proxy_error
                      ? <span className="text-xs text-red-500 dark:text-red-400 font-mono" title={r.proxy_error}>{r.proxy_error.slice(0, 60)}</span>
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
  

  const save = useMutation({
    mutationFn: (data: ProxySettings) => api.updateAdminSettings({ proxy: data }),
    onSuccess: () => { qc.invalidateQueries({ queryKey: queryKeys.admin.settings }); toast.success({ message: t("notification.saved") }); },
  });
  const testProxy = useMutation({ mutationFn: () => api.testProxy() });

  const current = local || settings.data?.proxy;
  if (settings.isError) return <main className="max-w-4xl mx-auto p-6"><ErrorState message={settings.error?.message || t("proxy.failed")} onRetry={() => settings.refetch()} /></main>;
  if (!settings.data) return <main className="max-w-4xl mx-auto p-6"><div className="animate-pulse space-y-4"><div className="h-8 w-1/3 rounded-md bg-subtle dark:bg-subtle" /><div className="h-48 rounded-md bg-subtle dark:bg-subtle" /></div></main>;
  if (!local && settings.data.proxy) setLocal({ ...settings.data.proxy });

  const setStr = (key: keyof ProxySettings, val: string) => { if (current) setLocal({ ...current, [key]: val }); };

  return (
    <main className="max-w-4xl mx-auto p-6">
      <div className="flex items-center gap-4 mb-6">
        <Link href="/admin/settings" className="text-sm text-blue-600 hover:underline">&larr; {t("proxy.back")}</Link>
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
              <button onClick={() => setLocal({ ...current, enabled: !current.enabled })}
                className={"relative inline-flex h-6 w-11 items-center rounded-full transition-colors shrink-0 " + (current.enabled ? "bg-green-600" : "bg-subtle")}>
                <span className={"inline-block h-4 w-4 transform rounded-full bg-white transition-transform " + (current.enabled ? "translate-x-6" : "translate-x-1")} />
              </button>
            </div>

            <div className="space-y-4">
              {([
                ["http_proxy", t("proxy.http"), t("proxy.http.desc"), "http://192.168.10.170:7890"],
                ["https_proxy", t("proxy.https"), t("proxy.https.desc"), "http://192.168.10.170:7890"],
                ["no_proxy", t("proxy.no"), t("proxy.no.desc"), "localhost,127.0.0.1,::1"],
              ] as [keyof ProxySettings, string, string, string][]).map(([key, label, desc, ph]) => (
                <div key={key}>
                  <label className="block text-sm font-medium mb-1.5 dark:text-white">{label}</label>
                  <input type="text" value={(current[key] as string) || ""}
                    onChange={(e) => setStr(key, e.target.value)} placeholder={ph}
                    className="input w-full max-w-lg font-mono" />
                  <p className="text-xs text-muted mt-1">{desc}</p>
                </div>
              ))}
            </div>
          </div>

          <div className="mt-4 rounded-md border border-[#ddf4ff] bg-[#ddf4ff] p-4 text-sm text-accent dark:border-accent/30 dark:bg-accent/15 dark:text-accent">
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
                className="px-4 py-2 bg-blue-600 text-white rounded-lg text-sm font-medium hover:bg-blue-700 disabled:opacity-50 shrink-0 transition-colors">
                {testProxy.isPending ? t("proxy.testing") : t("proxy.test_now")}
              </button>
            </div>
            {testProxy.error && <p className="text-red-600 text-xs mt-2">{(testProxy.error as Error).message}</p>}
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
    </main>
  );
}

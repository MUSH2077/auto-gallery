"use client";
import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api, queryKeys, ProxySettings } from "@/lib/api";
import { PageHeader, ErrorState } from "@/components";
import { useT } from "@/lib/i18n";
import Link from "next/link";

function TestResults({ data, proxyEnabled }: { data: any | null; proxyEnabled: boolean }) {
  const t = useT();
  if (!data) return null;
  const { results, proxy_reachable, proxy_reachable_error } = data;
  return (
    <div className="mt-6">
      {/* Proxy reachability banner */}
      {proxy_reachable !== null && proxy_reachable !== undefined && (
        <div className={`p-3 rounded text-sm font-medium mb-3 ${proxy_reachable ? "bg-green-50 dark:bg-green-900/30 border border-green-200 dark:border-green-800 text-green-700 dark:text-green-400" : "bg-red-50 dark:bg-red-900/30 border border-red-200 dark:border-red-800 text-red-700 dark:text-red-400"}`}>
          {proxy_reachable ? t("proxy.reachable") : `${t("proxy.unreachable")} — ${proxy_reachable_error}`}
        </div>
      )}
      {proxy_reachable === false && (
        <div className="p-3 rounded text-sm mb-3 bg-yellow-50 dark:bg-yellow-900/30 border border-yellow-200 dark:border-yellow-800 text-yellow-700 dark:text-yellow-400">
          {t("proxy.troubleshoot")}
        </div>
      )}
      <h4 className="font-medium text-sm mb-3 dark:text-white">{t("proxy.test_results")}</h4>
      <div className="bg-white dark:bg-slate-800 rounded-lg shadow overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b dark:border-slate-700 bg-gray-50 dark:bg-slate-700/50">
              <th className="text-left px-4 py-2 font-medium">{t("proxy.col_site")}</th>
              <th className="text-center px-4 py-2 font-medium w-28">{t("proxy.col_direct")}</th>
              <th className="text-left px-4 py-2 font-medium w-48">{t("proxy.col_direct_error")}</th>
              {proxyEnabled && <th className="text-center px-4 py-2 font-medium w-28">{t("proxy.col_proxy")}</th>}
              {proxyEnabled && <th className="text-left px-4 py-2 font-medium w-48">{t("proxy.col_proxy_error")}</th>}
            </tr>
          </thead>
          <tbody>
            {results.map((r: any, i: number) => (
              <tr key={i} className="border-b dark:border-slate-700 last:border-0">
                <td className="px-4 py-3">
                  <div className="font-medium dark:text-white">{r.name}</div>
                  <div className="text-xs text-gray-400 dark:text-gray-500 font-mono">{r.url}</div>
                </td>
                <td className="text-center px-4 py-3">
                  {r.direct_ok
                    ? <span className="text-green-600 dark:text-green-400 text-xs font-medium">{r.direct_ms}ms {t("proxy.ok")}</span>
                    : <span className="text-red-500 text-xs font-medium">{t("proxy.fail")}</span>}
                </td>
                <td className="px-3 py-3">
                  {r.direct_error
                    ? <span className="text-xs text-red-500 dark:text-red-400 font-mono" title={r.direct_error}>{r.direct_error.slice(0, 60)}</span>
                    : r.direct_ok ? <span className="text-gray-400 text-xs">—</span> : <span className="text-gray-400 text-xs">{t("proxy.unknown_error")}</span>}
                </td>
                {proxyEnabled && (
                  <td className="text-center px-4 py-3">
                    {r.proxy_ok === null
                      ? <span className="text-gray-400 text-xs">—</span>
                      : r.proxy_ok
                        ? <span className="text-green-600 dark:text-green-400 text-xs font-medium">{r.proxy_ms}ms {t("proxy.ok")}</span>
                        : <span className="text-red-500 text-xs font-medium">{t("proxy.fail")}</span>}
                  </td>
                )}
                {proxyEnabled && (
                  <td className="px-3 py-3">
                    {r.proxy_error
                      ? <span className="text-xs text-red-500 dark:text-red-400 font-mono" title={r.proxy_error}>{r.proxy_error.slice(0, 60)}</span>
                      : r.proxy_ok ? <span className="text-gray-400 text-xs">—</span> : <span className="text-gray-400 text-xs">{t("proxy.unknown_error")}</span>}
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
  const qc = useQueryClient();
  const settings = useQuery({ queryKey: queryKeys.admin.settings, queryFn: api.getAdminSettings });
  const [local, setLocal] = useState<ProxySettings | null>(null);

  const save = useMutation({
    mutationFn: (data: ProxySettings) => api.updateAdminSettings({ proxy: data }),
    onSuccess: () => qc.invalidateQueries({ queryKey: queryKeys.admin.settings }),
  });

  const testProxy = useMutation({
    mutationFn: () => api.testProxy(),
  });

  const current = local || settings.data?.proxy;

  if (settings.isError) {
    return (
      <main className="max-w-4xl mx-auto p-6">
        <ErrorState message={settings.error?.message || t("proxy.failed")} onRetry={() => settings.refetch()} />
      </main>
    );
  }

  if (!settings.data) {
    return (
      <main className="max-w-4xl mx-auto p-6">
        <div className="animate-pulse space-y-4">
          <div className="h-8 bg-gray-200 rounded w-1/3" />
          <div className="h-48 bg-gray-200 rounded" />
        </div>
      </main>
    );
  }

  if (!local && settings.data.proxy) {
    setLocal({ ...settings.data.proxy });
  }

  const setStr = (key: keyof ProxySettings, val: string) => {
    if (!current) return;
    setLocal({ ...current, [key]: val });
  };

  const toggle = () => {
    if (!current) return;
    setLocal({ ...current, enabled: !current.enabled });
  };

  return (
    <main className="max-w-4xl mx-auto p-6">
      <div className="flex items-center gap-4 mb-6">
        <Link href="/admin/settings" className="text-sm text-blue-600 hover:underline">&larr; {t("proxy.back")}</Link>
      </div>
      <PageHeader title={t("proxy.title")} description={t("proxy.desc")} />

      {!current ? null : (
        <>
          <div className="bg-white dark:bg-slate-800 rounded-lg shadow p-6 space-y-5 text-sm">
            <div className="flex items-center justify-between py-3 border-b dark:border-slate-700">
              <div>
                <span className="font-medium">{t("proxy.enable")}</span>
                <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">{t("proxy.enable.desc")}</p>
              </div>
              <button
                onClick={toggle}
                className={`relative inline-flex h-6 w-11 items-center rounded-full transition-colors shrink-0 ${
                  current.enabled ? "bg-green-600" : "bg-gray-300"
                }`}
              >
                <span className={`inline-block h-4 w-4 transform rounded-full bg-white transition-transform ${
                  current.enabled ? "translate-x-6" : "translate-x-1"
                }`} />
              </button>
            </div>

            <div className="grid grid-cols-1 gap-4">
              <div>
                <label className="block text-sm font-medium mb-1">{t("proxy.http")}</label>
                <input
                  type="text" value={current.http_proxy}
                  onChange={(e) => setStr("http_proxy", e.target.value)}
                  placeholder="http://192.0.2.10:7890"
                  className="w-full max-w-md border rounded px-3 py-2 text-sm font-mono dark:bg-slate-700 dark:text-white"
                />
                <p className="text-xs text-gray-400 mt-1">{t("proxy.http.desc")}</p>
              </div>

              <div>
                <label className="block text-sm font-medium mb-1">{t("proxy.https")}</label>
                <input
                  type="text" value={current.https_proxy}
                  onChange={(e) => setStr("https_proxy", e.target.value)}
                  placeholder="http://192.0.2.10:7890"
                  className="w-full max-w-md border rounded px-3 py-2 text-sm font-mono dark:bg-slate-700 dark:text-white"
                />
                <p className="text-xs text-gray-400 mt-1">{t("proxy.https.desc")}</p>
              </div>

              <div>
                <label className="block text-sm font-medium mb-1">{t("proxy.no")}</label>
                <input
                  type="text" value={current.no_proxy}
                  onChange={(e) => setStr("no_proxy", e.target.value)}
                  placeholder="localhost,127.0.0.1,::1"
                  className="w-full max-w-md border rounded px-3 py-2 text-sm font-mono dark:bg-slate-700 dark:text-white"
                />
                <p className="text-xs text-gray-400 mt-1">{t("proxy.no.desc")}</p>
              </div>
            </div>
          </div>

          <div className="mt-4 p-4 bg-blue-50 dark:bg-blue-900/30 border border-blue-200 dark:border-blue-800 rounded-lg text-sm text-blue-800 dark:text-blue-300">
            <strong>{t("proxy.scope")}</strong>
            <ul className="list-disc list-inside mt-1 space-y-1">
              <li>{t("proxy.scope.1")}</li>
              <li>{t("proxy.scope.2")}</li>
              <li>{t("proxy.scope.3")}</li>
            </ul>
          </div>

          <div className="mt-4 p-4 bg-yellow-50 dark:bg-yellow-900/30 border border-yellow-200 dark:border-yellow-800 rounded-lg text-sm text-yellow-800 dark:text-yellow-300">
            <strong>{t("proxy.known_config")}</strong>
          </div>

          {/* Connectivity Test */}
          <div className="mt-4 p-4 bg-gray-50 dark:bg-slate-700/30 rounded-lg">
            <div className="flex items-center justify-between">
              <div>
                <span className="font-medium text-sm dark:text-white">{t("proxy.connectivity_test")}</span>
                <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">{t("proxy.connectivity_test.desc")}</p>
              </div>
              <button
                onClick={() => testProxy.mutate()}
                disabled={testProxy.isPending}
                className="px-4 py-2 bg-blue-600 text-white rounded text-sm hover:bg-blue-700 disabled:opacity-50 shrink-0"
              >
                {testProxy.isPending ? t("proxy.testing") : t("proxy.test_now")}
              </button>
            </div>
            {testProxy.error && <p className="text-red-600 text-xs mt-2">{(testProxy.error as Error).message}</p>}
            {testProxy.data && (
              <TestResults data={testProxy.data} proxyEnabled={testProxy.data.proxy_enabled} />
            )}
          </div>

          <div className="mt-4 flex justify-end">
            <button
              onClick={() => save.mutate(current)}
              disabled={save.isPending}
              className="px-6 py-2 bg-slate-900 dark:bg-slate-700 text-white rounded text-sm hover:bg-slate-800 dark:hover:bg-slate-600 disabled:opacity-50"
            >
              {save.isPending ? t("common.saving") : t("proxy.save")}
            </button>
          </div>
          {save.isSuccess && <p className="text-green-600 text-sm mt-2">{t("proxy.saved")}</p>}
          {save.error && <p className="text-red-600 text-sm mt-2">{(save.error as Error).message}</p>}
        </>
      )}
    </main>
  );
}

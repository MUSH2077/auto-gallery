"use client";
import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api, queryKeys, ProxySettings } from "@/lib/api";
import { PageHeader, ErrorState } from "@/components";
import Link from "next/link";

export default function ProxySettingsPage() {
  const qc = useQueryClient();
  const settings = useQuery({ queryKey: queryKeys.admin.settings, queryFn: api.getAdminSettings });
  const [local, setLocal] = useState<ProxySettings | null>(null);

  const save = useMutation({
    mutationFn: (data: ProxySettings) => api.updateAdminSettings({ proxy: data }),
    onSuccess: () => qc.invalidateQueries({ queryKey: queryKeys.admin.settings }),
  });

  const current = local || settings.data?.proxy;

  if (settings.isError) {
    return (
      <main className="max-w-4xl mx-auto p-6">
        <ErrorState message={settings.error?.message || "Failed"} onRetry={() => settings.refetch()} />
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
        <Link href="/admin/settings" className="text-sm text-blue-600 hover:underline">&larr; Settings</Link>
      </div>
      <PageHeader title="Network Proxy" description="Configure HTTP/HTTPS proxy for gallery-dl and external API access." />

      {!current ? null : (
        <>
          <div className="bg-white dark:bg-slate-800 rounded-lg shadow p-6 space-y-5 text-sm">
            <div className="flex items-center justify-between py-3 border-b dark:border-slate-700">
              <div>
                <span className="font-medium">Enable Proxy</span>
                <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">Route external API calls and gallery-dl traffic through a proxy server.</p>
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
                <label className="block text-sm font-medium mb-1">HTTP Proxy</label>
                <input
                  type="text" value={current.http_proxy}
                  onChange={(e) => setStr("http_proxy", e.target.value)}
                  placeholder="http://192.168.10.170:7890"
                  className="w-full max-w-md border rounded px-3 py-2 text-sm font-mono dark:bg-slate-700 dark:text-white"
                />
                <p className="text-xs text-gray-400 mt-1">Used for HTTP requests (Danbooru API, etc.). Leave empty to not use HTTP proxy.</p>
              </div>

              <div>
                <label className="block text-sm font-medium mb-1">HTTPS Proxy</label>
                <input
                  type="text" value={current.https_proxy}
                  onChange={(e) => setStr("https_proxy", e.target.value)}
                  placeholder="http://192.168.10.170:7890"
                  className="w-full max-w-md border rounded px-3 py-2 text-sm font-mono dark:bg-slate-700 dark:text-white"
                />
                <p className="text-xs text-gray-400 mt-1">Used for HTTPS connections. Usually same as HTTP proxy.</p>
              </div>

              <div>
                <label className="block text-sm font-medium mb-1">No Proxy (bypass)</label>
                <input
                  type="text" value={current.no_proxy}
                  onChange={(e) => setStr("no_proxy", e.target.value)}
                  placeholder="localhost,127.0.0.1,::1"
                  className="w-full max-w-md border rounded px-3 py-2 text-sm font-mono dark:bg-slate-700 dark:text-white"
                />
                <p className="text-xs text-gray-400 mt-1">Comma-separated hosts to bypass proxy. Default: localhost,127.0.0.1,::1</p>
              </div>
            </div>
          </div>

          <div className="mt-4 p-4 bg-blue-50 dark:bg-blue-900/30 border border-blue-200 dark:border-blue-800 rounded-lg text-sm text-blue-800 dark:text-blue-300">
            <strong>Applies to:</strong>
            <ul className="list-disc list-inside mt-1 space-y-1">
              <li>gallery-dl subprocess (downloads from Pixiv, Twitter, Iwara)</li>
              <li>Danbooru API reference queries (artist search, URL import)</li>
              <li>Other external HTTP calls made by the backend</li>
            </ul>
          </div>

          <div className="mt-4 p-4 bg-yellow-50 dark:bg-yellow-900/30 border border-yellow-200 dark:border-yellow-800 rounded-lg text-sm text-yellow-800 dark:text-yellow-300">
            <strong>Known configuration:</strong> If using Clash/V2Ray on WSL2, set both HTTP and HTTPS proxy to <code className="bg-yellow-100 dark:bg-yellow-900/50 px-1 rounded">http://192.168.10.170:7890</code> (your Windows host IP with proxy port).
          </div>

          <div className="mt-4 flex justify-end">
            <button
              onClick={() => save.mutate(current)}
              disabled={save.isPending}
              className="px-6 py-2 bg-slate-900 dark:bg-slate-700 text-white rounded text-sm hover:bg-slate-800 dark:hover:bg-slate-600 disabled:opacity-50"
            >
              {save.isPending ? "Saving..." : "Save Settings"}
            </button>
          </div>
          {save.isSuccess && <p className="text-green-600 text-sm mt-2">Proxy settings saved.</p>}
          {save.error && <p className="text-red-600 text-sm mt-2">{(save.error as Error).message}</p>}
        </>
      )}
    </main>
  );
}

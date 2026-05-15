"use client";
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, queryKeys, ProviderInfo } from "@/lib/api";
import { PageHeader, EmptyState } from "@/components";

const DEFAULT_URLS: Record<string, string> = {
  pixiv: "https://www.pixiv.net/en/artworks/12345678",
  iwara: "https://www.iwara.tv/video/abc123",
  x: "https://x.com/artist_handle/status/1234567890123456789",
  danbooru_reference: "https://danbooru.donmai.us/artists/12345",
  local: "/path/to/local/folder",
  manual: "(manual upload — no URL required)",
};

const SOURCE_DESCRIPTIONS: Record<string, string> = {
  pixiv: "Pixiv artworks, user profiles, favorites, rankings, and search results. First fully-supported downloadable source. Requires cookies or OAuth refresh-token for authentication.",
  iwara: "Iwara video and profile pages. Currently a placeholder — download support planned for a future phase.",
  x: "X / Twitter media posts and user timelines. Currently a placeholder with no timeline. Re-evaluate after Pixiv pipeline stabilizes.",
  danbooru_reference: "Danbooru artist tag reference data for creator identity mapping. Reference only — not a default media download source. Does not contain the complete union of all works.",
  local: "Import media from a local folder on the NAS. Does not use gallery-dl. Supports manual organization of existing media libraries.",
  manual: "Manual upload of individual files through the admin interface. Does not use gallery-dl. For one-off additions.",
};

const URL_PATTERNS: Record<string, RegExp> = {
  pixiv: /pixiv\.net\/(?:en\/)?(artworks|users)\/\d+/,
  iwara: /iwara\.tv\/(video|profile)\/[\w-]+/,
  x: /(?:twitter\.com|x\.com)\/\w+(?:\/status\/\d+)?\/?$/,
  danbooru_reference: /danbooru\.donmai\.us\/(artists\/\d+|posts\?tags=.+)/,
  local: /.+/,
  manual: /.+/,
};

function ProviderCard({ s }: { s: ProviderInfo }) {
  const [url, setUrl] = useState("");
  const [validResult, setValidResult] = useState<{ ok: boolean; msg: string } | null>(null);

  const handleTest = () => {
    if (!url.trim()) { setValidResult({ ok: false, msg: "Enter a URL to validate." }); return; }
    const pattern = URL_PATTERNS[s.source_name];
    if (!pattern) { setValidResult({ ok: false, msg: "No validation pattern defined." }); return; }
    if (pattern.test(url)) {
      setValidResult({ ok: true, msg: `URL matches expected ${s.display_name} pattern. Ready to submit as download job.` });
    } else {
      setValidResult({ ok: false, msg: `URL does not match expected ${s.display_name} pattern. Check the format and try again.` });
    }
  };

  return (
    <div className="bg-white rounded-lg shadow p-4">
      <div className="flex items-center justify-between mb-2">
        <span className="font-medium text-lg">{s.display_name}</span>
        <span className="text-xs text-gray-400 font-mono">{s.source_name}</span>
      </div>
      <p className="text-xs text-gray-500 mb-3 leading-relaxed">{SOURCE_DESCRIPTIONS[s.source_name] || "No description available."}</p>
      <div className="flex gap-2 flex-wrap mb-3">
        {s.capabilities.can_download
          ? <span className="px-2 py-0.5 bg-green-100 text-green-700 rounded text-xs font-medium">Download: Available</span>
          : <span className="px-2 py-0.5 bg-gray-100 text-gray-500 rounded text-xs">Download: Placeholder</span>}
        {s.capabilities.supports_gallerydl && <span className="px-2 py-0.5 bg-blue-100 text-blue-700 rounded text-xs font-medium">gallery-dl</span>}
        {s.capabilities.supports_tags && <span className="px-2 py-0.5 bg-purple-100 text-purple-700 rounded text-xs font-medium">Tags</span>}
        {s.capabilities.is_reference_only && <span className="px-2 py-0.5 bg-yellow-100 text-yellow-700 rounded text-xs font-medium">Reference Only</span>}
        {s.capabilities.can_import_local && <span className="px-2 py-0.5 bg-teal-100 text-teal-700 rounded text-xs font-medium">Local Import</span>}
      </div>

      <div className="border-t pt-3">
        <label className="text-xs text-gray-500 block mb-1">Test URL Validation</label>
        <div className="flex gap-2 mb-2">
          <input type="text" value={url} onChange={(e) => setUrl(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && handleTest()}
            placeholder={DEFAULT_URLS[s.source_name] || "https://..."}
            className="flex-1 text-sm border rounded px-3 py-1.5 font-mono" />
          <button onClick={handleTest} className="text-xs px-3 py-1.5 bg-slate-900 text-white rounded hover:bg-slate-800 shrink-0">Test</button>
        </div>
        <button onClick={() => setUrl(DEFAULT_URLS[s.source_name] || "")}
          className="text-xs text-blue-600 hover:underline mb-2 block">
          Try default URL: <span className="font-mono text-gray-400">{DEFAULT_URLS[s.source_name]?.slice(0, 40)}{(DEFAULT_URLS[s.source_name]?.length || 0) > 40 ? "..." : ""}</span>
        </button>
        {validResult && (
          <div className={`p-2 rounded text-xs ${validResult.ok ? "bg-green-50 text-green-700 border border-green-200" : "bg-red-50 text-red-700 border border-red-200"}`}>
            {validResult.ok ? "✓ " : "✗ "}{validResult.msg}
          </div>
        )}
      </div>
    </div>
  );
}

export default function SourcesPage() {
  const sources = useQuery({ queryKey: queryKeys.sources, queryFn: api.sources });
  const downloadable = sources.data?.sources?.filter((s) => s.capabilities.can_download).length || 0;
  const reference = sources.data?.sources?.filter((s) => s.capabilities.is_reference_only).length || 0;

  return (
    <main className="max-w-6xl mx-auto p-6">
      <PageHeader title="Source Providers" description={`${sources.data?.sources?.length || 0} registered · ${downloadable} downloadable · ${reference} reference-only`} />

      {sources.isLoading && <div className="grid grid-cols-1 md:grid-cols-2 gap-4">{Array.from({ length: 6 }).map((_, i) => <div key={i} className="bg-white rounded-lg shadow p-4 animate-pulse"><div className="h-4 bg-gray-200 rounded w-1/2 mb-2" /><div className="h-3 bg-gray-200 rounded w-3/4 mb-4" /><div className="h-16 bg-gray-200 rounded" /></div>)}</div>}

      {sources.error && <div className="p-4 bg-red-50 border border-red-200 rounded-lg text-red-700">{(sources.error as Error).message}</div>}

      {sources.data && !sources.data.sources.length && <EmptyState title="No providers registered" description="Register source providers in the backend configuration." />}

      {sources.data && sources.data.sources.length > 0 && (
        <>
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4 mb-8">
            {sources.data.sources.map((s) => <ProviderCard key={s.source_name} s={s} />)}
          </div>

          <details className="bg-white rounded-lg shadow p-4 text-sm">
            <summary className="font-medium cursor-pointer">Provider Capability Matrix</summary>
            <div className="overflow-x-auto mt-3">
              <table className="w-full text-sm">
                <thead><tr className="border-b bg-gray-50"><th className="text-left py-2 px-2">Provider</th><th className="text-center py-2 px-2">Download</th><th className="text-center py-2 px-2">gallery-dl</th><th className="text-center py-2 px-2">Tags</th><th className="text-center py-2 px-2">Reference</th><th className="text-center py-2 px-2">Local</th><th className="text-left py-2 px-2">Auth</th></tr></thead>
                <tbody>
                  {sources.data.sources.map((s) => (
                    <tr key={s.source_name} className="border-b hover:bg-gray-50">
                      <td className="py-2 px-2 font-medium">{s.display_name}</td>
                      <td className="text-center py-2 px-2">{s.capabilities.can_download ? "✓" : "—"}</td>
                      <td className="text-center py-2 px-2">{s.capabilities.supports_gallerydl ? "✓" : "—"}</td>
                      <td className="text-center py-2 px-2">{s.capabilities.supports_tags ? "✓" : "—"}</td>
                      <td className="text-center py-2 px-2">{s.capabilities.is_reference_only ? "✓" : "—"}</td>
                      <td className="text-center py-2 px-2">{s.capabilities.can_import_local ? "✓" : "—"}</td>
                      <td className="py-2 px-2 text-xs text-gray-500">{s.source_name === "pixiv" ? "OAuth / Cookies" : s.source_name === "x" ? "OAuth (future)" : "N/A"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </details>
        </>
      )}
    </main>
  );
}

"use client";
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, queryKeys, ProviderInfo } from "@/lib/api";
import { PageHeader, EmptyState } from "@/components";
import { useT } from "@/lib/i18n";

const DEFAULT_URLS: Record<string, string> = {
  pixiv: "https://www.pixiv.net/artworks/12345678",
  iwara: "https://www.iwara.tv/video/abc123",
  x: "https://x.com/artist_handle/status/1234567890123456789",
  danbooru: "https://danbooru.donmai.us/posts?tags=ask",
  pinterest: "https://www.pinterest.com/username/pins/",
  lofter: "https://blogname.lofter.com/",
  danbooru_reference: "https://danbooru.donmai.us/artists/12345",
  local: "/path/to/local/folder",
  manual: "(manual upload — no URL required)",
};

const SOURCE_DESCRIPTIONS: Record<string, string> = {
  pixiv: "Pixiv artworks, user profiles, favorites, rankings, and search results. First fully-supported downloadable source. Requires cookies or OAuth refresh-token for authentication.",
  iwara: "Iwara video and profile pages. Currently a placeholder — download support planned for a future phase.",
  x: "X / Twitter media posts and user timelines. Currently a placeholder with no timeline. Re-evaluate after Pixiv pipeline stabilizes.",
  danbooru: "Danbooru post download via tag search (e.g. posts?tags=artist_name). Supports full download pipeline with rich tag metadata (artist, character, copyright, general, meta categories). Defaults to disabled when a creator is imported — must be manually enabled.",
  danbooru_reference: "Danbooru artist reference data for creator identity mapping. Used to discover external URLs (Pixiv, Twitter, etc.) from Danbooru artist records and suggest creator links. Not a download source — see 'danbooru' provider above for post downloads.",
  local: "Import media from a local folder on the NAS. Does not use gallery-dl. Supports manual organization of existing media libraries.",
  pinterest: "Pinterest pins, boards, and user all-pins. Downloads images from pin pages, user profiles, and board collections. No tag system.",
  lofter: "LOFTER blog posts and images. Chinese blogging platform popular with artists. Downloads post images. No tag metadata from gallery-dl.",
  manual: "Manual upload of individual files through the admin interface. Does not use gallery-dl. For one-off additions.",
};

const URL_PATTERNS: Record<string, RegExp> = {
  pixiv: /pixiv\.net\/(?:en\/)?(artworks|users)\/\d+/,
  iwara: /iwara\.tv\/(video|profile)\/[\w-]+/,
  x: /(?:twitter\.com|x\.com)\/\w+(?:\/status\/\d+)?\/?$/,
  danbooru: /danbooru\.donmai\.us\/posts\?tags=.+/,
  danbooru_reference: /danbooru\.donmai\.us\/(artists\/\d+|posts\?tags=.+)/,
  pinterest: /pinterest\.\w+\/(pin\/\d+|[\w.-]+\/(pins|[\w.-]+))/,
  lofter: /[\w-]+\.lofter\.com(\/post\/[\w_]+)?/,
  local: /.+/,
  manual: /.+/,
};

function ProviderCard({ s }: { s: ProviderInfo }) {
  const t = useT();
  const [url, setUrl] = useState("");
  const [validResult, setValidResult] = useState<{ ok: boolean; msg: string } | null>(null);

  const handleTest = () => {
    if (!url.trim()) { setValidResult({ ok: false, msg: t("sources.enter_url") }); return; }
    const pattern = URL_PATTERNS[s.source_name];
    if (!pattern) { setValidResult({ ok: false, msg: t("sources.no_pattern") }); return; }
    if (pattern.test(url)) {
      setValidResult({ ok: true, msg: t("sources.match_ok").replace("{source}", s.display_name) });
    } else {
      setValidResult({ ok: false, msg: t("sources.match_fail").replace("{source}", s.display_name) });
    }
  };

  return (
    <div className="bg-white dark:bg-slate-800 rounded-lg shadow p-4">
      <div className="flex items-center justify-between mb-2">
        <span className="font-medium text-lg">{s.display_name}</span>
        <span className="text-xs text-gray-400 dark:text-gray-500 font-mono">{s.source_name}</span>
      </div>
      <p className="text-xs text-gray-500 dark:text-gray-400 mb-3 leading-relaxed">{SOURCE_DESCRIPTIONS[s.source_name] || t("sources.no_desc")}</p>
      <div className="flex gap-2 flex-wrap mb-3">
        {s.capabilities.can_download
          ? <span className="px-2 py-0.5 bg-green-100 dark:bg-green-900/30 text-green-700 dark:text-green-400 rounded text-xs font-medium">{t("sources.download_available")}</span>
          : <span className="px-2 py-0.5 bg-gray-100 dark:bg-slate-700 text-gray-500 dark:text-gray-400 rounded text-xs">{t("sources.download_placeholder")}</span>}
        {s.capabilities.supports_gallerydl && <span className="px-2 py-0.5 bg-blue-100 text-blue-700 rounded text-xs font-medium">{t("sources.gallerydl")}</span>}
        {s.capabilities.supports_tags && <span className="px-2 py-0.5 bg-purple-100 text-purple-700 rounded text-xs font-medium">{t("sources.tags")}</span>}
        {s.capabilities.is_reference_only && <span className="px-2 py-0.5 bg-yellow-100 text-yellow-700 rounded text-xs font-medium">{t("sources.reference_only")}</span>}
        {s.capabilities.can_import_local && <span className="px-2 py-0.5 bg-teal-100 text-teal-700 rounded text-xs font-medium">{t("sources.local_import")}</span>}
      </div>

      <div className="border-t pt-3">
        <label className="text-xs text-gray-500 dark:text-gray-400 block mb-1">{t("sources.test_validation")}</label>
        <div className="flex gap-2 mb-2">
          <input type="text" value={url} onChange={(e) => setUrl(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && handleTest()}
            placeholder={DEFAULT_URLS[s.source_name] || "https://..."}
            className="flex-1 text-sm border rounded px-3 py-1.5 font-mono" />
          <button onClick={handleTest} className="text-xs px-3 py-1.5 bg-slate-900 dark:bg-slate-700 text-white rounded hover:bg-slate-800 dark:hover:bg-slate-600 shrink-0">{t("sources.test")}</button>
        </div>
        <button onClick={() => setUrl(DEFAULT_URLS[s.source_name] || "")}
          className="text-xs text-blue-600 hover:underline mb-2 block">
          {t("sources.try_default")} <span className="font-mono text-gray-400 dark:text-gray-500">{DEFAULT_URLS[s.source_name]?.slice(0, 40)}{(DEFAULT_URLS[s.source_name]?.length || 0) > 40 ? "..." : ""}</span>
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
  const t = useT();
  const sources = useQuery({ queryKey: queryKeys.sources, queryFn: api.sources });
  const downloadable = sources.data?.sources?.filter((s) => s.capabilities.can_download).length || 0;
  const reference = sources.data?.sources?.filter((s) => s.capabilities.is_reference_only).length || 0;
  const total = sources.data?.sources?.length || 0;

  return (
    <main className="max-w-6xl mx-auto p-6">
      <PageHeader title={t("sources.title")} description={t("sources.desc").replace("{total}", String(total)).replace("{downloadable}", String(downloadable)).replace("{reference}", String(reference))} />

      {sources.isLoading && <div className="grid grid-cols-1 md:grid-cols-2 gap-4">{Array.from({ length: 6 }).map((_, i) => <div key={i} className="bg-white dark:bg-slate-800 rounded-lg shadow p-4 animate-pulse"><div className="h-4 bg-gray-200 rounded w-1/2 mb-2" /><div className="h-3 bg-gray-200 rounded w-3/4 mb-4" /><div className="h-16 bg-gray-200 rounded" /></div>)}</div>}

      {sources.error && <div className="p-4 bg-red-50 border border-red-200 rounded-lg text-red-700 dark:text-red-400">{(sources.error as Error).message}</div>}

      {sources.data && !sources.data.sources.length && <EmptyState title={t("sources.no_providers")} description={t("sources.no_providers_desc")} />}

      {sources.data && sources.data.sources.length > 0 && (
        <>
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4 mb-8">
            {sources.data.sources.map((s) => <ProviderCard key={s.source_name} s={s} />)}
          </div>

          <details className="bg-white dark:bg-slate-800 rounded-lg shadow p-4 text-sm">
            <summary className="font-medium cursor-pointer">{t("sources.matrix")}</summary>
            <div className="overflow-x-auto mt-3">
              <table className="w-full text-sm">
                <thead><tr className="border-b dark:border-slate-700 bg-gray-50 dark:bg-slate-800/50"><th className="text-left py-2 px-2">{t("sources.col_provider")}</th><th className="text-center py-2 px-2">{t("sources.col_download")}</th><th className="text-center py-2 px-2">{t("sources.col_gallerydl")}</th><th className="text-center py-2 px-2">{t("sources.col_tags")}</th><th className="text-center py-2 px-2">{t("sources.col_reference")}</th><th className="text-center py-2 px-2">{t("sources.col_local")}</th><th className="text-left py-2 px-2">{t("sources.col_auth")}</th></tr></thead>
                <tbody>
                  {sources.data.sources.map((s) => (
                    <tr key={s.source_name} className="border-b dark:border-slate-700 hover:bg-gray-50 dark:hover:bg-slate-700 dark:bg-slate-800/50">
                      <td className="py-2 px-2 font-medium">{s.display_name}</td>
                      <td className="text-center py-2 px-2">{s.capabilities.can_download ? "✓" : "—"}</td>
                      <td className="text-center py-2 px-2">{s.capabilities.supports_gallerydl ? "✓" : "—"}</td>
                      <td className="text-center py-2 px-2">{s.capabilities.supports_tags ? "✓" : "—"}</td>
                      <td className="text-center py-2 px-2">{s.capabilities.is_reference_only ? "✓" : "—"}</td>
                      <td className="text-center py-2 px-2">{s.capabilities.can_import_local ? "✓" : "—"}</td>
                      <td className="py-2 px-2 text-xs text-gray-500 dark:text-gray-400">{s.source_name === "pixiv" ? t("sources.auth_oauth") : s.source_name === "x" ? t("sources.auth_oauth_future") : s.source_name === "danbooru" ? t("sources.auth_basic") : t("sources.auth_na")}</td>
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

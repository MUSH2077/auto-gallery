"use client";
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, queryKeys, ProviderInfo } from "@/lib/api";
import { getSourceColor } from "@/lib/sourceColors";
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
  weibo: "https://weibo.com/u/1234567890",
  bilibili: "https://space.bilibili.com/123456",
  xiaohongshu: "https://www.xiaohongshu.com/user/profile/abc123",
  youtube: "https://www.youtube.com/@channelname",
  deviantart: "https://www.deviantart.com/username",
  artstation: "https://www.artstation.com/username",
  instagram: "https://www.instagram.com/username/",
  tiktok: "https://www.tiktok.com/@username",
  bluesky: "https://bsky.app/profile/username.bsky.social",
  mastodon: "https://mastodon.social/@username",
  misskey: "https://misskey.io/@username",
  fantia: "https://fantia.jp/fanclubs/12345",
  fanbox: "https://username.fanbox.cc",
  skeb: "https://skeb.jp/@username",
  patreon: "https://www.patreon.com/username",
  boosty: "https://boosty.to/username",
  gumroad: "https://username.gumroad.com",
  nicovideo: "https://www.nicovideo.jp/user/12345",
  vimeo: "https://vimeo.com/username",
  carrd: "https://username.carrd.co",
  aboutme: "https://about.me/username",
  linktree: "https://linktr.ee/username",
  threads: "https://www.threads.net/@username",
  reddit: "https://www.reddit.com/user/username",
  tumblr: "https://username.tumblr.com",
  facebook: "https://www.facebook.com/username",
};

const SOURCE_DESCRIPTIONS: Record<string, string> = {
  pixiv: "Pixiv artworks, user profiles, favorites, rankings, and search results. First fully-supported downloadable source. Requires cookies or OAuth refresh-token for authentication.",
  iwara: "Iwara video and profile pages. gallery-dl supported with username/password or cookie authentication, configurable format preferences, and tag metadata.",
  x: "X / Twitter media posts and user timelines. gallery-dl uses the twitter extractor with cookie authentication and the tweets strategy.",
  danbooru: "Danbooru post download via tag search (e.g. posts?tags=artist_name). Supports full download pipeline with rich tag metadata (artist, character, copyright, general, meta categories). Defaults to disabled when a creator is imported — must be manually enabled.",
  danbooru_reference: "Danbooru artist reference data for creator identity mapping. Used to discover external URLs (Pixiv, Twitter, etc.) from Danbooru artist records and suggest creator links. Not a download source — see 'danbooru' provider above for post downloads.",
  local: "Local folder import is planned. It will not use gallery-dl and will target existing media libraries on the host.",
  pinterest: "Pinterest pins, boards, and user all-pins. gallery-dl supported for public image collections. No tag metadata from gallery-dl.",
  lofter: "LOFTER blog posts and images. gallery-dl supported for public blog images. Directory templates should include post IDs to avoid merging posts.",
  manual: "Manual upload is planned for one-off additions through the admin interface. It will not use gallery-dl.",
  weibo: "Weibo user timelines and media posts. gallery-dl supported for public media, with optional cookies for account-specific access.",
  bilibili: "Bilibili user videos, covers, and dynamic media. gallery-dl supported for public content.",
  xiaohongshu: "Xiaohongshu (RED) user posts and media. Chinese lifestyle platform. Downloads post images.",
  youtube: "YouTube channel videos and thumbnails. Downloads video metadata and cover images.",
  deviantart: "DeviantArt user galleries and artwork. Downloads images with tag metadata.",
  artstation: "ArtStation user portfolios and projects. Downloads high-resolution artwork.",
  instagram: "Instagram user posts and stories. Downloads images from public profiles.",
  tiktok: "TikTok user videos. Downloads public videos without watermark.",
  bluesky: "Bluesky user posts and media. AT Protocol-based social network. Downloads post images.",
  mastodon: "Mastodon user posts and media. Federated social network. Downloads post images.",
  misskey: "Misskey user notes and media. Federated microblogging platform.",
  fantia: "Fantia creator posts and media. Japanese creator support platform. Downloads images.",
  fanbox: "Pixiv Fanbox creator posts. Japanese creator support platform. Downloads images.",
  skeb: "Skeb creator commissions. Japanese commission platform.",
  patreon: "Patreon creator posts. International creator support platform. Downloads public posts.",
  boosty: "Boosty creator posts. Creator support platform. Downloads public posts.",
  gumroad: "Gumroad creator products. Digital product platform.",
  nicovideo: "Niconico video pages. Japanese video platform.",
  vimeo: "Vimeo video pages. Professional video platform.",
  carrd: "Carrd profile pages. Single-page website builder.",
  aboutme: "About.me profile pages. Personal profile platform.",
  linktree: "Linktree profile pages. Link aggregator platform.",
  threads: "Threads user profiles. Meta's text-based social network.",
  reddit: "Reddit user posts and subreddits. Downloads images from public posts.",
  tumblr: "Tumblr blog posts. Downloads images from public blogs.",
  facebook: "Facebook public pages and posts. Downloads public media.",
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
  weibo: /weibo\.(?:com|cn)\/(?:u\/\d+|[\w一-鿿]+)/,
  bilibili: /bilibili\.com\/video\/BV[\w]+|space\.bilibili\.com\/\d+/,
  xiaohongshu: /xiaohongshu\.com\/user\/profile\/[\w-]+/,
  youtube: /youtube\.com\/(?:@[\w-]+|channel\/[\w-]+|c\/[\w-]+)/,
  deviantart: /deviantart\.com\/[\w-]+/,
  artstation: /artstation\.com\/[\w-]+/,
  instagram: /instagram\.com\/[\w.-]+\/?/,
  tiktok: /tiktok\.com\/@[\w.-]+/,
  bluesky: /bsky\.app\/profile\/[\w.-]+/,
  mastodon: /mastodon\.\w+\/@[\w.]+/,
  misskey: /misskey\.\w+\/@[\w.]+/,
  fantia: /fantia\.jp\/fanclubs\/\d+/,
  fanbox: /[\w-]+\.fanbox\.cc/,
  skeb: /skeb\.jp\/@[\w-]+/,
  patreon: /patreon\.com\/[\w-]+/,
  boosty: /boosty\.to\/[\w-]+/,
  gumroad: /[\w-]+\.gumroad\.com/,
  nicovideo: /nicovideo\.jp\/user\/\d+/,
  vimeo: /vimeo\.com\/[\w-]+/,
  carrd: /[\w-]+\.carrd\.co/,
  aboutme: /about\.me\/[\w-]+/,
  linktree: /linktr\.ee\/[\w-]+/,
  threads: /threads\.net\/@[\w.]+/,
  reddit: /reddit\.com\/u(?:ser)?\/[\w-]+/,
  tumblr: /[\w-]+\.tumblr\.com/,
  facebook: /facebook\.com\/[\w.-]+/,
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
      setValidResult({ ok: true, msg: t("sources.match_ok", { source: s.display_name }) });
    } else {
      setValidResult({ ok: false, msg: t("sources.match_fail", { source: s.display_name }) });
    }
  };

  return (
    <div className="card p-4">
      <div className="flex items-center justify-between mb-2">
        <span className="w-3 h-3 rounded-full inline-block mr-2 shrink-0" style={{ backgroundColor: getSourceColor(s.source_name) }} /><span className="font-medium text-lg">{s.display_name}</span>
        <span className="font-mono text-xs text-[#57606a] dark:text-[#8b949e]">{s.source_name}</span>
      </div>
      <p className="mb-3 text-xs leading-relaxed text-[#57606a] dark:text-[#8b949e]">{SOURCE_DESCRIPTIONS[s.source_name] || t("sources.no_desc")}</p>
      <div className="flex gap-2 flex-wrap mb-3">
        {s.capabilities.can_download
          ? <span className="badge border-[#dafbe1] bg-[#dafbe1] text-[#1a7f37] dark:border-[#238636]/30 dark:bg-[#238636]/15 dark:text-[#56d364]">{t("sources.download_available")}</span>
          : <span className="badge">{t("sources.download_placeholder")}</span>}
        {s.capabilities.supports_gallerydl && <span className="badge border-[#ddf4ff] bg-[#ddf4ff] text-[#0969da] dark:border-[#1f6feb]/30 dark:bg-[#1f6feb]/15 dark:text-[#58a6ff]">{t("sources.gallerydl")}</span>}
        {s.capabilities.supports_tags && <span className="badge">{t("sources.tags")}</span>}
        {s.capabilities.is_reference_only && <span className="badge border-[#fff8c5] bg-[#fff8c5] text-[#9a6700] dark:border-[#d29922]/30 dark:bg-[#d29922]/15 dark:text-[#f2cc60]">{t("sources.reference_only")}</span>}
        {s.capabilities.can_import_local && <span className="badge border-[#dafbe1] bg-[#dafbe1] text-[#1a7f37] dark:border-[#238636]/30 dark:bg-[#238636]/15 dark:text-[#56d364]">{t("sources.local_import")}</span>}
      </div>

      <div className="border-t border-ag-border pt-3 dark:border-ag-border">
        <label className="mb-1 block text-xs text-[#57606a] dark:text-[#8b949e]">{t("sources.test_validation")}</label>
        <div className="flex gap-2 mb-2">
          <input type="text" value={url} onChange={(e) => setUrl(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && handleTest()}
            placeholder={DEFAULT_URLS[s.source_name] || "https://..."}
            className="input flex-1 font-mono" />
          <button onClick={handleTest} className="btn-primary shrink-0 px-3 py-1.5 text-xs">{t("sources.test")}</button>
        </div>
        <button onClick={() => setUrl(DEFAULT_URLS[s.source_name] || "")}
          className="text-xs text-blue-600 hover:underline mb-2 block">
          {t("sources.try_default")} <span className="font-mono text-[#57606a] dark:text-[#8b949e]">{DEFAULT_URLS[s.source_name]?.slice(0, 40)}{(DEFAULT_URLS[s.source_name]?.length || 0) > 40 ? "..." : ""}</span>
        </button>
        {validResult && (
          <div className={`rounded-md border p-2 text-xs ${validResult.ok ? "border-[#dafbe1] bg-[#dafbe1] text-[#1a7f37] dark:border-[#238636]/30 dark:bg-[#238636]/15 dark:text-[#56d364]" : "border-[#ff8182]/40 bg-[#ffebe9] text-[#cf222e] dark:border-[#da3633]/40 dark:bg-[#da3633]/15 dark:text-[#ff7b72]"}`}>
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
      <PageHeader title={t("sources.title")} description={t("sources.desc", { total, downloadable, reference })} />

      {sources.isLoading && <div className="grid grid-cols-1 gap-4 md:grid-cols-2">{Array.from({ length: 6 }).map((_, i) => <div key={i} className="card p-4 animate-pulse"><div className="mb-2 h-4 w-1/2 rounded bg-[#eaeef2] dark:bg-[#21262d]" /><div className="mb-4 h-3 w-3/4 rounded bg-[#eaeef2] dark:bg-[#21262d]" /><div className="h-16 rounded bg-[#eaeef2] dark:bg-[#21262d]" /></div>)}</div>}

      {sources.error && <div className="rounded-md border border-[#ff8182]/40 bg-[#ffebe9] p-4 text-sm text-[#cf222e] dark:border-[#da3633]/40 dark:bg-[#da3633]/15 dark:text-[#ff7b72]">{(sources.error as Error).message}</div>}

      {sources.data && !sources.data.sources.length && <EmptyState title={t("sources.no_providers")} description={t("sources.no_providers_desc")} />}

      {sources.data && sources.data.sources.length > 0 && (
        <>
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4 mb-8">
            {sources.data.sources.map((s) => <ProviderCard key={s.source_name} s={s} />)}
          </div>

          <details className="card p-4 text-sm">
            <summary className="font-medium cursor-pointer">{t("sources.matrix")}</summary>
            <div className="table-shell mt-3">
              <table className="w-full text-sm">
                <thead><tr className="table-head"><th className="text-left py-2 px-2">{t("sources.col_provider")}</th><th className="text-center py-2 px-2">{t("sources.col_download")}</th><th className="text-center py-2 px-2">{t("sources.col_gallerydl")}</th><th className="text-center py-2 px-2">{t("sources.col_tags")}</th><th className="text-center py-2 px-2">{t("sources.col_reference")}</th><th className="text-center py-2 px-2">{t("sources.col_local")}</th><th className="text-left py-2 px-2">{t("sources.col_auth")}</th></tr></thead>
                <tbody>
                  {sources.data.sources.map((s) => (
                    <tr key={s.source_name} className="table-row">
                      <td className="py-2 px-2 font-medium">{s.display_name}</td>
                      <td className="text-center py-2 px-2">{s.capabilities.can_download ? "✓" : "—"}</td>
                      <td className="text-center py-2 px-2">{s.capabilities.supports_gallerydl ? "✓" : "—"}</td>
                      <td className="text-center py-2 px-2">{s.capabilities.supports_tags ? "✓" : "—"}</td>
                      <td className="text-center py-2 px-2">{s.capabilities.is_reference_only ? "✓" : "—"}</td>
                      <td className="text-center py-2 px-2">{s.capabilities.can_import_local ? "✓" : "—"}</td>
                      <td className="py-2 px-2 text-xs text-[#57606a] dark:text-[#8b949e]">{s.source_name === "pixiv" ? t("sources.auth_oauth") : s.source_name === "x" ? t("sources.auth_oauth_future") : s.source_name === "danbooru" ? t("sources.auth_basic") : t("sources.auth_na")}</td>
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

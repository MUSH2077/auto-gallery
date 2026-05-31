"use client";
import { useState, useEffect } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api, queryKeys } from "@/lib/api";
import { useToast } from "@/components/Toast";
import { useT } from "@/lib/i18n";
import { PageHeader, EmptyState, ErrorState, SourceBadge } from "@/components";

interface ArtistUrls { url: string; normalized_url: string; is_active: boolean }
interface ArtistResult {
  id: number; name: string; other_names: string[]; post_count?: number;
  notes?: string; is_active?: boolean; created_at?: string; urls: ArtistUrls[];
  pixiv_display_name?: string | null;
}
interface SuggestedLink { url: string; link_type: string; source: string; confidence: number; is_verified: boolean; notes?: string }

function PreviewResult({ artist, links, onImport, importPending, onImportAll, importAllPending, importAllError, selectedCreator, setSelectedCreator, importName, setImportName }: {
  artist: ArtistResult; links: SuggestedLink[];
  onImport: (creatorId: string) => void; importPending: boolean;
  onImportAll: (creatorName: string) => void; importAllPending: boolean; importAllError: string | null;
  selectedCreator: string; setSelectedCreator: (v: string) => void;
  importName: string; setImportName: (v: string) => void;
}) {
  const t = useT();
  const toast = useToast();
  const creators = useQuery({ queryKey: queryKeys.creators.all, queryFn: () => api.listCreators() });
  const qc = useQueryClient();

  const [subscribingUrl, setSubscribingUrl] = useState<string | null>(null);
  const subs = useQuery({ queryKey: queryKeys.subscriptions.all, queryFn: () => api.listSubscriptions() });

  const subscribe = useMutation({
    mutationFn: async (params: { creatorId: string; url: string; source: string; sourceCreatorId?: string }) => {
      // Find existing subscription for this creator, or create one
      let sub = subs.data?.find((s) => s.creator_id === params.creatorId);
      if (!sub) {
        sub = await api.createSubscription({ creator_id: params.creatorId, name: undefined });
      }
      return api.createSubscriptionSource(sub.id, {
        source: params.source,
        source_url: params.url,
        source_creator_id: params.sourceCreatorId,
        is_enabled: true,
      });
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: queryKeys.subscriptions.all });
      setSubscribingUrl(null);
      toast.info("Subscription source created! Trigger a sync from the Subscriptions page.");
    },
    onError: (err) => {
      toast.info(`Failed: ${(err as Error).message}`);
      setSubscribingUrl(null);
    },
  });

  // Find downloadable URLs from the artist
  const downloadableUrls = artist.urls.filter((u) => {
    const linkType = classifyUrl(u.normalized_url);
    return DOWNLOADABLE_SOURCES.includes(linkType) && u.is_active;
  });

  return (
    <div className="mt-4 space-y-4">
      <div className="bg-white dark:bg-slate-800 border rounded-lg p-4">
        <h3 className="font-medium mb-2">{t("danbooru.artist_label").replace("{id}", String(artist.id)).replace("{name}", artist.name)}</h3>
        {artist.other_names.length > 0 && (
          <div className="flex flex-wrap items-center gap-1 mb-1">
            <span className="text-xs text-gray-500 dark:text-gray-400 shrink-0">{t("danbooru.also_known")}</span>
            {artist.other_names.map((n) => (
              <button key={n} type="button" onClick={() => setImportName(n)}
                className={`text-xs px-2 py-0.5 rounded-full border transition-colors cursor-pointer
                  ${importName === n
                    ? "bg-blue-600 text-white border-blue-600"
                    : "bg-gray-100 dark:bg-slate-700 text-gray-700 dark:text-gray-300 border-gray-300 hover:bg-blue-50 hover:border-blue-400 dark:hover:bg-blue-900/30"}`}>
                {n}
              </button>
            ))}
          </div>
        )}
        {artist.post_count != null && <p className="text-xs text-gray-500 dark:text-gray-400">{t("danbooru.posts_count")} {artist.post_count}</p>}
        {artist.notes && <p className="text-xs text-gray-600 dark:text-gray-300 mt-2 bg-gray-50 dark:bg-slate-800/50 p-2 rounded">{artist.notes}</p>}
      </div>

      {/* One-Click Import All & Subscribe */}
      <div className="bg-blue-50 dark:bg-blue-900/30 border border-blue-200 dark:border-blue-800 rounded-lg p-4">
        <p className="text-sm text-blue-800 dark:text-blue-300 font-medium mb-1">{t("danbooru.one_click_import")}</p>
        <p className="text-xs text-blue-600 mb-3">Creates Creator + Subscription + Sources + Links in one step.</p>
        <div className="mb-3">
          <label className="block text-xs font-medium text-blue-800 dark:text-blue-300 mb-1">
            创建者名称
            <span className="font-normal text-blue-600 dark:text-blue-400 ml-1">（留空则自动使用 Danbooru 标签名）</span>
          </label>
          {/* Clickable name chips: Pixiv display name + Danbooru aliases */}
          {(artist.pixiv_display_name || artist.other_names.length > 0) && (
            <div className="flex flex-wrap gap-1 mb-2">
              {artist.pixiv_display_name && (
                <button key="__pixiv__" type="button" onClick={() => setImportName(artist.pixiv_display_name!)}
                  title="Pixiv 用户名"
                  className={`text-xs px-2 py-0.5 rounded-full border transition-colors cursor-pointer flex items-center gap-1
                    ${importName === artist.pixiv_display_name
                      ? "bg-blue-600 text-white border-blue-600"
                      : "bg-blue-100 dark:bg-blue-900/50 text-blue-700 dark:text-blue-300 border-blue-300 hover:bg-blue-200 dark:hover:bg-blue-800/50"}`}>
                  <span className="opacity-70">P</span>{artist.pixiv_display_name}
                </button>
              )}
              {artist.other_names.map((n) => (
                <button key={n} type="button" onClick={() => setImportName(n)}
                  title="Danbooru 别名"
                  className={`text-xs px-2 py-0.5 rounded-full border transition-colors cursor-pointer
                    ${importName === n
                      ? "bg-blue-600 text-white border-blue-600"
                      : "bg-gray-100 dark:bg-slate-700 text-gray-700 dark:text-gray-300 border-gray-300 hover:bg-blue-50 hover:border-blue-400 dark:hover:bg-blue-900/30"}`}>
                  {n}
                </button>
              ))}
            </div>
          )}
          <input type="text" value={importName} onChange={(e) => setImportName(e.target.value)}
            placeholder="留空则自动使用 Danbooru 标签名"
            className="w-full border rounded px-2 py-1 text-xs" />
        </div>
        <div className="flex items-center gap-2">
          <button onClick={() => onImportAll(importName)} disabled={importAllPending}
            className="flex-1 px-4 py-1.5 bg-blue-600 text-white rounded text-sm hover:bg-blue-700 disabled:opacity-50">
            {importAllPending ? t("danbooru.importing") : t("danbooru.import_all_subscribe")}
          </button>
        </div>
        {importAllPending && (
          <div className="mt-2">
            <div className="w-full bg-blue-200 dark:bg-blue-800 rounded-full h-1.5 overflow-hidden">
              <div className="bg-blue-600 h-1.5 rounded-full animate-pulse" style={{ width: "100%" }} />
            </div>
          </div>
        )}
        {importAllError && <p className="text-red-600 text-xs mt-2">{importAllError}</p>}
      </div>

      <div className="bg-white dark:bg-slate-800 border rounded-lg p-4">
        {/* Downloadable URLs with Subscribe buttons */}
        {downloadableUrls.length > 0 && (
          <div className="mt-3">
            <h4 className="text-xs font-medium text-gray-500 dark:text-gray-400 mb-2">{t("danbooru.downloadable_sources").replace("{count}", String(downloadableUrls.length))}</h4>
            <div className="space-y-2">
              {downloadableUrls.map((u, i) => (
                <div key={i} className="flex items-center justify-between bg-green-50 border border-green-200 rounded p-2 text-xs">
                  <div className="flex items-center gap-2">
                    <SourceBadge source={classifyUrl(u.normalized_url)} />
                    <a href={u.normalized_url} target="_blank" rel="noopener noreferrer"
                      className="text-blue-600 hover:underline truncate max-w-md">{u.normalized_url}</a>
                  </div>
                  <div className="flex items-center gap-2">
                    <select value={selectedCreator} onChange={(e) => setSelectedCreator(e.target.value)}
                      className="border rounded px-2 py-1 text-xs">
                      <option value="">{t("danbooru.select_creator")}</option>
                      {creators.data?.map((c) => <option key={c.id} value={c.id}>{c.display_name || c.name}</option>)}
                    </select>
                    <button
                      onClick={() => {
                        if (!selectedCreator) return;
                        setSubscribingUrl(u.normalized_url);
                        const src = classifyUrl(u.normalized_url);
                        // Extract user ID from Pixiv URL
                        let srcCreatorId: string | undefined;
                        const m = u.normalized_url.match(/pixiv\.net\/(?:en\/)?users\/(\d+)/);
                        if (m) srcCreatorId = m[1];
                        subscribe.mutate({ creatorId: selectedCreator, url: u.normalized_url, source: src, sourceCreatorId: srcCreatorId });
                      }}
                      disabled={!selectedCreator || subscribe.isPending}
                      className="px-2 py-1 bg-green-600 text-white rounded text-xs hover:bg-green-700 disabled:opacity-50 shrink-0">
                      {subscribingUrl === u.normalized_url && subscribe.isPending ? "..." : t("danbooru.subscribe")}
                    </button>
                  </div>
                </div>
              ))}
            </div>
          </div>
        )}

        {/* Other URLs */}
        {artist.urls.filter(u => !DOWNLOADABLE_SOURCES.includes(classifyUrl(u.normalized_url))).length > 0 && (
          <div className="mt-3">
            <h4 className="text-xs font-medium text-gray-500 dark:text-gray-400 mb-1">{t("danbooru.other_urls")}</h4>
            <div className="space-y-1">
              {artist.urls.filter(u => !DOWNLOADABLE_SOURCES.includes(classifyUrl(u.normalized_url))).map((u, i) => (
                <div key={i} className="flex items-center gap-2 text-xs">
                  {!u.is_active && <span className="text-yellow-500">[inactive]</span>}
                  <a href={u.normalized_url} target="_blank" rel="noopener noreferrer"
                    className="text-blue-600 hover:underline truncate max-w-lg">{u.normalized_url}</a>
                </div>
              ))}
            </div>
          </div>
        )}

        {artist.urls.length === 0 && (
          <p className="text-xs text-gray-400 dark:text-gray-500 mt-3">{t("danbooru.no_urls")}</p>
        )}
      </div>

      {/* Import all links */}
      {links.length > 0 && (
        <div className="bg-white dark:bg-slate-800 border rounded-lg p-4">
          <h3 className="font-medium mb-2">{t("danbooru.import_links_count").replace("{count}", String(links.length))}</h3>
          <p className="text-xs text-gray-500 dark:text-gray-400 mb-3">Creates creator_link records for all of this artist's associated URLs in Danbooru.</p>
          <div className="space-y-2 mb-4">
            {links.map((l, i) => (
              <div key={i} className="flex items-center gap-2 text-xs border-b dark:border-slate-700 pb-2">
                <SourceBadge source={l.link_type} />
                <a href={l.url} target="_blank" rel="noopener noreferrer"
                  className="text-blue-600 hover:underline truncate max-w-md">{l.url}</a>
                <span className="text-gray-400 dark:text-gray-500">confidence: {l.confidence.toFixed(1)}</span>
              </div>
            ))}
          </div>
          <div className="flex items-end gap-3">
            <div className="flex-1">
              <label className="block text-xs font-medium mb-1">{t("danbooru.target_creator")}</label>
              <select value={selectedCreator} onChange={(e) => setSelectedCreator(e.target.value)}
                className="w-full border rounded px-3 py-2 text-sm">
                <option value="">{t("danbooru.select_creator")}</option>
                {creators.data?.map((c) => <option key={c.id} value={c.id}>{c.display_name || c.name}</option>)}
              </select>
            </div>
            <button onClick={() => onImport(selectedCreator)} disabled={!selectedCreator || importPending}
              className="px-4 py-2 text-sm bg-slate-900 dark:bg-slate-700 text-white rounded hover:bg-slate-800 dark:hover:bg-slate-600 disabled:opacity-50 shrink-0">
              {importPending ? t("danbooru.importing") : t("danbooru.import_links_btn").replace("{count}", String(links.length))}
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

function classifyUrl(url: string): string {
  const u = url.toLowerCase();
  if (u.includes("sketch.pixiv.net")) return "pixiv_sketch";
  if (u.includes("pixiv.net/stacc")) return "pixiv_stacc";
  if (u.includes("pixiv.net")) return "pixiv";
  if (u.includes("twitter.com") || u.includes("x.com")) return "x";
  if (u.includes("bsky.app")) return "bluesky";
  if (u.includes("iwara.tv")) return "iwara";
  if (u.includes("youtube.com") || u.includes("youtu.be")) return "youtube";
  if (u.includes("bilibili.com")) return "bilibili";
  if (u.includes("danbooru")) return "danbooru";
  if (u.includes("deviantart.com")) return "deviantart";
  if (u.includes("artstation.com")) return "artstation";
  if (u.includes("weibo.com") || u.includes("weibo.cn")) return "weibo";
  if (u.includes("xiaohongshu.com")) return "xiaohongshu";
  if (u.includes("fanbox")) return "fanbox";
  if (u.includes("skeb.jp")) return "skeb";
  if (u.includes("patreon.com")) return "patreon";
  if (u.includes("instagram.com")) return "instagram";
  if (u.includes("tumblr.com")) return "tumblr";
  if (u.includes("tiktok.com")) return "tiktok";
  if (u.includes("nicovideo.jp")) return "nicovideo";
  if (u.includes("fantia.jp")) return "fantia";
  return "website";
}

const DOWNLOADABLE_SOURCES = ["pixiv", "iwara"];

export default function DanbooruReferencePage() {
  const t = useT();
  const toast = useToast();
  const [searchUrl, setSearchUrl] = useState("");
  const [searchName, setSearchName] = useState("");
  const [searchPixivId, setSearchPixivId] = useState("");
  const [selectedCreator, setSelectedCreator] = useState("");
  const [importName, setImportName] = useState("");
  const [searchParams, setSearchParams] = useState<{ url?: string; pixiv_id?: string; name?: string } | null>(null);
  const [batchInput, setBatchInput] = useState("");
  const [showBatch, setShowBatch] = useState(false);

  const preview = useQuery({
    queryKey: ["danbooru-preview", searchParams],
    queryFn: () => api.previewDanbooruArtist(searchParams!),
    enabled: !!searchParams,
  });

  const qc = useQueryClient();
  const importMutation = useMutation({
    mutationFn: (creatorId: string) => api.importDanbooruArtist({ creator_id: creatorId, ...searchParams }),
    onSuccess: (data) => {
      toast.info(`Imported ${data.imported} links from Danbooru artist "${data.artist_name}"`);
      qc.invalidateQueries({ queryKey: queryKeys.creators.all });
    },
  });

  const importAllMutation = useMutation({
    mutationFn: (creatorName: string) => api.importAllDanbooru({
      creator_name: creatorName || undefined,
      ...searchParams,
    }),
    onSuccess: (data) => {
      if (!data.found) { toast.info("No matching Danbooru artist found."); return; }
      qc.invalidateQueries({ queryKey: queryKeys.creators.all });
      qc.invalidateQueries({ queryKey: queryKeys.subscriptions.all });
      toast.info(`Done! Creator: ${data.creator_id?.slice(0, 8)}... | Links: ${data.links_imported} | Sources: ${data.sources_created}`);
    },
  });

  const [batchJobId, setBatchJobId] = useState<string | null>(null);

  const enqueueBatch = useMutation({
    mutationFn: (ids: string[]) => api.batchImportDanbooru(ids),
    onSuccess: (data) => {
      setBatchJobId(data.job_id);
    },
  });

  // URL batch import (synchronous, returns per-URL results directly)
  const [urlBatchInput, setUrlBatchInput] = useState("");
  const [showUrlBatch, setShowUrlBatch] = useState(false);
  const urlBatchImport = useMutation({
    mutationFn: (urls: string[]) => api.urlBatchImportDanbooru(urls),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: queryKeys.creators.all });
      qc.invalidateQueries({ queryKey: queryKeys.subscriptions.all });
    },
  });

  const handleUrlBatchSubmit = () => {
    const urls = urlBatchInput
      .split(/\n/)
      .map((s) => s.trim())
      .filter((s) => s.startsWith("http"));
    if (urls.length === 0) return;
    urlBatchImport.mutate(urls);
  };

  // Poll for batch results while a job is running
  const batchStatus = useQuery({
    queryKey: ["batch-import-status", batchJobId],
    queryFn: () => api.getBatchImportStatus(batchJobId || undefined),
    enabled: !!batchJobId,
    refetchInterval: (query) => query.state.data?.status === "completed" ? false : 2000,
    // Keep previous data hidden when switching to a new job
    placeholderData: undefined,
  });

  // Only use results from the current job (guard against stale Redis data)
  const batchResult = batchJobId ? batchStatus.data?.result : null;
  const batchProgress = batchJobId ? batchStatus.data?.progress : null;

  useEffect(() => {
    if (batchResult) {
      qc.invalidateQueries({ queryKey: queryKeys.creators.all });
      qc.invalidateQueries({ queryKey: queryKeys.subscriptions.all });
    }
  }, [batchResult, qc]);

  const handleBatchSubmit = () => {
    const ids = batchInput
      .split(/[\n,]+/)
      .map((s) => s.trim())
      .filter((s) => /^\d+$/.test(s));
    if (ids.length === 0) return;
    // Clear old state and query cache before starting new batch
    setBatchJobId(null);
    qc.removeQueries({ queryKey: ["batch-import-status"] });
    enqueueBatch.mutate(ids);
  };

  const handleSearch = (type: "url" | "name" | "pixiv_id") => {
    const params: Record<string, string> = {};
    if (type === "url" && searchUrl.trim()) params.url = searchUrl.trim();
    if (type === "name" && searchName.trim()) params.name = searchName.trim();
    if (type === "pixiv_id" && searchPixivId.trim()) params.pixiv_id = searchPixivId.trim();
    if (Object.keys(params).length === 0) return;
    setSearchParams(params);
  };

  const artist = preview.data?.artist;
  const links = preview.data?.suggested_links || [];

  // Auto-fill importName from Pixiv display name when preview data arrives
  useEffect(() => {
    if (artist) {
      setImportName(artist.pixiv_display_name || "");
    }
  }, [artist]);

  return (
    <main className="max-w-5xl mx-auto p-6">
      <PageHeader title={t("danbooru.title")} description={t("danbooru.desc")} />

      <div className="bg-blue-50 dark:bg-blue-900/30 border border-blue-200 dark:border-blue-800 rounded-lg p-4 text-sm mb-6">
        <p className="font-medium text-blue-800 dark:text-blue-300 mb-1">{t("danbooru.search_info")}</p>
        <p className="text-blue-700 dark:text-blue-300">Danbooru artists map source profiles (Pixiv, Twitter, Iwara, etc.) to a canonical name. Use any of the three methods below to find the matching Danbooru artist record.</p>
      </div>

      {/* URL Batch Import */}
      <div className="bg-white dark:bg-slate-800 rounded-lg shadow p-4 mb-6">
        <div className="flex items-center justify-between mb-3">
          <h3 className="font-medium text-sm">{t("danbooru.url_batch_title")}</h3>
          <button onClick={() => setShowUrlBatch(!showUrlBatch)} className="text-xs text-blue-600 hover:underline">
            {showUrlBatch ? t("danbooru.batch_collapse") : t("danbooru.batch_expand")}
          </button>
        </div>
        {!showUrlBatch && (
          <p className="text-xs text-gray-500 dark:text-gray-400">
            Paste multiple creator profile URLs (Pixiv, Twitter/X, Iwara, etc.) to bulk import via Danbooru. Results are shown immediately per URL.
          </p>
        )}
        {showUrlBatch && (
          <div className="space-y-3">
            <p className="text-xs text-gray-500 dark:text-gray-400">
              Enter one URL per line. Supported: Pixiv, Twitter/X, Iwara, and other Danbooru-indexed profile URLs.
            </p>
            <textarea
              value={urlBatchInput}
              onChange={(e) => setUrlBatchInput(e.target.value)}
              placeholder={"https://www.pixiv.net/users/123456\nhttps://twitter.com/artist_name\nhttps://www.iwara.tv/profile/..."}
              rows={5}
              className="w-full border rounded px-3 py-2 text-sm font-mono resize-y"
            />
            <div className="flex items-center gap-3">
              <button
                onClick={handleUrlBatchSubmit}
                disabled={urlBatchImport.isPending || !urlBatchInput.trim()}
                className="px-4 py-2 bg-indigo-600 text-white rounded text-sm hover:bg-indigo-700 disabled:opacity-50"
              >
                {urlBatchImport.isPending ? t("danbooru.processing") : t("danbooru.url_batch_import")}
              </button>
              <span className="text-xs text-gray-400">
                {urlBatchInput.trim() ? `${urlBatchInput.split("\n").filter((s) => s.trim().startsWith("http")).length} URLs` : ""}
              </span>
              {urlBatchImport.data && (
                <button onClick={() => { urlBatchImport.reset(); setUrlBatchInput(""); }} className="text-xs text-blue-600 hover:underline">{t("common.close")}</button>
              )}
            </div>
            {urlBatchImport.error && (
              <p className="text-red-600 text-sm">{(urlBatchImport.error as Error).message}</p>
            )}
            {urlBatchImport.data && (
              <div className="mt-4 space-y-3">
                {/* Summary */}
                <div className="grid grid-cols-3 gap-3">
                  <div className="bg-green-50 dark:bg-green-900/30 border border-green-200 dark:border-green-800 rounded p-3 text-center">
                    <div className="text-xl font-bold text-green-700 dark:text-green-400">{urlBatchImport.data.imported}</div>
                    <div className="text-xs text-green-600">{t("danbooru.batch_result_imported")}</div>
                  </div>
                  <div className="bg-gray-50 dark:bg-slate-800/50 border border-gray-200 dark:border-slate-700 rounded p-3 text-center">
                    <div className="text-xl font-bold text-gray-600 dark:text-gray-400">{urlBatchImport.data.not_found}</div>
                    <div className="text-xs text-gray-500">{t("danbooru.batch_result_not_found")}</div>
                  </div>
                  <div className="bg-red-50 dark:bg-red-900/30 border border-red-200 dark:border-red-800 rounded p-3 text-center">
                    <div className="text-xl font-bold text-red-700 dark:text-red-400">{urlBatchImport.data.errors}</div>
                    <div className="text-xs text-red-600">{t("danbooru.batch_result_errors")}</div>
                  </div>
                </div>
                {/* Per-URL results */}
                <div className="space-y-1 max-h-72 overflow-y-auto">
                  {urlBatchImport.data.results.map((r, i) => (
                    <div key={i} className={`rounded p-2 text-xs flex items-start justify-between gap-2 border ${
                      r.status === "imported" ? "bg-green-50 dark:bg-green-900/20 border-green-100 dark:border-green-900" :
                      r.status === "not_found" ? "bg-gray-50 dark:bg-slate-800/50 border-gray-200 dark:border-slate-700" :
                      "bg-red-50 dark:bg-red-900/20 border-red-100 dark:border-red-900"
                    }`}>
                      <div className="min-w-0 flex-1">
                        <div className="font-mono text-gray-600 dark:text-gray-400 truncate">{r.url}</div>
                        {r.artist_name && (
                          <div className="mt-1 font-medium text-gray-800 dark:text-gray-200">{r.artist_name}</div>
                        )}
                        {r.message && (
                          <div className="mt-1 text-gray-500">{r.message}</div>
                        )}
                      </div>
                      <div className="shrink-0 text-right">
                        {r.status === "imported" && (
                          <span className="text-green-600">
                            {r.created_new ? "New" : "Exists"} · {r.links_imported} links · {r.sources_created} src
                          </span>
                        )}
                        {r.status === "not_found" && <span className="text-gray-400">Not found</span>}
                        {r.status === "error" && <span className="text-red-500">Error</span>}
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            )}
          </div>
        )}
      </div>

      {/* Batch Import */}
      <div className="bg-white dark:bg-slate-800 rounded-lg shadow p-4 mb-6">
        <div className="flex items-center justify-between mb-3">
          <h3 className="font-medium text-sm">{t("danbooru.batch_title")}</h3>
          <button onClick={() => setShowBatch(!showBatch)}
            className="text-xs text-blue-600 hover:underline">
            {showBatch ? t("danbooru.batch_collapse") : t("danbooru.batch_expand")}
          </button>
        </div>
        {!showBatch && (
          <p className="text-xs text-gray-500 dark:text-gray-400">
            Paste multiple Pixiv user IDs to bulk import via Danbooru. High-confidence matches are auto-imported; low-confidence and not-found entries are flagged for manual review.
          </p>
        )}
        {showBatch && (
          <div className="space-y-3">
            <p className="text-xs text-gray-500 dark:text-gray-400">
              Enter one Pixiv user ID per line, or comma-separated. Valid IDs are numeric (e.g., 1980643).
            </p>
            <textarea
              value={batchInput}
              onChange={(e) => setBatchInput(e.target.value)}
              placeholder={t("danbooru.batch_placeholder")}
              rows={5}
              className="w-full border rounded px-3 py-2 text-sm font-mono resize-y"
            />
            <div className="flex items-center gap-3">
              <button
                onClick={handleBatchSubmit}
                disabled={enqueueBatch.isPending || (!!batchJobId && !batchResult) || !batchInput.trim()}
                className="px-4 py-2 bg-blue-600 text-white rounded text-sm hover:bg-blue-700 disabled:opacity-50"
              >
                {enqueueBatch.isPending ? "..." : (!!batchJobId && !batchResult) ? t("danbooru.processing") : t("danbooru.batch_import")}
              </button>
              <span className="text-xs text-gray-400">
                {batchInput.trim() ? `${batchInput.split(/[\n,]+/).filter((s: string) => /^\d+$/.test(s.trim())).length} ${t("danbooru.valid_ids")}` : ""}
              </span>
              {enqueueBatch.data?.duplicates_removed ? (
                <span className="text-xs text-yellow-600">({enqueueBatch.data.duplicates_removed} duplicates removed)</span>
              ) : null}
              {enqueueBatch.data?.already_exists && enqueueBatch.data.already_exists.length > 0 && (
                <div className="mt-2 p-2 bg-yellow-50 dark:bg-yellow-900/20 border border-yellow-200 dark:border-yellow-800 rounded text-xs">
                  <span className="text-yellow-700 dark:text-yellow-400 font-medium">{enqueueBatch.data.already_exists.length} ID(s) already have creators: </span>
                  {enqueueBatch.data.already_exists.map((e, i) => (
                    <span key={e.pixiv_id} className="text-yellow-600">
                      {i > 0 && ", "}
                      <span className="font-mono">Pixiv {e.pixiv_id}</span> → {e.creator_name}
                    </span>
                  ))}
                </div>
              )}
              {batchResult && (
                <button onClick={() => { setBatchJobId(null); }} className="text-xs text-blue-600 hover:underline">{t("common.close")}</button>
              )}
            </div>
            {enqueueBatch.error && (
              <p className="text-red-600 text-sm">{(enqueueBatch.error as Error).message}</p>
            )}

            {/* Progress bar */}
            {batchProgress && !batchResult && (
              <div className="mt-3">
                <div className="flex justify-between text-xs text-gray-500 mb-1">
                  <span>Processing {batchProgress.current}/{batchProgress.total}</span>
                  <span>{batchProgress.imported} imported, {batchProgress.errors} errors</span>
                </div>
                <div className="w-full bg-gray-200 dark:bg-slate-700 rounded-full h-2">
                  <div className="bg-blue-600 h-2 rounded-full transition-all" style={{ width: `${(batchProgress.current / batchProgress.total) * 100}%` }} />
                </div>
              </div>
            )}

            {/* Results */}
            {batchResult && (
              <div className="mt-4 space-y-4">
                {/* Summary */}
                <div className="grid grid-cols-4 gap-3">
                  <div className="bg-green-50 dark:bg-green-900/30 border border-green-200 dark:border-green-800 rounded p-3 text-center">
                    <div className="text-xl font-bold text-green-700 dark:text-green-400">{batchResult.imported_count}</div>
                    <div className="text-xs text-green-600">{t("danbooru.batch_result_imported")}</div>
                  </div>
                  <div className="bg-yellow-50 dark:bg-yellow-900/30 border border-yellow-200 dark:border-yellow-800 rounded p-3 text-center">
                    <div className="text-xl font-bold text-yellow-700 dark:text-yellow-400">{batchResult.low_confidence_count}</div>
                    <div className="text-xs text-yellow-600">{t("danbooru.batch_result_low_confidence")}</div>
                  </div>
                  <div className="bg-red-50 dark:bg-red-900/30 border border-red-200 dark:border-red-800 rounded p-3 text-center">
                    <div className="text-xl font-bold text-red-700 dark:text-red-400">{batchResult.not_found_count}</div>
                    <div className="text-xs text-red-600">{t("danbooru.batch_result_not_found")}</div>
                  </div>
                  <div className="bg-gray-50 dark:bg-slate-800/50 border border-gray-200 dark:border-slate-700 rounded p-3 text-center">
                    <div className="text-xl font-bold text-gray-600 dark:text-gray-400">{batchResult.error_count}</div>
                    <div className="text-xs text-gray-500">{t("danbooru.batch_result_errors")}</div>
                  </div>
                </div>

                {/* Imported list */}
                {batchResult.imported.length > 0 && (
                  <div>
                    <h4 className="text-sm font-medium text-green-700 dark:text-green-400 mb-2">{t("danbooru.batch_result_imported")} ({batchResult.imported.length})</h4>
                    <div className="space-y-1 max-h-48 overflow-y-auto">
                      {batchResult.imported.map((r: any, i: number) => (
                        <div key={i} className="bg-green-50 dark:bg-green-900/20 border border-green-100 dark:border-green-900 rounded p-2 text-xs flex items-center justify-between">
                          <div>
                            <span className="font-mono text-green-800 dark:text-green-300">Pixiv {r.pixiv_id}</span>
                            <span className="text-green-600 mx-2">→</span>
                            <span className="font-medium">{r.artist_name}</span>
                            <span className="text-gray-500 dark:text-gray-400 ml-2">Danbooru #{r.artist_id}</span>
                          </div>
                          <div className="text-green-600 shrink-0">
                            {r.links_imported} links · {r.sources_created} sources
                          </div>
                        </div>
                      ))}
                    </div>
                  </div>
                )}

                {/* Low confidence list */}
                {batchResult.low_confidence.length > 0 && (
                  <div>
                    <h4 className="text-sm font-medium text-yellow-700 dark:text-yellow-400 mb-2">{t("danbooru.batch_result_low_confidence")} — Manual Review ({batchResult.low_confidence.length})</h4>
                    <p className="text-xs text-yellow-600 mb-2">Found Danbooru artist but no downloadable source URLs. Use individual search below to review and manually subscribe.</p>
                    <div className="space-y-1 max-h-48 overflow-y-auto">
                      {batchResult.low_confidence.map((r: any, i: number) => (
                        <div key={i} className="bg-yellow-50 dark:bg-yellow-900/20 border border-yellow-100 dark:border-yellow-900 rounded p-2 text-xs flex items-center justify-between">
                          <div>
                            <span className="font-mono">Pixiv {r.pixiv_id}</span>
                            <span className="mx-2">→</span>
                            <span className="font-medium">{r.artist_name}</span>
                            <span className="text-gray-500 dark:text-gray-400 ml-2">Danbooru #{r.artist_id}</span>
                          </div>
                          <div className="text-yellow-600 shrink-0">{r.url_count} URLs · {r.message}</div>
                        </div>
                      ))}
                    </div>
                  </div>
                )}

                {/* Not found list */}
                {batchResult.not_found.length > 0 && (
                  <div>
                    <h4 className="text-sm font-medium text-red-700 dark:text-red-400 mb-2">{t("danbooru.batch_result_not_found")} ({batchResult.not_found.length})</h4>
                    <p className="text-xs text-red-600 mb-2">No matching Danbooru artist. May need manual creator creation and source linking.</p>
                    <div className="flex flex-wrap gap-2">
                      {batchResult.not_found.map((r: any, i: number) => (
                        <span key={i} className="bg-red-50 dark:bg-red-900/20 border border-red-100 dark:border-red-900 rounded px-2 py-1 text-xs font-mono text-red-700 dark:text-red-400">
                          Pixiv {r.pixiv_id}
                        </span>
                      ))}
                    </div>
                  </div>
                )}
              </div>
            )}
          </div>
        )}
      </div>

      <div className="grid grid-cols-1 md:grid-cols-3 gap-4 mb-6">
        <div className="bg-white dark:bg-slate-800 rounded-lg shadow p-4">
          <h3 className="font-medium mb-1 text-sm">{t("danbooru.search_url_title")}</h3>
          <p className="text-xs text-gray-500 dark:text-gray-400 mb-3">{t("danbooru.search_url_desc")}</p>
          <input value={searchUrl} onChange={(e) => setSearchUrl(e.target.value)}
            placeholder={t("danbooru.url_placeholder")}
            className="w-full border rounded px-3 py-2 text-sm mb-2" />
          <button onClick={() => handleSearch("url")} disabled={!searchUrl.trim()}
            className="w-full px-3 py-2 bg-slate-900 dark:bg-slate-700 text-white rounded text-sm hover:bg-slate-800 dark:hover:bg-slate-600 disabled:opacity-50">
            {t("danbooru.search_btn")}
          </button>
        </div>

        <div className="bg-white dark:bg-slate-800 rounded-lg shadow p-4">
          <h3 className="font-medium mb-1 text-sm">{t("danbooru.search_pixiv_title")}</h3>
          <p className="text-xs text-gray-500 dark:text-gray-400 mb-3">{t("danbooru.search_pixiv_desc")}</p>
          <input value={searchPixivId} onChange={(e) => setSearchPixivId(e.target.value)}
            placeholder={t("danbooru.pixiv_id_placeholder")}
            className="w-full border rounded px-3 py-2 text-sm mb-2" />
          <button onClick={() => handleSearch("pixiv_id")} disabled={!searchPixivId.trim()}
            className="w-full px-3 py-2 bg-slate-900 dark:bg-slate-700 text-white rounded text-sm hover:bg-slate-800 dark:hover:bg-slate-600 disabled:opacity-50">
            {t("danbooru.search_btn")}
          </button>
        </div>

        <div className="bg-white dark:bg-slate-800 rounded-lg shadow p-4">
          <h3 className="font-medium mb-1 text-sm">{t("danbooru.search_name_title")}</h3>
          <p className="text-xs text-gray-500 dark:text-gray-400 mb-3">{t("danbooru.search_name_desc")}</p>
          <input value={searchName} onChange={(e) => setSearchName(e.target.value)}
            placeholder={t("danbooru.name_placeholder")}
            className="w-full border rounded px-3 py-2 text-sm mb-2" />
          <button onClick={() => handleSearch("name")} disabled={!searchName.trim()}
            className="w-full px-3 py-2 bg-slate-900 dark:bg-slate-700 text-white rounded text-sm hover:bg-slate-800 dark:hover:bg-slate-600 disabled:opacity-50">
            {t("danbooru.search_btn")}
          </button>
        </div>
      </div>

      {preview.isLoading && <div className="bg-white dark:bg-slate-800 rounded-lg shadow p-4 animate-pulse"><div className="h-24 bg-gray-100 dark:bg-slate-700 rounded" /></div>}
      {preview.error && <ErrorState message={(preview.error as Error).message} />}
      {preview.data && !preview.data.found && (
        <div className="bg-white dark:bg-slate-800 rounded-lg shadow p-4">
          <EmptyState title={t("danbooru.no_match")} description={preview.data.message || "No matching Danbooru artist found."} />
        </div>
      )}

      {artist && (
        <PreviewResult artist={artist} links={links}
          onImport={(creatorId) => importMutation.mutate(creatorId)}
          importPending={importMutation.isPending}
          onImportAll={(creatorName) => importAllMutation.mutate(creatorName)}
          importAllPending={importAllMutation.isPending}
          importAllError={(importAllMutation.error as Error)?.message || null}
          selectedCreator={selectedCreator} setSelectedCreator={setSelectedCreator}
          importName={importName} setImportName={setImportName} />
      )}
    </main>
  );
}

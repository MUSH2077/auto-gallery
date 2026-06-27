"use client";
import { useState, useEffect, useRef, useCallback } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api, queryKeys } from "@/lib/api";
import { useToast } from "@/components/Toast";
import { useNotifications } from "@/components/NotificationCenter";
import { useT } from "@/lib/i18n";
import { PageHeader, EmptyState, ErrorState, SourceBadge } from "@/components";

function CopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <button
      onClick={() => { navigator.clipboard.writeText(text).then(() => { setCopied(true); setTimeout(() => setCopied(false), 1500); }); }}
      className="text-xs px-2 py-1 rounded border border-border hover:bg-subtle dark:hover:bg-subtle transition-colors inline-flex items-center gap-1"
    >
      {copied ? "✓ Copied" : "📋 Copy"}
    </button>
  );
}

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
      toast.success({ title: t("notification.created"), message: "Subscription source created! Trigger a sync from the Subscriptions page." });
    },
    onError: (err) => {
      toast.error({ message: (err as Error).message });
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
      <div className="card p-4">
        <h3 className="font-medium mb-2">{t("danbooru.artist_label").replace("{id}", String(artist.id)).replace("{name}", artist.name)}</h3>
        {artist.other_names.length > 0 && (
          <div className="flex flex-wrap items-center gap-1 mb-1">
            <span className="text-xs text-muted shrink-0">{t("danbooru.also_known")}</span>
            {artist.other_names.map((n) => (
              <button key={n} type="button" onClick={() => setImportName(n)}
                className={`text-xs px-2 py-0.5 rounded-full border transition-colors cursor-pointer
                  ${importName === n
                    ? "bg-blue-600 text-white border-blue-600"
                    : "bg-subtle text-fg border-border hover:bg-blue-50 hover:border-blue-400 dark:hover:bg-blue-900/30"}`}>
                {n}
              </button>
            ))}
          </div>
        )}
        {artist.post_count != null && <p className="text-xs text-muted">{t("danbooru.posts_count")} {artist.post_count}</p>}
        {artist.notes && <p className="text-xs text-fg mt-2 bg-subtle p-2 rounded">{artist.notes}</p>}
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
                      : "bg-subtle text-fg border-border hover:bg-blue-50 hover:border-blue-400 dark:hover:bg-blue-900/30"}`}>
                  {n}
                </button>
              ))}
            </div>
          )}
          <input type="text" value={importName} onChange={(e) => setImportName(e.target.value)}
            placeholder="留空则自动使用 Danbooru 标签名"
            className="input w-full px-2 py-1 text-xs" />
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

      <div className="card p-4">
        {/* Downloadable URLs with Subscribe buttons */}
        {downloadableUrls.length > 0 && (
          <div className="mt-3">
            <h4 className="text-xs font-medium text-muted mb-2">{t("danbooru.downloadable_sources").replace("{count}", String(downloadableUrls.length))}</h4>
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
                      className="select px-2 py-1 text-xs">
                      <option value="">{t("danbooru.select_creator")}</option>
                      {creators.data?.items.map((c) => <option key={c.id} value={c.id}>{c.display_name || c.name}</option>)}
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
            <h4 className="text-xs font-medium text-muted mb-1">{t("danbooru.other_urls")}</h4>
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
          <p className="text-xs text-muted mt-3">{t("danbooru.no_urls")}</p>
        )}
      </div>

      {/* Import all links */}
      {links.length > 0 && (
        <div className="card p-4">
          <h3 className="font-medium mb-2">{t("danbooru.import_links_count").replace("{count}", String(links.length))}</h3>
          <p className="text-xs text-muted mb-3">Creates creator_link records for all of this artist's associated URLs in Danbooru.</p>
          <div className="space-y-2 mb-4">
            {links.map((l, i) => (
              <div key={i} className="flex items-center gap-2 text-xs border-b border-border pb-2">
                <SourceBadge source={l.link_type} />
                <a href={l.url} target="_blank" rel="noopener noreferrer"
                  className="text-blue-600 hover:underline truncate max-w-md">{l.url}</a>
                <span className="text-muted">confidence: {l.confidence.toFixed(1)}</span>
              </div>
            ))}
          </div>
          <div className="flex items-end gap-3">
            <div className="flex-1">
              <label className="block text-xs font-medium mb-1">{t("danbooru.target_creator")}</label>
              <select value={selectedCreator} onChange={(e) => setSelectedCreator(e.target.value)}
                className="select w-full">
                <option value="">{t("danbooru.select_creator")}</option>
                {creators.data?.items.map((c) => <option key={c.id} value={c.id}>{c.display_name || c.name}</option>)}
              </select>
            </div>
            <button onClick={() => onImport(selectedCreator)} disabled={!selectedCreator || importPending}
              className="btn-primary shrink-0">
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
  const notify = useNotifications();
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
      toast.success({ message: `Imported ${data.imported} links from Danbooru artist "${data.artist_name}"` });
      qc.invalidateQueries({ queryKey: queryKeys.creators.all });
    },
  });

  const importAllMutation = useMutation({
    mutationFn: (creatorName: string) => api.importAllDanbooruAsync({
      creator_name: creatorName || undefined,
      ...searchParams,
    }),
    onSuccess: (data) => {
      notify.startOperationJob(data.job_id, "danbooru-import-all", "Danbooru import", {
        name: searchParams?.name,
        pixiv_id: searchParams?.pixiv_id,
      });
      toast.success({ message: "Danbooru import queued. You can leave this page; progress will continue in notifications." });
    },
  });

  // Batch job state is managed globally by NotificationCenter (layout level).
  // Polling runs in the context and survives all page navigation.
  const { batchJob } = notify;
  const importAllOperation = notify.operationJob?.kind === "danbooru-import-all" ? notify.operationJob : null;

  // Fallback: on mount, directly check if there's a stored batch job and fetch result.
  // This handles edge cases where context state isn't restored after navigation.
  const [directResult, setDirectResult] = useState<any>(null);
  useEffect(() => {
    try {
      const stored = sessionStorage.getItem("danbooru_batch_job");
      if (stored) {
        const { jobId, startedAt } = JSON.parse(stored);
        if (Date.now() - startedAt < 2 * 60 * 60 * 1000) {
          api.getBatchImportStatus(jobId).then((data: any) => {
            if (data?.result) setDirectResult(data.result);
            else if (data?.status === "completed") {
              // If completed but no result in response, keep polling once
              setTimeout(() => {
                api.getBatchImportStatus(jobId).then((d: any) => {
                  if (d?.result) setDirectResult(d.result);
                });
              }, 2000);
            }
          }).catch(() => {});
        }
      }
    } catch {}
  }, []);

  // Merge: prefer context batchJob, fall back to direct mount fetch
  const displayBatchResult = batchJob?.result || directResult;
  const displayBatchProgress = batchJob?.progress;

  const enqueueBatch = useMutation({
    mutationFn: (ids: string[]) => api.batchImportDanbooru(ids),
    onSuccess: (data) => {
      notify.startBatchJob(data.job_id, "pixiv", data.total);
    },
  });

  // ── Preview mutations (auto‑triggered on textarea blur) ──────────
  const [pixivPreview, setPixivPreview] = useState<{
    total: number; unique_count: number; duplicates_removed: number;
    duplicate_ids: string[]; new_count: number;
    already_exists: { pixiv_id: string; creator_name: string; creator_id: string }[];
  } | null>(null);
  const pixivPreviewMut = useMutation({
    mutationFn: (ids: string[]) => api.previewBatchImport(ids),
    onSuccess: setPixivPreview,
  });
  const pixivDebounce = useRef<ReturnType<typeof setTimeout>>();
  const handlePixivBlur = useCallback(() => {
    const ids = batchInput.split(/[\n,]+/).map((s) => s.trim()).filter((s) => /^\d+$/.test(s));
    if (ids.length === 0) { setPixivPreview(null); return; }
    clearTimeout(pixivDebounce.current);
    pixivDebounce.current = setTimeout(() => pixivPreviewMut.mutate(ids), 500);
  }, [batchInput, pixivPreviewMut]);

  const [urlBatchInput, setUrlBatchInput] = useState("");
  const [showUrlBatch, setShowUrlBatch] = useState(false);
  const urlBatchImport = useMutation({
    mutationFn: (urls: string[]) => api.urlBatchImportDanbooru(urls),
    onSuccess: (data) => {
      notify.startBatchJob(data.job_id, "url", data.total);
    },
  });

  const [urlPreview, setUrlPreview] = useState<{
    total: number; unique_count: number; duplicates_removed: number;
    duplicate_urls: string[];
  } | null>(null);
  const urlPreviewMut = useMutation({
    mutationFn: (urls: string[]) => api.previewUrlBatchImport(urls),
    onSuccess: setUrlPreview,
  });
  const urlDebounce = useRef<ReturnType<typeof setTimeout>>();
  const handleUrlBlur = useCallback(() => {
    const urls = urlBatchInput.split(/\n/).map((s) => s.trim()).filter((s) => s.startsWith("http"));
    if (urls.length === 0) { setUrlPreview(null); return; }
    clearTimeout(urlDebounce.current);
    urlDebounce.current = setTimeout(() => urlPreviewMut.mutate(urls), 500);
  }, [urlBatchInput, urlPreviewMut]);

  const handleUrlBatchSubmit = () => {
    const urls = urlBatchInput
      .split(/\n/)
      .map((s) => s.trim())
      .filter((s) => s.startsWith("http"));
    if (urls.length === 0) return;
    notify.clearBatchJob();
    urlBatchImport.mutate(urls);
  };

  const handleBatchSubmit = () => {
    const ids = batchInput
      .split(/[\n,]+/)
      .map((s) => s.trim())
      .filter((s) => /^\d+$/.test(s));
    if (ids.length === 0) return;
    notify.clearBatchJob();
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
      <div className="card p-4 mb-6">
        <div className="flex items-center justify-between mb-3">
          <h3 className="font-medium text-sm">{t("danbooru.url_batch_title")}</h3>
          <button onClick={() => setShowUrlBatch(!showUrlBatch)} className="text-xs text-blue-600 hover:underline">
            {showUrlBatch ? t("danbooru.batch_collapse") : t("danbooru.batch_expand")}
          </button>
        </div>
        {!showUrlBatch && (
          <p className="text-xs text-muted">
            Paste multiple creator profile URLs (Pixiv, Twitter/X, Iwara, etc.) to bulk import via Danbooru. Results are shown immediately per URL.
          </p>
        )}
        {showUrlBatch && (
          <div className="space-y-3">
            <p className="text-xs text-muted">
              Enter one URL per line. Supported: Pixiv, Twitter/X, Iwara, and other Danbooru-indexed profile URLs.
            </p>
            <textarea
              value={urlBatchInput}
              onChange={(e) => { setUrlBatchInput(e.target.value); setUrlPreview(null); }}
              onBlur={handleUrlBlur}
              placeholder={"https://www.pixiv.net/users/123456\nhttps://twitter.com/artist_name\nhttps://www.iwara.tv/profile/..."}
              rows={5}
              className="textarea w-full resize-y font-mono"
            />

            {urlPreview && (
              <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs bg-indigo-50 dark:bg-indigo-900/20 border border-indigo-150 dark:border-indigo-900 rounded p-2">
                <span className="text-muted">{urlPreview.total} input</span>
                <span className="text-muted">→</span>
                <span className="font-medium">{urlPreview.unique_count} unique</span>
                {urlPreview.duplicates_removed > 0 && (
                  <span className="text-yellow-600">({urlPreview.duplicates_removed} dupe)</span>
                )}
                <span className="ml-auto font-semibold text-indigo-700 dark:text-indigo-300">
                  {urlPreview.unique_count} ready to import
                </span>
              </div>
            )}

            <div className="flex items-center gap-3">
              <button
                onClick={handleUrlBatchSubmit}
                disabled={urlBatchImport.isPending || !urlBatchInput.trim()}
                className="px-4 py-2 bg-indigo-600 text-white rounded text-sm hover:bg-indigo-700 disabled:opacity-50"
              >
                {urlBatchImport.isPending ? t("danbooru.processing") : t("danbooru.url_batch_import")}
              </button>
              <span className="text-xs text-muted">
                {urlBatchInput.trim() ? `${urlBatchInput.split("\n").filter((s) => s.trim().startsWith("http")).length} URLs` : ""}
              </span>
              {urlBatchImport.data && (
                <button onClick={() => { urlBatchImport.reset(); setUrlBatchInput(""); }} className="text-xs text-blue-600 hover:underline">{t("common.close")}</button>
              )}
            </div>
            {urlBatchImport.error && (
              <p className="text-red-600 text-sm">{(urlBatchImport.error as Error).message}</p>
            )}
            {urlBatchImport.isSuccess && !displayBatchResult && (
              <p className="mt-2 text-xs text-blue-600 dark:text-blue-400">{t("danbooru.job_enqueued", "任务已入队，可在下方查看进度。")}</p>
            )}
          </div>
        )}
      </div>

      {/* Batch Import */}
      <div className="card p-4 mb-6">
        <div className="flex items-center justify-between mb-3">
          <h3 className="font-medium text-sm">{t("danbooru.batch_title")}</h3>
          <button onClick={() => setShowBatch(!showBatch)}
            className="text-xs text-blue-600 hover:underline">
            {showBatch ? t("danbooru.batch_collapse") : t("danbooru.batch_expand")}
          </button>
        </div>
        {!showBatch && (
          <p className="text-xs text-muted">
            Paste multiple Pixiv user IDs to bulk import via Danbooru. High-confidence matches are auto-imported; low-confidence and not-found entries are flagged for manual review.
          </p>
        )}
        {showBatch && (
          <div className="space-y-3">
            <p className="text-xs text-muted">
              Enter one Pixiv user ID per line, or comma-separated. Valid IDs are numeric (e.g., 1980643).
            </p>
            <textarea
              value={batchInput}
              onChange={(e) => { setBatchInput(e.target.value); setPixivPreview(null); }}
              onBlur={handlePixivBlur}
              placeholder={t("danbooru.batch_placeholder")}
              rows={5}
              className="textarea w-full resize-y font-mono"
            />

            {/* Preview summary */}
            {pixivPreview && (
              <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs bg-blue-50 dark:bg-blue-900/20 border border-blue-150 dark:border-blue-900 rounded p-2">
                <span className="text-muted">{pixivPreview.total} input</span>
                <span className="text-muted">→</span>
                <span className="font-medium">{pixivPreview.unique_count} unique</span>
                {pixivPreview.duplicates_removed > 0 && (
                  <span className="text-yellow-600">({pixivPreview.duplicates_removed} dupe)</span>
                )}
                {pixivPreview.already_exists.length > 0 && (
                  <span className="text-amber-600">({pixivPreview.already_exists.length} already exist)</span>
                )}
                <span className="ml-auto font-semibold text-blue-700 dark:text-blue-300">
                  {pixivPreview.new_count} ready to import
                </span>
              </div>
            )}

            {/* Already-exists detail */}
            {pixivPreview?.already_exists && pixivPreview.already_exists.length > 0 && (
              <details className="text-xs">
                <summary className="cursor-pointer text-amber-600 hover:text-amber-700">
                  {pixivPreview.already_exists.length} ID(s) already have local creators
                </summary>
                <div className="mt-1 space-y-0.5 pl-4 max-h-24 overflow-y-auto">
                  {pixivPreview.already_exists.map((e) => (
                    <div key={e.pixiv_id} className="text-amber-700 dark:text-amber-400">
                      <span className="font-mono">Pixiv {e.pixiv_id}</span> → {e.creator_name}
                    </div>
                  ))}
                </div>
              </details>
            )}

            <div className="flex items-center gap-3">
              <button
                onClick={handleBatchSubmit}
                disabled={enqueueBatch.isPending || (!!batchJob?.jobId && !displayBatchResult) || !batchInput.trim()}
                className="px-4 py-2 bg-blue-600 text-white rounded text-sm hover:bg-blue-700 disabled:opacity-50"
              >
                {enqueueBatch.isPending ? "..." : (!!batchJob?.jobId && !displayBatchResult) ? t("danbooru.processing") : t("danbooru.batch_import")}
              </button>
              <span className="text-xs text-muted">
                {batchInput.trim() ? `${batchInput.split(/[\n,]+/).filter((s: string) => /^\d+$/.test(s.trim())).length} ${t("danbooru.valid_ids")}` : ""}
              </span>
              {displayBatchResult && (
                <button onClick={() => { notify.clearBatchJob(); }} className="text-xs text-blue-600 hover:underline">{t("common.close")}</button>
              )}
            </div>
            {enqueueBatch.error && (
              <p className="text-red-600 text-sm">{(enqueueBatch.error as Error).message}</p>
            )}

            {/* Progress bar */}
            {displayBatchProgress && !displayBatchResult && (
              <div className="mt-3">
                <div className="flex justify-between text-xs text-muted mb-1">
                  <span>Processing {displayBatchProgress.current}/{displayBatchProgress.total}</span>
                  <span>{displayBatchProgress.imported} imported, {displayBatchProgress.errors} errors</span>
                </div>
                <div className="w-full bg-subtle rounded-full h-2">
                  <div className="bg-blue-600 h-2 rounded-full transition-all" style={{ width: `${((displayBatchProgress?.current || 0) / (displayBatchProgress?.total || 1)) * 100}%` }} />
                </div>
              </div>
            )}

            {/* Results */}
            {displayBatchResult && (
              <div className="mt-4 space-y-3">
                {/* Summary bar */}
                <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs bg-subtle border rounded p-2">
                  <span className="font-medium">{displayBatchResult.total} total</span>
                  <span className="text-muted">|</span>
                  {displayBatchResult.imported_count > 0 && (
                    <span className="text-green-600">🟢 {displayBatchResult.imported_count} imported</span>
                  )}
                  {displayBatchResult.low_confidence_count > 0 && (
                    <span className="text-yellow-600">🟡 {displayBatchResult.low_confidence_count} low confidence</span>
                  )}
                  {displayBatchResult.not_found_count > 0 && (
                    <span className="text-red-600">🔴 {displayBatchResult.not_found_count} not found</span>
                  )}
                  {displayBatchResult.error_count > 0 && (
                    <span className="text-red-700">💥 {displayBatchResult.error_count} errors</span>
                  )}
                </div>

                {/* Per‑result rows */}
                {[...displayBatchResult.imported, ...displayBatchResult.low_confidence].map((r: any, i: number) => {
                  const isLowConf = displayBatchResult.low_confidence.includes(r);
                  return (
                    <details key={i} className="bg-subtle border rounded p-2 text-xs">
                      <summary className="cursor-pointer flex items-center gap-2">
                        <span>{isLowConf ? "🟡" : "🟢"}</span>
                        <span className="font-mono">Pixiv {r.pixiv_id}</span>
                        <span className="text-muted">→</span>
                        <span className="font-medium">{r.artist_name}</span>
                        <span className="text-muted">Danbooru #{r.artist_id}</span>
                        {!isLowConf && (
                          <span className="ml-auto text-green-600">{r.links_imported} links · {r.sources_created} sources</span>
                        )}
                      </summary>
                      <div className="mt-1 pl-6 text-muted space-y-0.5">
                        {r.downloadable_urls && r.downloadable_urls.length > 0 && (
                          <div>Sources: {r.downloadable_urls.join(", ")}</div>
                        )}
                        {isLowConf && r.message && <div className="text-yellow-600">{r.message}</div>}
                      </div>
                    </details>
                  );
                })}

                {/* Not found — list with copy */}
                {displayBatchResult.not_found.length > 0 && (
                  <details className="bg-red-50 dark:bg-red-900/15 border border-red-200 dark:border-red-800 rounded p-2 text-xs">
                    <summary className="cursor-pointer font-medium text-red-700 dark:text-red-400 flex items-center gap-2">
                      🔴 Not found ({displayBatchResult.not_found.length})
                    </summary>
                    <div className="mt-2 space-y-1 pl-4 max-h-36 overflow-y-auto font-mono text-red-600">
                      {displayBatchResult.not_found.map((r: any, i: number) => (
                        <div key={i}>Pixiv {r.pixiv_id}{r.message ? ` — ${r.message}` : ""}</div>
                      ))}
                    </div>
                    <div className="mt-2">
                      <CopyButton text={displayBatchResult.not_found.map((r: any) => r.pixiv_id).join("\n")} />
                    </div>
                  </details>
                )}

                {/* Errors — list with copy */}
                {displayBatchResult.errors && displayBatchResult.errors.length > 0 && (
                  <details className="bg-red-50 dark:bg-red-900/15 border border-red-200 dark:border-red-800 rounded p-2 text-xs">
                    <summary className="cursor-pointer font-medium text-red-700 dark:text-red-400 flex items-center gap-2">
                      💥 Errors ({displayBatchResult.errors.length})
                    </summary>
                    <div className="mt-2 space-y-1 pl-4 max-h-36 overflow-y-auto font-mono text-red-600">
                      {displayBatchResult.errors.map((r: any, i: number) => (
                        <div key={i}>Pixiv {r.pixiv_id}{r.error ? ` — ${r.error}` : ""}</div>
                      ))}
                    </div>
                    <div className="mt-2">
                      <CopyButton text={displayBatchResult.errors.map((r: any) => r.pixiv_id).join("\n")} />
                    </div>
                  </details>
                )}
              </div>
            )}
          </div>
        )}
      </div>

      <div className="grid grid-cols-1 md:grid-cols-3 gap-4 mb-6">
        <div className="card p-4">
          <h3 className="font-medium mb-1 text-sm">{t("danbooru.search_url_title")}</h3>
          <p className="text-xs text-muted mb-3">{t("danbooru.search_url_desc")}</p>
          <input value={searchUrl} onChange={(e) => setSearchUrl(e.target.value)}
            placeholder={t("danbooru.url_placeholder")}
            className="input mb-2 w-full" />
          <button onClick={() => handleSearch("url")} disabled={!searchUrl.trim()}
            className="btn-primary w-full">
            {t("danbooru.search_btn")}
          </button>
        </div>

        <div className="card p-4">
          <h3 className="font-medium mb-1 text-sm">{t("danbooru.search_pixiv_title")}</h3>
          <p className="text-xs text-muted mb-3">{t("danbooru.search_pixiv_desc")}</p>
          <input value={searchPixivId} onChange={(e) => setSearchPixivId(e.target.value)}
            placeholder={t("danbooru.pixiv_id_placeholder")}
            className="input mb-2 w-full" />
          <button onClick={() => handleSearch("pixiv_id")} disabled={!searchPixivId.trim()}
            className="btn-primary w-full">
            {t("danbooru.search_btn")}
          </button>
        </div>

        <div className="card p-4">
          <h3 className="font-medium mb-1 text-sm">{t("danbooru.search_name_title")}</h3>
          <p className="text-xs text-muted mb-3">{t("danbooru.search_name_desc")}</p>
          <input value={searchName} onChange={(e) => setSearchName(e.target.value)}
            placeholder={t("danbooru.name_placeholder")}
            className="input mb-2 w-full" />
          <button onClick={() => handleSearch("name")} disabled={!searchName.trim()}
            className="btn-primary w-full">
            {t("danbooru.search_btn")}
          </button>
        </div>
      </div>

      {preview.isLoading && <div className="card p-4 animate-pulse"><div className="h-24 rounded-md bg-subtle dark:bg-subtle" /></div>}
      {preview.error && <ErrorState message={(preview.error as Error).message} />}
      {preview.data && !preview.data.found && (
        <div className="card p-4">
          <EmptyState title={t("danbooru.no_match")} description={preview.data.message || "No matching Danbooru artist found."} />
        </div>
      )}

      {artist && (
        <PreviewResult artist={artist} links={links}
          onImport={(creatorId) => importMutation.mutate(creatorId)}
          importPending={importMutation.isPending}
          onImportAll={(creatorName) => importAllMutation.mutate(creatorName)}
          importAllPending={importAllMutation.isPending || importAllOperation?.status === "running"}
          importAllError={(importAllMutation.error as Error)?.message || null}
          selectedCreator={selectedCreator} setSelectedCreator={setSelectedCreator}
          importName={importName} setImportName={setImportName} />
      )}
    </main>
  );
}

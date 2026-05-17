"use client";
import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api, queryKeys } from "@/lib/api";
import { PageHeader, EmptyState, ErrorState, SourceBadge } from "@/components";

interface ArtistUrls { url: string; normalized_url: string; is_active: boolean }
interface ArtistResult {
  id: number; name: string; other_names: string[]; post_count?: number;
  notes?: string; is_active?: boolean; created_at?: string; urls: ArtistUrls[];
}
interface SuggestedLink { url: string; link_type: string; source: string; confidence: number; is_verified: boolean; notes?: string }

function PreviewResult({ artist, links, onImport, importPending, onImportAll, importAllPending, importAllError, selectedCreator, setSelectedCreator }: {
  artist: ArtistResult; links: SuggestedLink[];
  onImport: (creatorId: string) => void; importPending: boolean;
  onImportAll: (creatorName: string) => void; importAllPending: boolean; importAllError: string | null;
  selectedCreator: string; setSelectedCreator: (v: string) => void;
}) {
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
      alert("Subscription source created! Trigger a sync from the Subscriptions page.");
    },
    onError: (err) => {
      alert(`Failed: ${(err as Error).message}`);
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
        <h3 className="font-medium mb-2">Artist #{artist.id}: {artist.name}</h3>
        {artist.other_names.length > 0 && (
          <p className="text-xs text-gray-500 dark:text-gray-400 mb-1">Also known as: {artist.other_names.join(", ")}</p>
        )}
        {artist.post_count != null && <p className="text-xs text-gray-500 dark:text-gray-400">Danbooru posts: {artist.post_count}</p>}
        {artist.notes && <p className="text-xs text-gray-600 dark:text-gray-300 mt-2 bg-gray-50 dark:bg-slate-800/50 p-2 rounded">{artist.notes}</p>}
      </div>

      {/* One-Click Import All & Subscribe */}
      <div className="bg-blue-50 dark:bg-blue-900/30 border border-blue-200 dark:border-blue-800 rounded-lg p-4">
        <p className="text-sm text-blue-800 dark:text-blue-300 font-medium mb-1">One-Click Import</p>
        <p className="text-xs text-blue-600 mb-3">Creates Creator + Subscription + Sources + Links in one step.</p>
        <div className="flex items-end gap-2">
          <div className="flex-1">
            <input type="text" value={selectedCreator} onChange={(e) => setSelectedCreator(e.target.value)}
              placeholder="Creator name (leave empty for auto)"
              className="w-full border rounded px-2 py-1 text-xs" />
          </div>
          <button onClick={() => onImportAll(selectedCreator)} disabled={importAllPending}
            className="px-4 py-1.5 bg-blue-600 text-white rounded text-sm hover:bg-blue-700 disabled:opacity-50 shrink-0">
            {importAllPending ? "Importing..." : "Import All & Subscribe"}
          </button>
        </div>
        {importAllError && <p className="text-red-600 text-xs mt-2">{importAllError}</p>}
      </div>

      <div className="bg-white dark:bg-slate-800 border rounded-lg p-4">
        {/* Downloadable URLs with Subscribe buttons */}
        {downloadableUrls.length > 0 && (
          <div className="mt-3">
            <h4 className="text-xs font-medium text-gray-500 dark:text-gray-400 mb-2">Downloadable Sources ({downloadableUrls.length})</h4>
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
                      <option value="">Select creator...</option>
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
                      {subscribingUrl === u.normalized_url && subscribe.isPending ? "..." : "Subscribe"}
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
            <h4 className="text-xs font-medium text-gray-500 dark:text-gray-400 mb-1">Other Associated URLs</h4>
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
          <p className="text-xs text-gray-400 dark:text-gray-500 mt-3">No associated source URLs in Danbooru.</p>
        )}
      </div>

      {/* Import all links */}
      {links.length > 0 && (
        <div className="bg-white dark:bg-slate-800 border rounded-lg p-4">
          <h3 className="font-medium mb-2">Import All Links ({links.length})</h3>
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
              <label className="block text-xs font-medium mb-1">Target Creator</label>
              <select value={selectedCreator} onChange={(e) => setSelectedCreator(e.target.value)}
                className="w-full border rounded px-3 py-2 text-sm">
                <option value="">Select creator...</option>
                {creators.data?.map((c) => <option key={c.id} value={c.id}>{c.display_name || c.name}</option>)}
              </select>
            </div>
            <button onClick={() => onImport(selectedCreator)} disabled={!selectedCreator || importPending}
              className="px-4 py-2 text-sm bg-slate-900 dark:bg-slate-700 text-white rounded hover:bg-slate-800 dark:hover:bg-slate-600 disabled:opacity-50 shrink-0">
              {importPending ? "Importing..." : `Import ${links.length} Links`}
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
  const [searchUrl, setSearchUrl] = useState("");
  const [searchName, setSearchName] = useState("");
  const [searchPixivId, setSearchPixivId] = useState("");
  const [selectedCreator, setSelectedCreator] = useState("");
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
      alert(`Imported ${data.imported} links from Danbooru artist "${data.artist_name}"`);
      qc.invalidateQueries({ queryKey: queryKeys.creators.all });
    },
  });

  const importAllMutation = useMutation({
    mutationFn: (creatorName: string) => api.importAllDanbooru({
      creator_name: creatorName || undefined,
      ...searchParams,
    }),
    onSuccess: (data) => {
      if (!data.found) { alert("No matching Danbooru artist found."); return; }
      qc.invalidateQueries({ queryKey: queryKeys.creators.all });
      qc.invalidateQueries({ queryKey: queryKeys.subscriptions.all });
      alert(`Done! Creator: ${data.creator_id?.slice(0, 8)}... | Links: ${data.links_imported} | Sources: ${data.sources_created}`);
    },
  });

  const batchImport = useMutation({
    mutationFn: (ids: string[]) => api.batchImportDanbooru(ids),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: queryKeys.creators.all });
      qc.invalidateQueries({ queryKey: queryKeys.subscriptions.all });
    },
  });

  const handleBatchSubmit = () => {
    const ids = batchInput
      .split(/[\n,]+/)
      .map((s) => s.trim())
      .filter((s) => /^\d+$/.test(s));
    if (ids.length === 0) return;
    batchImport.mutate(ids);
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

  return (
    <main className="max-w-5xl mx-auto p-6">
      <PageHeader title="Danbooru Reference Mapping" description="Import Danbooru artist data and subscribe to source profiles" />

      <div className="bg-blue-50 dark:bg-blue-900/30 border border-blue-200 dark:border-blue-800 rounded-lg p-4 text-sm mb-6">
        <p className="font-medium text-blue-800 dark:text-blue-300 mb-1">All searches query Danbooru's artist database directly.</p>
        <p className="text-blue-700 dark:text-blue-300">Danbooru artists map source profiles (Pixiv, Twitter, Iwara, etc.) to a canonical name. Use any of the three methods below to find the matching Danbooru artist record.</p>
      </div>

      {/* Batch Import */}
      <div className="bg-white dark:bg-slate-800 rounded-lg shadow p-4 mb-6">
        <div className="flex items-center justify-between mb-3">
          <h3 className="font-medium text-sm">Batch Import by Pixiv User ID</h3>
          <button onClick={() => setShowBatch(!showBatch)}
            className="text-xs text-blue-600 hover:underline">
            {showBatch ? "Collapse" : "Expand"}
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
              placeholder={"1980643\n123456\n789012"}
              rows={5}
              className="w-full border rounded px-3 py-2 text-sm font-mono resize-y"
            />
            <div className="flex items-center gap-3">
              <button
                onClick={handleBatchSubmit}
                disabled={batchImport.isPending || !batchInput.trim()}
                className="px-4 py-2 bg-blue-600 text-white rounded text-sm hover:bg-blue-700 disabled:opacity-50"
              >
                {batchImport.isPending ? "Processing..." : "Batch Import"}
              </button>
              <span className="text-xs text-gray-400">
                {batchInput.trim() ? `${batchInput.split(/[\\n,]+/).filter(s => /^\\d+$/.test(s.trim())).length} valid IDs` : ""}
              </span>
            </div>
            {batchImport.error && (
              <p className="text-red-600 text-sm">{(batchImport.error as Error).message}</p>
            )}

            {/* Results */}
            {batchImport.data && (
              <div className="mt-4 space-y-4">
                {/* Summary */}
                <div className="grid grid-cols-4 gap-3">
                  <div className="bg-green-50 dark:bg-green-900/30 border border-green-200 dark:border-green-800 rounded p-3 text-center">
                    <div className="text-xl font-bold text-green-700 dark:text-green-400">{batchImport.data.imported_count}</div>
                    <div className="text-xs text-green-600">Imported</div>
                  </div>
                  <div className="bg-yellow-50 dark:bg-yellow-900/30 border border-yellow-200 dark:border-yellow-800 rounded p-3 text-center">
                    <div className="text-xl font-bold text-yellow-700 dark:text-yellow-400">{batchImport.data.low_confidence_count}</div>
                    <div className="text-xs text-yellow-600">Low Confidence</div>
                  </div>
                  <div className="bg-red-50 dark:bg-red-900/30 border border-red-200 dark:border-red-800 rounded p-3 text-center">
                    <div className="text-xl font-bold text-red-700 dark:text-red-400">{batchImport.data.not_found_count}</div>
                    <div className="text-xs text-red-600">Not Found</div>
                  </div>
                  <div className="bg-gray-50 dark:bg-slate-800/50 border border-gray-200 dark:border-slate-700 rounded p-3 text-center">
                    <div className="text-xl font-bold text-gray-600 dark:text-gray-400">{batchImport.data.error_count}</div>
                    <div className="text-xs text-gray-500">Errors</div>
                  </div>
                </div>

                {/* Imported list */}
                {batchImport.data.imported.length > 0 && (
                  <div>
                    <h4 className="text-sm font-medium text-green-700 dark:text-green-400 mb-2">Imported ({batchImport.data.imported.length})</h4>
                    <div className="space-y-1 max-h-48 overflow-y-auto">
                      {batchImport.data.imported.map((r, i) => (
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
                {batchImport.data.low_confidence.length > 0 && (
                  <div>
                    <h4 className="text-sm font-medium text-yellow-700 dark:text-yellow-400 mb-2">Low Confidence — Manual Review ({batchImport.data.low_confidence.length})</h4>
                    <p className="text-xs text-yellow-600 mb-2">Found Danbooru artist but no downloadable source URLs. Use individual search below to review and manually subscribe.</p>
                    <div className="space-y-1 max-h-48 overflow-y-auto">
                      {batchImport.data.low_confidence.map((r, i) => (
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
                {batchImport.data.not_found.length > 0 && (
                  <div>
                    <h4 className="text-sm font-medium text-red-700 dark:text-red-400 mb-2">Not Found ({batchImport.data.not_found.length})</h4>
                    <p className="text-xs text-red-600 mb-2">No matching Danbooru artist. May need manual creator creation and source linking.</p>
                    <div className="flex flex-wrap gap-2">
                      {batchImport.data.not_found.map((r, i) => (
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
          <h3 className="font-medium mb-1 text-sm">Search by Source URL</h3>
          <p className="text-xs text-gray-500 dark:text-gray-400 mb-3">Matches Danbooru artist URLs exactly. Works with any URL type (Pixiv, Twitter, Iwara, etc.).</p>
          <input value={searchUrl} onChange={(e) => setSearchUrl(e.target.value)}
            placeholder="https://www.pixiv.net/en/users/1980643"
            className="w-full border rounded px-3 py-2 text-sm mb-2" />
          <button onClick={() => handleSearch("url")} disabled={!searchUrl.trim()}
            className="w-full px-3 py-2 bg-slate-900 dark:bg-slate-700 text-white rounded text-sm hover:bg-slate-800 dark:hover:bg-slate-600 disabled:opacity-50">
            Search Danbooru
          </button>
        </div>

        <div className="bg-white dark:bg-slate-800 rounded-lg shadow p-4">
          <h3 className="font-medium mb-1 text-sm">Search by Pixiv User ID</h3>
          <p className="text-xs text-gray-500 dark:text-gray-400 mb-3">Constructs a Pixiv user URL and matches it against Danbooru artist records.</p>
          <input value={searchPixivId} onChange={(e) => setSearchPixivId(e.target.value)}
            placeholder="1980643"
            className="w-full border rounded px-3 py-2 text-sm mb-2" />
          <button onClick={() => handleSearch("pixiv_id")} disabled={!searchPixivId.trim()}
            className="w-full px-3 py-2 bg-slate-900 dark:bg-slate-700 text-white rounded text-sm hover:bg-slate-800 dark:hover:bg-slate-600 disabled:opacity-50">
            Search Danbooru
          </button>
        </div>

        <div className="bg-white dark:bg-slate-800 rounded-lg shadow p-4">
          <h3 className="font-medium mb-1 text-sm">Search by Artist Name</h3>
          <p className="text-xs text-gray-500 dark:text-gray-400 mb-3">Searches Danbooru artist names directly using wildcard matching. Supports partial names.</p>
          <input value={searchName} onChange={(e) => setSearchName(e.target.value)}
            placeholder="ask (askzy)"
            className="w-full border rounded px-3 py-2 text-sm mb-2" />
          <button onClick={() => handleSearch("name")} disabled={!searchName.trim()}
            className="w-full px-3 py-2 bg-slate-900 dark:bg-slate-700 text-white rounded text-sm hover:bg-slate-800 dark:hover:bg-slate-600 disabled:opacity-50">
            Search Danbooru
          </button>
        </div>
      </div>

      {preview.isLoading && <div className="bg-white dark:bg-slate-800 rounded-lg shadow p-4 animate-pulse"><div className="h-24 bg-gray-100 dark:bg-slate-700 rounded" /></div>}
      {preview.error && <ErrorState message={(preview.error as Error).message} />}
      {preview.data && !preview.data.found && (
        <div className="bg-white dark:bg-slate-800 rounded-lg shadow p-4">
          <EmptyState title="No match found" description={preview.data.message || "No matching Danbooru artist found."} />
        </div>
      )}

      {artist && (
        <PreviewResult artist={artist} links={links}
          onImport={(creatorId) => importMutation.mutate(creatorId)}
          importPending={importMutation.isPending}
          onImportAll={(creatorName) => importAllMutation.mutate(creatorName)}
          importAllPending={importAllMutation.isPending}
          importAllError={(importAllMutation.error as Error)?.message || null}
          selectedCreator={selectedCreator} setSelectedCreator={setSelectedCreator} />
      )}
    </main>
  );
}

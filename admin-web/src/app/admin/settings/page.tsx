"use client";
import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api, queryKeys } from "@/lib/api";
import { PageHeader, StatusBadge, ConfirmDialog } from "@/components";

function PixivAuthForm() {
  const qc = useQueryClient();
  const config = useQuery({ queryKey: ["gallerydl-config"], queryFn: api.getGalleryDLConfig });
  const [cookiesPath, setCookiesPath] = useState("");
  const [refreshToken, setRefreshToken] = useState("");
  const [saved, setSaved] = useState(false);

  // Sync loaded data into local state
  const [loaded, setLoaded] = useState(false);
  if (config.data && !loaded) { setCookiesPath(config.data.cookies_path || ""); setRefreshToken(config.data.refresh_token || ""); setLoaded(true); }

  const save = useMutation({
    mutationFn: () => api.updateGalleryDLConfig({ cookies_path: cookiesPath || undefined, refresh_token: refreshToken || undefined }),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["gallerydl-config"] }); setSaved(true); setTimeout(() => setSaved(false), 3000); },
  });

  return (
    <div className="space-y-4">
      <div><label className="block text-sm font-medium mb-1">Cookies File Path</label>
        <div className="flex gap-2">
          <input value={cookiesPath} onChange={(e) => setCookiesPath(e.target.value)} placeholder="/gallerydl-config/cookies/pixiv.txt" className="flex-1 border rounded px-3 py-2 text-sm font-mono" />
        </div>
        <p className="text-xs text-gray-400 mt-1">Export cookies from browser in Netscape format. Save to the gallery-dl config directory.</p>
      </div>
      <div><label className="block text-sm font-medium mb-1">Refresh Token</label>
        <div className="flex gap-2">
          <input value={refreshToken} onChange={(e) => setRefreshToken(e.target.value)} placeholder="Pixiv OAuth refresh token" className="flex-1 border rounded px-3 py-2 text-sm font-mono" type="password" />
        </div>
        <p className="text-xs text-gray-400 mt-1">Obtain via <code className="bg-gray-100 px-1 rounded">gallery-dl oauth:pixiv</code> on the NAS host. Preferred over cookies.</p>
      </div>
      <div className="flex justify-end gap-3 pt-2">
        <button onClick={() => save.mutate()} disabled={save.isPending || (!cookiesPath && !refreshToken)}
          className="px-4 py-2 bg-slate-900 text-white rounded text-sm hover:bg-slate-800 disabled:opacity-50">
          {save.isPending ? "Saving..." : saved ? "Saved!" : "Save Configuration"}
        </button>
      </div>
      {save.error && <p className="text-red-600 text-sm">{(save.error as Error).message}</p>}
    </div>
  );
}

function ConfigRow({ label, value, description, type = "text" }: { label: string; value: unknown; description: string; type?: string }) {
  const display = type === "boolean"
    ? (value ? "true" : "false")
    : type === "null"
    ? "null (not set)"
    : value === null || value === undefined
    ? "—"
    : String(value);
  return (
    <div className="flex items-center justify-between py-2 border-b last:border-0">
      <div>
        <span className="font-medium font-mono text-sm">extractor.pixiv.{label}</span>
        <p className="text-xs text-gray-500 mt-0.5 max-w-md">{description}</p>
      </div>
      <span className={`px-2 py-0.5 rounded text-xs font-mono shrink-0 ml-4 ${
        type === "boolean"
          ? (value ? "bg-green-100 text-green-700" : "bg-gray-100 text-gray-500")
          : type === "null" || value === null
          ? "bg-gray-100 text-gray-400 italic"
          : "bg-blue-100 text-blue-700"
      }`}>{display}</span>
    </div>
  );
}

export default function SettingsPage() {
  const qc = useQueryClient();
  const settings = useQuery({ queryKey: queryKeys.admin.settings, queryFn: api.getAdminSettings });
  const reindex = useMutation({
    mutationFn: api.reindexSearch,
    onSuccess: () => qc.invalidateQueries({ queryKey: queryKeys.admin.settings }),
  });
  const [confirmReindex, setConfirmReindex] = useState(false);

  const d = settings.data?.dedup;

  return (
    <main className="max-w-4xl mx-auto p-6">
      <PageHeader title="Settings" description="System configuration and administrative tools" />

      {/* gallery-dl: Pixiv Configuration */}
      <section className="mb-8">
        <h2 className="text-lg font-semibold mb-3">gallery-dl — Pixiv Extractor</h2>
        <div className="bg-white rounded-lg shadow p-4">
          <p className="text-sm text-gray-600 mb-4">
            These options configure the Pixiv extractor in gallery-dl. They correspond to <code className="text-xs bg-gray-100 px-1 rounded">extractor.pixiv.*</code> in gallery-dl&apos;s config.
            Values shown are gallery-dl defaults. Actual runtime config is stored in <code className="text-xs bg-gray-100 px-1 rounded">$GALLERYDL_CONFIG_ROOT/config.json</code>.
          </p>

          <h3 className="font-medium text-sm mb-2 text-gray-700">Authentication</h3>
          <div className="text-sm space-y-1 mb-4">
            <ConfigRow label="refresh-token" value="(OAuth)" type="null" description="Pixiv OAuth refresh token. Preferred auth method. Obtain via gallery-dl oauth:pixiv." />
            <ConfigRow label="cookies" value="(path)" type="null" description="Path to Netscape-format cookies file. Alternative to refresh-token. Store in /gallerydl-config/cookies/." />
          </div>

          <h3 className="font-medium text-sm mb-2 text-gray-700">Content Filters</h3>
          <div className="text-sm space-y-1 mb-4">
            <ConfigRow label="include" value='["artworks"]' description='Content types to download. Options: "artworks", "favorites", "bookmarks", "follows", "rankings", "search", "series", "sketch".' />
            <ConfigRow label="ugoira" value={true} type="boolean" description="Download animated Ugoira illustrations as ZIP archives." />
            <ConfigRow label="tags" value="japanese" description='Tag language. Options: "japanese" (original), "english" (translated), "translated" (if available).' />
            <ConfigRow label="max-posts" value={null} type="null" description="Maximum number of posts to download per URL. null = no limit." />
            <ConfigRow label="sanity" value={true} type="boolean" description="Skip images that fail sanity checks (e.g., corrupted files)." />
          </div>

          <h3 className="font-medium text-sm mb-2 text-gray-700">Metadata</h3>
          <div className="text-sm space-y-1 mb-4">
            <ConfigRow label="metadata" value={false} type="boolean" description="Write metadata JSON files alongside downloads." />
            <ConfigRow label="metadata-bookmark" value={false} type="boolean" description="Include bookmark/favorite metadata in output." />
            <ConfigRow label="captions" value={false} type="boolean" description="Download artwork captions/descriptions." />
            <ConfigRow label="comments" value={false} type="boolean" description="Download artwork comments." />
          </div>

          <h3 className="font-medium text-sm mb-2 text-gray-700">General Extractor Options</h3>
          <div className="text-sm space-y-1">
            <ConfigRow label="filename" value="{id}_p{num}.{extension}" description='Output filename pattern. Keywords: {id}, {title}, {num}, {extension}, {date}, {user[id]}, {user[name]}, {user[account]}, {tags}, {type}.' />
            <ConfigRow label="directory" value="{user[id]}" description='Output directory pattern. Same keywords as filename. Use {user[id]} for per-creator folders.' />
            <ConfigRow label="skip" value={true} type="boolean" description="Skip downloading files that already exist in the target directory." />
            <ConfigRow label="sleep-request" value={0} description="Seconds to sleep between HTTP requests. Increase to avoid rate-limiting." />
            <ConfigRow label="retries" value={4} description="Number of retry attempts for failed downloads." />
            <ConfigRow label="timeout" value={30} description="HTTP request timeout in seconds." />
            <ConfigRow label="archive" value={null} type="null" description="Path to archive file tracking downloaded IDs. Enables incremental downloads." />
          </div>
        </div>
      </section>

      {/* gallery-dl Auth Configuration */}
      <section className="mb-8">
        <h2 className="text-lg font-semibold mb-3">gallery-dl — Pixiv Authentication</h2>
        <div className="bg-white rounded-lg shadow p-4">
          <p className="text-sm text-gray-600 mb-4">Configure Pixiv authentication for gallery-dl. At least one method (cookies or refresh-token) is required for downloads to work.</p>
          <PixivAuthForm />
        </div>
      </section>

      {/* Deduplication */}
      <section className="mb-8">
        <h2 className="text-lg font-semibold mb-3">Deduplication</h2>
        {settings.isLoading ? (
          <div className="bg-white rounded-lg shadow p-4 animate-pulse"><div className="h-32 bg-gray-100 rounded" /></div>
        ) : (
          <div className="bg-white rounded-lg shadow p-4">
            <div className="text-sm space-y-3">
              {d && Object.entries(d).map(([key, value]) => (
                <div key={key} className="flex items-center justify-between py-2 border-b last:border-0">
                  <div>
                    <span className="font-medium">{key}</span>
                    <p className="text-xs text-gray-500 mt-0.5 max-w-md">
                      {key === "source_level_enabled" && "Source-level exact deduplication. Same source + same work ID = skip download."}
                      {key === "cross_source_enabled" && "Cross-source SHA-256 deduplication. Matching files across different sources reuse existing asset records."}
                      {key === "auto_merge" && "Auto-merge visually similar works without admin review. DANGER: may incorrectly merge different works."}
                      {key === "phash_threshold" && "Perceptual hash similarity threshold (0–64). Lower = stricter match, fewer false positives. 8 is a reasonable default."}
                    </p>
                  </div>
                  <span className={`px-2 py-0.5 rounded text-xs font-mono shrink-0 ml-4 ${typeof value === "boolean" ? (value ? "bg-green-100 text-green-700" : "bg-gray-100 text-gray-500") : "bg-blue-100 text-blue-700"}`}>
                    {String(value)}
                  </span>
                </div>
              ))}
            </div>
            <div className="mt-4 p-3 bg-yellow-50 border border-yellow-200 rounded-lg text-sm text-yellow-800">
              <strong>All deduplication is OFF by default.</strong> Enabling auto-merge or auto-delete may irreversibly modify your library. Admin review is always required before these features take effect.
            </div>
          </div>
        )}
      </section>

      {/* Search Index */}
      <section className="mb-8">
        <h2 className="text-lg font-semibold mb-3">Search Index</h2>
        <div className="bg-white rounded-lg shadow p-4">
          <div className="flex items-center justify-between">
            <div>
              <p className="text-sm font-medium">Meilisearch Re-indexing</p>
              <p className="text-xs text-gray-500 mt-1">Admin-triggered full re-indexing for v1. Rebuilds the search index from all works, creators, and tags in the database.</p>
            </div>
            <button onClick={() => setConfirmReindex(true)} disabled={reindex.isPending}
              className="px-4 py-2 bg-slate-900 text-white rounded text-sm hover:bg-slate-800 disabled:opacity-50 shrink-0">
              {reindex.isPending ? "Reindexing..." : "Reindex Now"}
            </button>
          </div>
          {reindex.data && <p className="mt-3 text-sm text-green-600">{reindex.data.message}</p>}
          {reindex.error && <p className="mt-3 text-sm text-red-600">{(reindex.error as Error).message}</p>}
        </div>
      </section>

      {/* Backend Info */}
      <section className="mb-8">
        <h2 className="text-lg font-semibold mb-3">System Information</h2>
        <div className="bg-white rounded-lg shadow p-4 text-sm space-y-2">
          <div className="flex justify-between"><span className="text-gray-500">Backend API</span><span className="font-mono text-xs">{process.env.NEXT_PUBLIC_API_URL || "http://localhost:8818"}</span></div>
          <div className="flex justify-between"><span className="text-gray-500">Admin Web</span><span className="text-xs">Next.js 14 · TypeScript · Tailwind CSS · TanStack Query</span></div>
          <div className="flex justify-between"><span className="text-gray-500">gallery-dl Config</span><span className="font-mono text-xs">$GALLERYDL_CONFIG_ROOT/config.json</span></div>
          <div className="flex justify-between"><span className="text-gray-500">Dedup default</span><span className="flex items-center gap-1"><StatusBadge status="down" /> <span className="text-xs text-gray-400">OFF</span></span></div>
          <div className="flex justify-between"><span className="text-gray-500">Auth mode</span><span className="text-xs text-gray-400">Phase 1-5: Admin API key · Phase 6+: JWT multi-user</span></div>
        </div>
      </section>

      {confirmReindex && <ConfirmDialog open title="Reindex Search" message="This will trigger a full Meilisearch re-indexing. This may take a while for large libraries." onConfirm={() => { reindex.mutate(); setConfirmReindex(false); }} onCancel={() => setConfirmReindex(false)} />}
    </main>
  );
}

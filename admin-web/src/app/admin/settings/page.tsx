"use client";
import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api, queryKeys } from "@/lib/api";
import { PageHeader, ErrorState, ConfirmDialog, CardSkeleton } from "@/components";

function GalleryDLConfigForm() {
  const qc = useQueryClient();
  const config = useQuery({ queryKey: ["gallerydl-config"], queryFn: api.getGalleryDLConfig });

  const [form, setForm] = useState({
    cookies_path: "", refresh_token: "", filename: "", directory: "",
    include: "artworks", tags: "japanese", ugoira: true,
    sleep_request: "", max_posts: "",
  });
  const [saved, setSaved] = useState(false);

  const [loaded, setLoaded] = useState(false);
  if (config.data && !loaded) {
    setForm({
      cookies_path: config.data.cookies_path || "",
      refresh_token: config.data.refresh_token || "",
      filename: config.data.filename || "",
      directory: config.data.directory || "",
      include: config.data.include || "artworks",
      tags: config.data.tags || "japanese",
      ugoira: config.data.ugoira ?? true,
      sleep_request: config.data.sleep_request?.toString() || "",
      max_posts: config.data.max_posts?.toString() || "",
    });
    setLoaded(true);
  }

  if (config.isError) {
    return <ErrorState message={config.error?.message || "Failed to load gallery-dl config"} onRetry={() => config.refetch()} />;
  }

  if (config.isLoading || !loaded) {
    return <div className="space-y-4"><CardSkeleton /><CardSkeleton /><CardSkeleton /></div>;
  }

  const update = (k: string, v: any) => setForm((p) => ({ ...p, [k]: v }));

  const save = useMutation({
    mutationFn: () => api.updateGalleryDLConfig({
      cookies_path: form.cookies_path || undefined,
      refresh_token: form.refresh_token || undefined,
      filename: form.filename || undefined,
      directory: form.directory || undefined,
      include: form.include || undefined,
      tags: form.tags || undefined,
      ugoira: form.ugoira,
      sleep_request: form.sleep_request ? parseFloat(form.sleep_request) : undefined,
      max_posts: form.max_posts ? parseInt(form.max_posts) : undefined,
    }),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["gallerydl-config"] }); setSaved(true); setTimeout(() => setSaved(false), 3000); },
  });

  const field = (label: string, key: keyof typeof form, placeholder?: string, type = "text", desc?: string) => (
    <div>
      <label className="block text-sm font-medium mb-1">{label}</label>
      <div className="flex gap-2">
        {type === "select" ? (
          <select value={form[key] as string} onChange={(e) => update(key, e.target.value)}
            className="flex-1 border rounded px-3 py-2 text-sm bg-white">
            {key === "include" && ["artworks","favorites","bookmarks"].map((o) => <option key={o} value={o}>{o}</option>)}
            {key === "tags" && ["japanese","english","translated"].map((o) => <option key={o} value={o}>{o}</option>)}
          </select>
        ) : type === "checkbox" ? (
          <label className="flex items-center gap-2 cursor-pointer">
            <input type="checkbox" checked={form[key] as boolean} onChange={(e) => update(key, e.target.checked)} className="rounded" />
            <span className="text-sm text-gray-500">{form[key] ? "Enabled" : "Disabled"}</span>
          </label>
        ) : type === "password" ? (
          <input value={form[key] as string} onChange={(e) => update(key, e.target.value)}
            type="password" placeholder={placeholder} className="flex-1 border rounded px-3 py-2 text-sm font-mono" />
        ) : (
          <input value={form[key] as string} onChange={(e) => update(key, e.target.value)}
            type="text" placeholder={placeholder} className="flex-1 border rounded px-3 py-2 text-sm font-mono" />
        )}
      </div>
      {desc && <p className="text-xs text-gray-400 mt-1">{desc}</p>}
    </div>
  );

  return (
    <div className="space-y-5">
      <h4 className="font-medium text-sm text-gray-700 border-b pb-2">Authentication</h4>
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        {field("Cookies File Path", "cookies_path", "/gallerydl-config/cookies/pixiv.txt", "text", "Netscape-format cookies file. Export from browser.")}
        {field("Refresh Token", "refresh_token", "Pixiv OAuth refresh token", "password", "Obtain via gallery-dl oauth:pixiv. Preferred over cookies.")}
      </div>

      <h4 className="font-medium text-sm text-gray-700 border-b pb-2">Content Filters</h4>
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        {field("Include", "include", "", "select", 'Content types to download: "artworks", "favorites", "bookmarks"')}
        {field("Tag Language", "tags", "", "select", "japanese=original, english=translated, translated=if available")}
        {field("Download Ugoira", "ugoira", "", "checkbox", "Animated illustrations as ZIP archives")}
        {field("Max Posts", "max_posts", "", "text", "Max posts per URL. Leave empty for unlimited.")}
      </div>

      <h4 className="font-medium text-sm text-gray-700 border-b pb-2">File Organization</h4>
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        {field("Filename Pattern", "filename", "{id}_p{num}.{extension}", "text", "Keywords: {id}, {title}, {num}, {extension}, {date}, {user[id]}, {user[name]}, {user[account]}, {type}")}
        {field("Directory Pattern", "directory", "{user[id]}", "text", "Same keywords as filename. {user[id]} groups by creator.")}
      </div>

      <h4 className="font-medium text-sm text-gray-700 border-b pb-2">Rate & Network</h4>
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        {field("Sleep (seconds)", "sleep_request", "0", "text", "Seconds between HTTP requests. Increase to avoid rate-limiting.")}
      </div>

      <div className="flex justify-end pt-2">
        <button onClick={() => save.mutate()} disabled={save.isPending}
          className={`px-6 py-2 rounded text-sm font-medium text-white ${saved ? "bg-green-600" : "bg-slate-900 hover:bg-slate-800"} disabled:opacity-50`}>
          {save.isPending ? "Saving..." : saved ? "Saved!" : "Save All Config"}
        </button>
      </div>
      {save.error && <p className="text-red-600 text-sm">{(save.error as Error).message}</p>}
    </div>
  );
}

function DedupSettings({ settings: d, onSaved }: { settings: Record<string, unknown>; onSaved: () => void }) {
  const qc = useQueryClient();
  const [values, setValues] = useState<Record<string, unknown>>(d);

  const save = useMutation({
    mutationFn: () => api.updateAdminSettings({ dedup: values as Record<string, unknown> }),
    onSuccess: () => { qc.invalidateQueries({ queryKey: queryKeys.admin.settings }); onSaved(); },
  });

  const toggle = (key: string) => {
    setValues((prev) => ({ ...prev, [key]: !prev[key] }));
  };

  const setNumber = (key: string, val: string) => {
    const n = parseInt(val);
    if (!isNaN(n)) setValues((prev) => ({ ...prev, [key]: n }));
  };

  const desc: Record<string, string> = {
    source_level_enabled: "Same source + same ID = skip download",
    cross_source_enabled: "SHA-256 match across sources = reuse asset",
    auto_merge: "Auto-merge similar works without review. DANGER.",
    phash_threshold: "Perceptual hash threshold (0-64). Lower = stricter.",
  };

  const hasChanges = JSON.stringify(values) !== JSON.stringify(d);

  return (
    <div className="bg-white rounded-lg shadow p-4">
      <div className="text-sm space-y-3">
        {Object.entries(values).map(([key, value]) => (
          <div key={key} className="flex items-center justify-between py-2 border-b last:border-0">
            <div>
              <span className="font-medium">{key}</span>
              <p className="text-xs text-gray-500 mt-0.5 max-w-md">{desc[key] || ""}</p>
            </div>
            {typeof value === "boolean" ? (
              <button onClick={() => toggle(key)}
                className={`relative w-10 h-5 rounded-full transition-colors shrink-0 ml-4 ${value ? "bg-green-500" : "bg-gray-300"}`}>
                <span className={`absolute top-0.5 w-4 h-4 rounded-full bg-white transition-transform ${value ? "translate-x-5" : "translate-x-0.5"}`} />
              </button>
            ) : (
              <input type="number" value={String(value)} onChange={(e) => setNumber(key, e.target.value)}
                className="w-20 border rounded px-2 py-1 text-xs font-mono shrink-0 ml-4" min={0} max={64} />
            )}
          </div>
        ))}
      </div>
      <div className="mt-4 flex items-center justify-between">
        <div className="p-3 bg-yellow-50 border border-yellow-200 rounded-lg text-sm text-yellow-800 flex-1 mr-4">
          <strong>All deduplication is OFF by default.</strong> Auto-merge may irreversibly modify your library.
        </div>
        {hasChanges && (
          <button onClick={() => save.mutate()} disabled={save.isPending}
            className="px-4 py-2 bg-blue-600 text-white rounded text-sm hover:bg-blue-700 disabled:opacity-50 shrink-0">
            {save.isPending ? "Saving..." : "Save Changes"}
          </button>
        )}
      </div>
      {save.error && <p className="text-red-600 text-sm mt-2">{(save.error as Error).message}</p>}
    </div>
  );
}

export default function SettingsPage() {
  const qc = useQueryClient();
  const settings = useQuery({ queryKey: queryKeys.admin.settings, queryFn: api.getAdminSettings });
  const reindex = useMutation({ mutationFn: api.reindexSearch, onSuccess: () => qc.invalidateQueries({ queryKey: queryKeys.admin.settings }) });
  const [confirmReindex, setConfirmReindex] = useState(false);

  return (
    <main className="max-w-4xl mx-auto p-6">
      <PageHeader title="Settings" description="System configuration and administrative tools" />

      <section className="mb-8">
        <h2 className="text-lg font-semibold mb-3">gallery-dl — Pixiv Configuration</h2>
        <div className="bg-white rounded-lg shadow p-4">
          <p className="text-sm text-gray-600 mb-4">Configure all Pixiv extractor options for gallery-dl. Changes are saved to <code className="text-xs bg-gray-100 px-1 rounded">$GALLERYDL_CONFIG_ROOT/config.json</code>.</p>
          <GalleryDLConfigForm />
        </div>
      </section>

      <section className="mb-8">
        <h2 className="text-lg font-semibold mb-3">Deduplication</h2>
        {settings.isError ? (
          <ErrorState message={settings.error?.message || "Failed to load settings"} onRetry={() => settings.refetch()} />
        ) : settings.isLoading ? (
          <div className="bg-white rounded-lg shadow p-4 animate-pulse"><div className="h-32 bg-gray-100 rounded" /></div>
        ) : settings.data?.dedup ? (
          <DedupSettings settings={settings.data.dedup} onSaved={() => settings.refetch()} />
        ) : null}
      </section>

      <section className="mb-8">
        <h2 className="text-lg font-semibold mb-3">Search Index</h2>
        <div className="bg-white rounded-lg shadow p-4">
          <div className="flex items-center justify-between">
            <div><p className="text-sm font-medium">Meilisearch Re-indexing</p><p className="text-xs text-gray-500 mt-1">Admin-triggered full re-indexing. Rebuilds index from all works, creators, and tags.</p></div>
            <button onClick={() => setConfirmReindex(true)} disabled={reindex.isPending} className="px-4 py-2 bg-slate-900 text-white rounded text-sm hover:bg-slate-800 disabled:opacity-50 shrink-0">{reindex.isPending ? "Reindexing..." : "Reindex Now"}</button>
          </div>
        </div>
      </section>

      <section className="mb-8">
        <h2 className="text-lg font-semibold mb-3">System Information</h2>
        <div className="bg-white rounded-lg shadow p-4 text-sm space-y-2">
          <div className="flex justify-between"><span className="text-gray-500">Backend API</span><span className="font-mono text-xs">{process.env.NEXT_PUBLIC_API_URL || "http://localhost:8818"}</span></div>
          <div className="flex justify-between"><span className="text-gray-500">Admin Web</span><span className="text-xs">Next.js 14 · TypeScript · Tailwind · TanStack Query</span></div>
          <div className="flex justify-between"><span className="text-gray-500">gallery-dl Config</span><span className="font-mono text-xs">$GALLERYDL_CONFIG_ROOT/config.json</span></div>
          <div className="flex justify-between"><span className="text-gray-500">Auth mode</span><span className="text-xs text-gray-400">Phase 1-5: Admin API key · Phase 6+: JWT multi-user</span></div>
        </div>
      </section>

      {confirmReindex && <ConfirmDialog open title="Reindex Search" message="Full Meilisearch re-indexing may take a while." onConfirm={() => { reindex.mutate(); setConfirmReindex(false); }} onCancel={() => setConfirmReindex(false)} />}
    </main>
  );
}

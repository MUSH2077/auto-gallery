"use client";
import { useState, useEffect, useRef } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { PixivSourceConfig, TwitterSourceConfig, IwaraSourceConfig, GalleryDLSourceMeta } from "@/lib/api";
import { PageHeader, ErrorState } from "@/components";
import Link from "next/link";

type TabKey = "pixiv" | "twitter" | "iwara";

const TABS: { key: TabKey; label: string; color: string }[] = [
  { key: "pixiv", label: "Pixiv", color: "border-blue-500" },
  { key: "twitter", label: "X / Twitter", color: "border-gray-700" },
  { key: "iwara", label: "Iwara", color: "border-pink-500" },
];

function ToggleField({ label, desc, value, onChange }: {
  label: string; desc?: string; value: boolean; onChange: (v: boolean) => void;
}) {
  return (
    <div className="flex items-center justify-between py-2">
      <div>
        <span className="text-sm font-medium">{label}</span>
        {desc && <p className="text-xs text-gray-500 dark:text-gray-400">{desc}</p>}
      </div>
      <button
        onClick={() => onChange(!value)}
        className={`relative inline-flex h-6 w-11 items-center rounded-full transition-colors shrink-0 ${
          value ? "bg-green-600" : "bg-gray-300"
        }`}
      >
        <span className={`inline-block h-4 w-4 transform rounded-full bg-white transition-transform ${
          value ? "translate-x-6" : "translate-x-1"
        }`} />
      </button>
    </div>
  );
}

function TextField({ label, value, onChange, type, placeholder }: {
  label: string; value: string; onChange: (v: string) => void;
  type?: string; placeholder?: string;
}) {
  return (
    <div>
      <label className="block text-sm font-medium mb-1">{label}</label>
      <input type={type || "text"} value={value} onChange={(e) => onChange(e.target.value)}
        className="w-full border rounded px-3 py-2 text-sm font-mono" placeholder={placeholder} />
    </div>
  );
}

function NumberField({ label, value, onChange, placeholder }: {
  label: string; value: string; onChange: (v: string) => void; placeholder?: string;
}) {
  return (
    <div>
      <label className="block text-sm font-medium mb-1">{label}</label>
      <input type="text" value={value} onChange={(e) => onChange(e.target.value)}
        className="w-32 border rounded px-3 py-2 text-sm font-mono" placeholder={placeholder} />
    </div>
  );
}

function SelectField({ label, value, onChange, options }: {
  label: string; value: string; onChange: (v: string) => void;
  options: { value: string; label: string }[];
}) {
  return (
    <div>
      <label className="block text-sm font-medium mb-1">{label}</label>
      <select value={value} onChange={(e) => onChange(e.target.value)}
        className="w-full border rounded px-3 py-2 text-sm bg-white dark:bg-slate-800">
        {options.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
      </select>
    </div>
  );
}

export default function GalleryDLConfigPage() {
  const qc = useQueryClient();
  const config = useQuery({ queryKey: ["gallerydl-config"], queryFn: () => api.getGalleryDLConfig() });
  const [activeTab, setActiveTab] = useState<TabKey>("pixiv");
  const [saved, setSaved] = useState<string | null>(null);

  // Per-source local state
  const [pixiv, setPixiv] = useState<PixivSourceConfig>({});
  const [twitter, setTwitter] = useState<TwitterSourceConfig>({});
  const [iwara, setIwara] = useState<IwaraSourceConfig>({});
  const seeded = useRef(false);

  const save = useMutation({
    mutationFn: () => api.updateGalleryDLConfig({ pixiv, twitter, iwara }),
    onSuccess: (_, v) => {
      qc.invalidateQueries({ queryKey: ["gallerydl-config"] });
      setSaved(activeTab); setTimeout(() => setSaved(null), 3000);
    },
  });

  useEffect(() => {
    if (config.data && !seeded.current) {
      const d = config.data;
      setPixiv(initPixiv(d.pixiv));
      setTwitter(initTwitter(d.twitter));
      setIwara(initIwara(d.iwara));
      seeded.current = true;
    }
  }, [config.data]);

  // Seed helpers
  const initPixiv = (d: any) => ({
    refresh_token: str(d?.refresh_token), cookies_path: str(d?.cookies_path),
    filename: str(d?.filename), directory: str(d?.directory),
    include: str(d?.include, "artworks"), tags: str(d?.tags, "japanese"),
    ugoira: d?.ugoira ?? true, sleep_request: d?.sleep_request,
    max_posts: d?.max_posts,
  });
  const initTwitter = (d: any) => ({
    cookies_path: str(d?.cookies_path), filename: str(d?.filename),
    directory: str(d?.directory), include: str(d?.include, "timeline"),
    retweets: d?.retweets ?? false, replies: d?.replies ?? false,
    cards: d?.cards ?? true, videos: d?.videos ?? true,
    text_tweets: d?.text_tweets ?? false, quoted: d?.quoted ?? false,
    max_posts: d?.max_posts,
  });
  const initIwara = (d: any) => ({
    cookies_path: str(d?.cookies_path), username: str(d?.username),
    password: str(d?.password), filename: str(d?.filename),
    directory: str(d?.directory), videos: d?.videos ?? true,
    format: str(d?.format),
  });

  if (config.isError) {
    return <main className="max-w-4xl mx-auto p-6">
      <ErrorState message={config.error?.message || "Failed"} onRetry={() => config.refetch()} />
    </main>;
  }
  if (!config.data) {
    return <main className="max-w-4xl mx-auto p-6">
      <div className="animate-pulse space-y-4"><div className="h-8 bg-gray-200 rounded w-1/3" /><div className="h-64 bg-gray-200 rounded" /></div>
    </main>;
  }

  const meta = config.data.sources || {} as Record<string, GalleryDLSourceMeta>;
  const currentMeta = meta[activeTab];

  return (
    <main className="max-w-4xl mx-auto p-6">
      <div className="flex items-center gap-4 mb-6">
        <Link href="/admin/settings" className="text-sm text-blue-600 hover:underline">&larr; Settings</Link>
      </div>
      <PageHeader title="gallery-dl Configuration" description="Per-source extractor options saved to config.json." />

      {/* Tabs */}
      <div className="flex border-b dark:border-slate-700 mb-6">
        {TABS.map((t) => (
          <button key={t.key}
            onClick={() => setActiveTab(t.key)}
            className={`px-5 py-3 text-sm font-medium border-b-2 -mb-px transition-colors
              ${activeTab === t.key ? `${t.color} text-slate-900` : "border-transparent text-gray-500 hover:text-gray-700"}`}>
            {t.label}
          </button>
        ))}
      </div>

      {/* Unsupported banner */}
      {currentMeta && !currentMeta.supported && (
        <div className="bg-yellow-50 dark:bg-yellow-900/30 border border-yellow-200 dark:border-yellow-800 rounded-lg p-4 mb-6 text-sm text-yellow-800 dark:text-yellow-300">
          <strong>{currentMeta.name} is not yet supported.</strong> {currentMeta.description}
        </div>
      )}

      {/* Tab content */}
      <div className="bg-white dark:bg-slate-800 rounded-lg shadow p-6 space-y-5">
        {activeTab === "pixiv" && <PixivTab data={pixiv} onChange={setPixiv} />}
        {activeTab === "twitter" && <TwitterTab data={twitter} onChange={setTwitter} />}
        {activeTab === "iwara" && <IwaraTab data={iwara} onChange={setIwara} />}

        <div className="flex justify-end pt-4 border-t">
          <button onClick={() => save.mutate()} disabled={save.isPending}
            className={`px-6 py-2 rounded text-sm font-medium text-white ${saved === activeTab ? "bg-green-600" : "bg-slate-900 hover:bg-slate-800"} disabled:opacity-50`}>
            {save.isPending ? "Saving..." : saved === activeTab ? `Saved!` : `Save ${TABS.find(t => t.key === activeTab)?.label} Config`}
          </button>
        </div>
        {save.error && <p className="text-red-600 text-sm mt-2">{(save.error as Error).message}</p>}
      </div>
    </main>
  );
}

// ── Pixiv Tab ──

function PixivTab({ data, onChange }: { data: PixivSourceConfig; onChange: (d: PixivSourceConfig) => void }) {
  const set = (k: keyof PixivSourceConfig, v: any) => onChange({ ...data, [k]: v });
  return (
    <>
      <h4 className="font-medium text-sm text-gray-700 dark:text-gray-300 border-b dark:border-slate-700 pb-2">Authentication</h4>
      <div className="grid grid-cols-2 gap-4">
        <TextField label="Refresh Token" value={str(data.refresh_token)} onChange={(v) => set("refresh_token", v || undefined)} type="password" />
        <TextField label="Cookies Path" value={str(data.cookies_path)} onChange={(v) => set("cookies_path", v || undefined)} placeholder="/gallerydl-config/cookies/pixiv.txt" />
      </div>
      <h4 className="font-medium text-sm text-gray-700 dark:text-gray-300 border-b dark:border-slate-700 pb-2">Content</h4>
      <div className="grid grid-cols-2 gap-4">
        <SelectField label="Include" value={str(data.include, "artworks")} onChange={(v) => set("include", v)}
          options={[{ value: "artworks", label: "artworks" }, { value: "favorites", label: "favorites" }, { value: "bookmarks", label: "bookmarks" }]} />
        <SelectField label="Tag Language" value={str(data.tags, "japanese")} onChange={(v) => set("tags", v)}
          options={[{ value: "japanese", label: "japanese" }, { value: "english", label: "english" }, { value: "translated", label: "translated" }]} />
        <NumberField label="Max Posts" value={numStr(data.max_posts)} onChange={(v) => set("max_posts", parseInt(v) || undefined)} placeholder="Unlimited" />
        <div className="flex items-end pb-2">
          <label className="flex items-center gap-2 cursor-pointer">
            <input type="checkbox" checked={data.ugoira ?? true} onChange={(e) => set("ugoira", e.target.checked)} className="rounded" />
            <span className="text-sm font-medium">Download Ugoira</span>
          </label>
        </div>
      </div>
      <h4 className="font-medium text-sm text-gray-700 dark:text-gray-300 border-b dark:border-slate-700 pb-2">File Organization</h4>
      <div className="grid grid-cols-2 gap-4">
        <TextField label="Directory Pattern" value={str(data.directory)} onChange={(v) => set("directory", v || undefined)} />
        <TextField label="Filename Pattern" value={str(data.filename)} onChange={(v) => set("filename", v || undefined)} placeholder="{id}_p{num}.{extension}" />
      </div>
      <h4 className="font-medium text-sm text-gray-700 dark:text-gray-300 border-b dark:border-slate-700 pb-2">Rate Limiting</h4>
      <div className="w-64">
        <NumberField label="Sleep (seconds)" value={numStr(data.sleep_request)} onChange={(v) => set("sleep_request", parseFloat(v) || undefined)} placeholder="0" />
      </div>
    </>
  );
}

// ── Twitter Tab ──

function TwitterTab({ data, onChange }: { data: TwitterSourceConfig; onChange: (d: TwitterSourceConfig) => void }) {
  const set = (k: keyof TwitterSourceConfig, v: any) => onChange({ ...data, [k]: v });
  return (
    <>
      <h4 className="font-medium text-sm text-gray-700 dark:text-gray-300 border-b dark:border-slate-700 pb-2">Authentication</h4>
      <div className="grid grid-cols-2 gap-4">
        <TextField label="Cookies Path" value={str(data.cookies_path)} onChange={(v) => set("cookies_path", v || undefined)} placeholder="/gallerydl-config/cookies/twitter.txt" />
      </div>
      <h4 className="font-medium text-sm text-gray-700 dark:text-gray-300 border-b dark:border-slate-700 pb-2">Content</h4>
      <div className="grid grid-cols-2 gap-4">
        <SelectField label="Include" value={str(data.include, "timeline")} onChange={(v) => set("include", v)}
          options={[
            { value: "timeline", label: "timeline" }, { value: "media", label: "media only" },
            { value: "tweets", label: "tweets" }, { value: "likes", label: "likes" },
          ]} />
        <NumberField label="Max Posts" value={numStr(data.max_posts)} onChange={(v) => set("max_posts", parseInt(v) || undefined)} placeholder="Unlimited" />
      </div>
      <h4 className="font-medium text-sm text-gray-700 dark:text-gray-300 border-b dark:border-slate-700 pb-2">Filters</h4>
      <div className="space-y-1">
        <ToggleField label="Retweets" desc="Include retweets." value={data.retweets ?? false} onChange={(v) => set("retweets", v)} />
        <ToggleField label="Replies" desc="Include replies to other users." value={data.replies ?? false} onChange={(v) => set("replies", v)} />
        <ToggleField label="Cards" desc="Download Twitter Cards (e.g. links, summary cards)." value={data.cards ?? true} onChange={(v) => set("cards", v)} />
        <ToggleField label="Videos" desc="Download embedded videos." value={data.videos ?? true} onChange={(v) => set("videos", v)} />
        <ToggleField label="Text Tweets" desc="Include text-only tweets (no media)." value={data.text_tweets ?? false} onChange={(v) => set("text_tweets", v)} />
        <ToggleField label="Quoted Tweets" desc="Include quoted tweet media." value={data.quoted ?? false} onChange={(v) => set("quoted", v)} />
      </div>
      <h4 className="font-medium text-sm text-gray-700 dark:text-gray-300 border-b dark:border-slate-700 pb-2">File Organization</h4>
      <div className="grid grid-cols-2 gap-4">
        <TextField label="Directory Pattern" value={str(data.directory)} onChange={(v) => set("directory", v || undefined)} placeholder="twitter/{user[name]}" />
        <TextField label="Filename Pattern" value={str(data.filename)} onChange={(v) => set("filename", v || undefined)} placeholder="{tweet_id}_{num}.{extension}" />
      </div>
    </>
  );
}

// ── Iwara Tab ──

function IwaraTab({ data, onChange }: { data: IwaraSourceConfig; onChange: (d: IwaraSourceConfig) => void }) {
  const set = (k: keyof IwaraSourceConfig, v: any) => onChange({ ...data, [k]: v });
  return (
    <>
      <h4 className="font-medium text-sm text-gray-700 dark:text-gray-300 border-b dark:border-slate-700 pb-2">Authentication</h4>
      <div className="grid grid-cols-2 gap-4">
        <TextField label="Username" value={str(data.username)} onChange={(v) => set("username", v || undefined)} placeholder="iwara username" />
        <TextField label="Password" value={str(data.password)} onChange={(v) => set("password", v || undefined)} type="password" placeholder="iwara password" />
        <TextField label="Cookies Path" value={str(data.cookies_path)} onChange={(v) => set("cookies_path", v || undefined)} placeholder="/gallerydl-config/cookies/iwara.txt" />
      </div>
      <h4 className="font-medium text-sm text-gray-700 dark:text-gray-300 border-b dark:border-slate-700 pb-2">Content</h4>
      <div className="space-y-1">
        <ToggleField label="Videos" desc="Download video content." value={data.videos ?? true} onChange={(v) => set("videos", v)} />
      </div>
      <h4 className="font-medium text-sm text-gray-700 dark:text-gray-300 border-b dark:border-slate-700 pb-2">File Organization</h4>
      <div className="grid grid-cols-2 gap-4">
        <TextField label="Directory Pattern" value={str(data.directory)} onChange={(v) => set("directory", v || undefined)} placeholder="iwara/{user[name]}" />
        <TextField label="Filename Pattern" value={str(data.filename)} onChange={(v) => set("filename", v || undefined)} />
      </div>
    </>
  );
}

// ── Helpers ──

function str(v: any, fallback = ""): string {
  if (v === null || v === undefined) return fallback;
  return String(v);
}
function numStr(v: any): string {
  if (v === null || v === undefined) return "";
  return String(v);
}

"use client";
import { useState, useEffect, useRef } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { PixivSourceConfig, TwitterSourceConfig, IwaraSourceConfig, GalleryDLSourceMeta } from "@/lib/api";
import { useT } from "@/lib/i18n";
import { PageHeader, ErrorState } from "@/components";
import Link from "next/link";

type TabKey = "pixiv" | "twitter" | "iwara";

function useGalleryTabs() {
  const t = useT();
  return [
    { key: "pixiv" as TabKey, label: t("gallerydl.tab.pixiv"), color: "border-blue-500" },
    { key: "twitter" as TabKey, label: t("gallerydl.tab.twitter"), color: "border-gray-700" },
    { key: "iwara" as TabKey, label: t("gallerydl.tab.iwara"), color: "border-pink-500" },
  ];
}

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
  const t = useT();
  const tabs = useGalleryTabs();
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
    mutationFn: () => {
      // Strip empty strings before saving to avoid gallery-dl IsADirectoryError
      const strip = (obj: Record<string, unknown>) => {
        const cleaned: Record<string, unknown> = {};
        for (const [k, v] of Object.entries(obj)) {
          if (v !== "" && v !== null && v !== undefined) cleaned[k] = v;
        }
        return cleaned;
      };
      return api.updateGalleryDLConfig({
        pixiv: strip(pixiv as unknown as Record<string, unknown>),
        twitter: strip(twitter as unknown as Record<string, unknown>),
        iwara: strip(iwara as unknown as Record<string, unknown>),
      });
    },
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
    cookie_content: str(d?.cookie_content),
    filename: str(d?.filename), directory: str(d?.directory),
    include: str(d?.include, "artworks"), tags: str(d?.tags, "japanese"),
    ugoira: str(d?.ugoira, "zip"), sleep_request: d?.sleep_request,
    max_posts: d?.max_posts,
  });
  const initTwitter = (d: any) => ({
    cookies_path: str(d?.cookies_path), cookie_content: str(d?.cookie_content),
    filename: str(d?.filename), directory: str(d?.directory),
    include: str(d?.include, "timeline"),
    retweets: d?.retweets ?? false, replies: d?.replies ?? false,
    cards: d?.cards ?? true, videos: d?.videos ?? true,
    text_tweets: d?.text_tweets ?? false, quoted: d?.quoted ?? false,
    max_posts: d?.max_posts,
  });
  const initIwara = (d: any) => ({
    cookies_path: str(d?.cookies_path), cookie_content: str(d?.cookie_content),
    username: str(d?.username), password: str(d?.password),
    filename: str(d?.filename),
    directory: str(d?.directory), format: str(d?.format),
  });

  if (config.isError) {
    return <main className="max-w-4xl mx-auto p-6">
      <ErrorState message={config.error?.message || t("gallerydl.failed")} onRetry={() => config.refetch()} />
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
        <Link href="/admin/settings" className="text-sm text-blue-600 hover:underline">&larr; {t("gallerydl.back")}</Link>
      </div>
      <PageHeader title={t("gallerydl.title")} description={t("gallerydl.desc")} />

      {/* Tabs */}
      <div className="flex border-b dark:border-slate-700 mb-6">
        {tabs.map((tab) => (
          <button key={tab.key}
            onClick={() => setActiveTab(tab.key)}
            className={`px-5 py-3 text-sm font-medium border-b-2 -mb-px transition-colors
              ${activeTab === tab.key ? `${tab.color} text-slate-900 dark:text-white` : "border-transparent text-gray-500 hover:text-gray-700"}`}>
            {tab.label}
          </button>
        ))}
      </div>

      {/* Unsupported banner */}
      {currentMeta && !currentMeta.supported && (
        <div className="bg-yellow-50 dark:bg-yellow-900/30 border border-yellow-200 dark:border-yellow-800 rounded-lg p-4 mb-6 text-sm text-yellow-800 dark:text-yellow-300">
          <strong>{currentMeta.name} {t("gallerydl.unsupported")}</strong> {currentMeta.description}
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
            {save.isPending ? t("common.saving") : saved === activeTab ? t("common.saved") : `${t("gallerydl.save")} (${tabs.find(tab => tab.key === activeTab)?.label})`}
          </button>
        </div>
        {save.error && <p className="text-red-600 text-sm mt-2">{(save.error as Error).message}</p>}
      </div>
    </main>
  );
}

// ── Pixiv Tab ──

function PixivTab({ data, onChange }: { data: PixivSourceConfig; onChange: (d: PixivSourceConfig) => void }) {
  const t = useT();
  const set = (k: keyof PixivSourceConfig, v: any) => onChange({ ...data, [k]: v });
  return (
    <>
      <h4 className="font-medium text-sm text-gray-700 dark:text-gray-300 border-b dark:border-slate-700 pb-2">{t("gallerydl.auth")}</h4>
      <div className="grid grid-cols-2 gap-4">
        <TextField label={t("gallerydl.refresh_token")} value={str(data.refresh_token)} onChange={(v) => set("refresh_token", v || undefined)} type="password" />
        <TextField label={t("gallerydl.cookies_path")} value={str(data.cookies_path)} onChange={(v) => set("cookies_path", v || undefined)} placeholder="/gallerydl-config/cookies/pixiv.txt" />
      </div>
      <div>
        <label className="block text-sm font-medium mb-1">{t("gallerydl.cookie_content")}</label>
        <textarea value={str(data.cookie_content)} onChange={(e) => set("cookie_content", e.target.value || undefined)}
          rows={3} className="w-full border rounded px-3 py-2 text-xs font-mono dark:bg-slate-700 dark:text-white"
          placeholder="Paste cookie text here. Auto-saved to /gallerydl-config/cookies/pixiv.txt" />
        <p className="text-xs text-gray-400 mt-1">{t("gallerydl.cookies_help")}</p>
      </div>
      <h4 className="font-medium text-sm text-gray-700 dark:text-gray-300 border-b dark:border-slate-700 pb-2">{t("gallerydl.content")}</h4>
      <div className="grid grid-cols-2 gap-4">
        <SelectField label={t("gallerydl.include")} value={str(data.include, "artworks")} onChange={(v) => set("include", v)}
          options={[{ value: "artworks", label: t("gallerydl.artworks") }, { value: "favorites", label: t("gallerydl.favorites") }, { value: "bookmarks", label: t("gallerydl.bookmarks") }]} />
        <SelectField label={t("gallerydl.tag_language")} value={str(data.tags, "japanese")} onChange={(v) => set("tags", v)}
          options={[{ value: "japanese", label: t("gallerydl.japanese") }, { value: "english", label: t("gallerydl.english") }, { value: "translated", label: t("gallerydl.translated") }]} />
        <NumberField label={t("gallerydl.max_posts")} value={numStr(data.max_posts)} onChange={(v) => set("max_posts", parseInt(v) || undefined)} placeholder="Unlimited" />
        <SelectField label={t("gallerydl.ugoira_format")} value={str(data.ugoira, "zip")} onChange={(v) => set("ugoira", v)}
          options={[
            { value: "zip", label: t("gallerydl.zip_format") },
            { value: "gif", label: t("gallerydl.gif_format") },
          ]} />
      </div>
      <h4 className="font-medium text-sm text-gray-700 dark:text-gray-300 border-b dark:border-slate-700 pb-2">{t("gallerydl.file_org")}</h4>
      <div className="grid grid-cols-2 gap-4">
        <TextField label={t("gallerydl.dir_pattern")} value={str(data.directory)} onChange={(v) => set("directory", v || undefined)} />
        <TextField label={t("gallerydl.filename_pattern")} value={str(data.filename)} onChange={(v) => set("filename", v || undefined)} placeholder="{id}_p{num}.{extension}" />
      </div>
      <h4 className="font-medium text-sm text-gray-700 dark:text-gray-300 border-b dark:border-slate-700 pb-2">{t("gallerydl.rate_limit")}</h4>
      <div className="w-64">
        <NumberField label={t("gallerydl.sleep_seconds")} value={numStr(data.sleep_request)} onChange={(v) => set("sleep_request", parseFloat(v) || undefined)} placeholder="0" />
      </div>
    </>
  );
}

// ── Twitter Tab ──

function TwitterTab({ data, onChange }: { data: TwitterSourceConfig; onChange: (d: TwitterSourceConfig) => void }) {
  const t = useT();
  const set = (k: keyof TwitterSourceConfig, v: any) => onChange({ ...data, [k]: v });
  return (
    <>
      <h4 className="font-medium text-sm text-gray-700 dark:text-gray-300 border-b dark:border-slate-700 pb-2">{t("gallerydl.auth")}</h4>
      <div className="grid grid-cols-2 gap-4">
        <TextField label={t("gallerydl.cookies_path")} value={str(data.cookies_path)} onChange={(v) => set("cookies_path", v || undefined)} placeholder="/gallerydl-config/cookies/twitter.txt" />
      </div>
      <div>
        <label className="block text-sm font-medium mb-1">{t("gallerydl.cookie_content")}</label>
        <textarea value={str(data.cookie_content)} onChange={(e) => set("cookie_content", e.target.value || undefined)}
          rows={3} className="w-full border rounded px-3 py-2 text-xs font-mono dark:bg-slate-700 dark:text-white"
          placeholder="Paste cookie text here. Auto-saved to /gallerydl-config/cookies/twitter.txt" />
        <p className="text-xs text-gray-400 mt-1">{t("gallerydl.cookies_help")}</p>
      </div>
      <h4 className="font-medium text-sm text-gray-700 dark:text-gray-300 border-b dark:border-slate-700 pb-2">{t("gallerydl.content")}</h4>
      <div className="grid grid-cols-2 gap-4">
        <SelectField label={t("gallerydl.include")} value={str(data.include, "timeline")} onChange={(v) => set("include", v)}
          options={[
            { value: "timeline", label: t("gallerydl.timeline") }, { value: "media", label: t("gallerydl.media_only") },
            { value: "tweets", label: t("gallerydl.tweets") }, { value: "likes", label: t("gallerydl.likes") },
          ]} />
        <NumberField label={t("gallerydl.max_posts")} value={numStr(data.max_posts)} onChange={(v) => set("max_posts", parseInt(v) || undefined)} placeholder="Unlimited" />
      </div>
      <h4 className="font-medium text-sm text-gray-700 dark:text-gray-300 border-b dark:border-slate-700 pb-2">{t("gallerydl.filters")}</h4>
      <div className="space-y-1">
        <ToggleField label={t("gallerydl.retweets")} desc={t("gallerydl.include_retweets")} value={data.retweets ?? false} onChange={(v) => set("retweets", v)} />
        <ToggleField label={t("gallerydl.replies")} desc={t("gallerydl.include_replies")} value={data.replies ?? false} onChange={(v) => set("replies", v)} />
        <ToggleField label={t("gallerydl.cards")} desc={t("gallerydl.cards.desc")} value={data.cards ?? true} onChange={(v) => set("cards", v)} />
        <ToggleField label={t("gallerydl.videos")} desc={t("gallerydl.videos.desc")} value={data.videos ?? true} onChange={(v) => set("videos", v)} />
        <ToggleField label={t("gallerydl.text_tweets")} desc={t("gallerydl.text_tweets.desc")} value={data.text_tweets ?? false} onChange={(v) => set("text_tweets", v)} />
        <ToggleField label={t("gallerydl.quoted")} desc={t("gallerydl.quoted.desc")} value={data.quoted ?? false} onChange={(v) => set("quoted", v)} />
      </div>
      <h4 className="font-medium text-sm text-gray-700 dark:text-gray-300 border-b dark:border-slate-700 pb-2">{t("gallerydl.file_org")}</h4>
      <div className="grid grid-cols-2 gap-4">
        <TextField label={t("gallerydl.dir_pattern")} value={str(data.directory)} onChange={(v) => set("directory", v || undefined)} placeholder="twitter/{user[name]}" />
        <TextField label={t("gallerydl.filename_pattern")} value={str(data.filename)} onChange={(v) => set("filename", v || undefined)} placeholder="{tweet_id}_{num}.{extension}" />
      </div>
    </>
  );
}

// ── Iwara Tab ──

function IwaraTab({ data, onChange }: { data: IwaraSourceConfig; onChange: (d: IwaraSourceConfig) => void }) {
  const t = useT();
  const set = (k: keyof IwaraSourceConfig, v: any) => onChange({ ...data, [k]: v });
  return (
    <>
      <h4 className="font-medium text-sm text-gray-700 dark:text-gray-300 border-b dark:border-slate-700 pb-2">{t("gallerydl.auth")}</h4>
      <div className="grid grid-cols-2 gap-4">
        <TextField label={t("gallerydl.username")} value={str(data.username)} onChange={(v) => set("username", v || undefined)} placeholder="Iwara account username/email" />
        <TextField label={t("gallerydl.password")} value={str(data.password)} onChange={(v) => set("password", v || undefined)} type="password" placeholder="Iwara account password" />
        <TextField label={t("gallerydl.cookies_path")} value={str(data.cookies_path)} onChange={(v) => set("cookies_path", v || undefined)} placeholder="/gallerydl-config/cookies/iwara.txt" />
      </div>
      <div>
        <label className="block text-sm font-medium mb-1">{t("gallerydl.cookie_content")}</label>
        <textarea value={str(data.cookie_content)} onChange={(e) => set("cookie_content", e.target.value || undefined)}
          rows={3} className="w-full border rounded px-3 py-2 text-xs font-mono dark:bg-slate-700 dark:text-white"
          placeholder="Paste cookie text here. Auto-saved to /gallerydl-config/cookies/iwara.txt" />
        <p className="text-xs text-gray-400 mt-1">{t("gallerydl.cookies_help")}</p>
      </div>
      <h4 className="font-medium text-sm text-gray-700 dark:text-gray-300 border-b dark:border-slate-700 pb-2 pt-4">{t("gallerydl.video_quality")}</h4>
      <TextField label={t("gallerydl.format")} value={str(data.format)} onChange={(v) => set("format", v || undefined)} placeholder="Source, 540, 360" />
      <p className="text-xs text-gray-500 dark:text-gray-400 mt-2">{t("gallerydl.format.desc")}</p>
      <h4 className="font-medium text-sm text-gray-700 dark:text-gray-300 border-b dark:border-slate-700 pb-2 pt-4">{t("gallerydl.file_org")}</h4>
      <div className="grid grid-cols-2 gap-4">
        <TextField label={t("gallerydl.dir_pattern")} value={str(data.directory)} onChange={(v) => set("directory", v || undefined)} placeholder="iwara/{user[name]}" />
        <TextField label={t("gallerydl.filename_pattern")} value={str(data.filename)} onChange={(v) => set("filename", v || undefined)} />
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

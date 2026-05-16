"use client";
import { useState, useEffect, useRef } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { PageHeader, ErrorState } from "@/components";
import Link from "next/link";

export default function GalleryDLConfigPage() {
  const qc = useQueryClient();
  const config = useQuery({ queryKey: ["gallerydl-config"], queryFn: api.getGalleryDLConfig });
  const [saved, setSaved] = useState(false);

  const [token, setToken] = useState("");
  const [cookies, setCookies] = useState("");
  const [directory, setDirectory] = useState("");
  const [filename, setFilename] = useState("");
  const [include, setInclude] = useState("artworks");
  const [tags, setTags] = useState("japanese");
  const [ugoira, setUgoira] = useState(true);
  const [maxPosts, setMaxPosts] = useState("");
  const [sleep, setSleep] = useState("");
  const seeded = useRef(false);

  // ALL hooks must be called before any conditional returns
  const save = useMutation({
    mutationFn: () => api.updateGalleryDLConfig({
      refresh_token: token || undefined,
      cookies_path: cookies || undefined,
      filename: filename || undefined,
      directory: directory || undefined,
      include: include || undefined,
      tags: tags || undefined,
      ugoira,
      sleep_request: sleep ? parseFloat(sleep) : undefined,
      max_posts: maxPosts ? parseInt(maxPosts) : undefined,
    }),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["gallerydl-config"] }); setSaved(true); setTimeout(() => setSaved(false), 3000); },
  });

  useEffect(() => {
    if (config.data && !seeded.current) {
      const d = config.data as Record<string, unknown>;
      setToken(String(d["refresh_token"] || ""));
      setCookies(String(d["cookies_path"] || ""));
      setDirectory(String(d["directory"] || ""));
      setFilename(String(d["filename"] || ""));
      setInclude(String(d["include"] || "artworks"));
      setTags(String(d["tags"] || "japanese"));
      setUgoira(Boolean(d["ugoira"] ?? true));
      setMaxPosts(String(d["max_posts"] || ""));
      setSleep(String(d["sleep_request"] || ""));
      seeded.current = true;
    }
  }, [config.data]);

  // Conditional rendering AFTER all hooks
  if (config.isError) {
    return (
      <main className="max-w-4xl mx-auto p-6">
        <ErrorState message={config.error?.message || "Failed"} onRetry={() => config.refetch()} />
      </main>
    );
  }
  if (!config.data) {
    return (
      <main className="max-w-4xl mx-auto p-6">
        <div className="animate-pulse space-y-4"><div className="h-8 bg-gray-200 rounded w-1/3" /><div className="h-64 bg-gray-200 rounded" /></div>
      </main>
    );
  }

  return (
    <main className="max-w-4xl mx-auto p-6">
      <div className="flex items-center gap-4 mb-6">
        <Link href="/admin/settings" className="text-sm text-blue-600 hover:underline">&larr; Settings</Link>
      </div>
      <PageHeader title="gallery-dl — Pixiv Configuration" description="Edit extractor options. Saved to config.json." />

      <div className="bg-white rounded-lg shadow p-6 space-y-5">
        <h4 className="font-medium text-sm text-gray-700 border-b pb-2">Authentication</h4>
        <div className="grid grid-cols-2 gap-4">
          <div>
            <label className="block text-sm font-medium mb-1">Refresh Token</label>
            <input type="password" value={token} onChange={(e) => setToken(e.target.value)}
              className="w-full border rounded px-3 py-2 text-sm font-mono" />
          </div>
          <div>
            <label className="block text-sm font-medium mb-1">Cookies Path</label>
            <input type="text" value={cookies} onChange={(e) => setCookies(e.target.value)}
              className="w-full border rounded px-3 py-2 text-sm font-mono" placeholder="/gallerydl-config/cookies/pixiv.txt" />
          </div>
        </div>

        <h4 className="font-medium text-sm text-gray-700 border-b pb-2">Content</h4>
        <div className="grid grid-cols-2 gap-4">
          <div>
            <label className="block text-sm font-medium mb-1">Include</label>
            <select value={include} onChange={(e) => setInclude(e.target.value)}
              className="w-full border rounded px-3 py-2 text-sm bg-white">
              <option value="artworks">artworks</option>
              <option value="favorites">favorites</option>
              <option value="bookmarks">bookmarks</option>
            </select>
          </div>
          <div>
            <label className="block text-sm font-medium mb-1">Tag Language</label>
            <select value={tags} onChange={(e) => setTags(e.target.value)}
              className="w-full border rounded px-3 py-2 text-sm bg-white">
              <option value="japanese">japanese</option>
              <option value="english">english</option>
              <option value="translated">translated</option>
            </select>
          </div>
          <div>
            <label className="block text-sm font-medium mb-1">Max Posts</label>
            <input type="text" value={maxPosts} onChange={(e) => setMaxPosts(e.target.value)}
              className="w-full border rounded px-3 py-2 text-sm font-mono" placeholder="Unlimited" />
          </div>
          <div className="flex items-end pb-2">
            <label className="flex items-center gap-2 cursor-pointer">
              <input type="checkbox" checked={ugoira} onChange={(e) => setUgoira(e.target.checked)} className="rounded" />
              <span className="text-sm font-medium">Download Ugoira</span>
            </label>
          </div>
        </div>

        <h4 className="font-medium text-sm text-gray-700 border-b pb-2">File Organization</h4>
        <div className="grid grid-cols-2 gap-4">
          <div>
            <label className="block text-sm font-medium mb-1">Directory Pattern</label>
            <input type="text" value={directory} onChange={(e) => setDirectory(e.target.value)}
              className="w-full border rounded px-3 py-2 text-sm font-mono" />
          </div>
          <div>
            <label className="block text-sm font-medium mb-1">Filename Pattern</label>
            <input type="text" value={filename} onChange={(e) => setFilename(e.target.value)}
              className="w-full border rounded px-3 py-2 text-sm font-mono" placeholder="{id}_p{num}.{extension}" />
          </div>
        </div>

        <h4 className="font-medium text-sm text-gray-700 border-b pb-2">Rate Limiting</h4>
        <div className="w-64">
          <label className="block text-sm font-medium mb-1">Sleep (seconds)</label>
          <input type="text" value={sleep} onChange={(e) => setSleep(e.target.value)}
            className="w-full border rounded px-3 py-2 text-sm font-mono" placeholder="0" />
        </div>

        <div className="flex justify-end pt-4 border-t">
          <button onClick={() => save.mutate()} disabled={save.isPending}
            className={`px-6 py-2 rounded text-sm font-medium text-white ${saved ? "bg-green-600" : "bg-slate-900 hover:bg-slate-800"} disabled:opacity-50`}>
            {save.isPending ? "Saving..." : saved ? "Saved!" : "Save Config"}
          </button>
        </div>
        {save.error && <p className="text-red-600 text-sm mt-2">{(save.error as Error).message}</p>}
      </div>
    </main>
  );
}

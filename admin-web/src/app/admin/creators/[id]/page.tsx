"use client";
import { useState } from "react";
import { useParams, useRouter } from "next/navigation";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api, queryKeys, CreatorLink as CreatorLinkType } from "@/lib/api";
import { PageHeader, StatusBadge, SourceBadge, Modal, WorkGrid } from "@/components";
import { useToast } from "@/components/Toast";
import { useT } from "@/lib/i18n";

export default function CreatorDetailPage() {
  const t = useT();
  const toast = useToast();
  const params = useParams(); const router = useRouter(); const qc = useQueryClient();
  const id = params.id as string;
  const creator = useQuery({ queryKey: queryKeys.creators.detail(id), queryFn: () => api.getCreator(id) });
  const links = useQuery({ queryKey: queryKeys.creators.links(id), queryFn: () => api.listCreatorLinks(id) });
  const sourceCreators = useQuery({ queryKey: ["source-creators", id], queryFn: () => api.listSourceCreators(id) });
  const timeline = useQuery({ queryKey: ["creator-timeline", id], queryFn: () => api.getCreatorTimeline(id) });
  const [showAddLink, setShowAddLink] = useState(false);
  const [editing, setEditing] = useState(false);
  const [editName, setEditName] = useState(""); const [editDisplay, setEditDisplay] = useState(""); const [editDesc, setEditDesc] = useState("");

  const openEdit = () => {
    if (!creator.data) return;
    setEditName(creator.data.name); setEditDisplay(creator.data.display_name || ""); setEditDesc(creator.data.description || ""); setEditing(true);
  };
  const update = useMutation({
    mutationFn: () => api.updateCreator(id, { name: editName, display_name: editDisplay || undefined, description: editDesc || undefined }),
    onSuccess: () => { qc.invalidateQueries({ queryKey: queryKeys.creators.detail(id) }); setEditing(false); toast.success("已保存"); },
    onError: (e: Error) => toast.error(e.message),
  });
  const toggleFavorite = useMutation({
    mutationFn: (cid: string) => api.toggleCreatorFavorite(cid),
    onSuccess: () => qc.invalidateQueries({ queryKey: queryKeys.creators.detail(id) }),
  });

  if (creator.isLoading) return <main className="max-w-5xl mx-auto p-10"><div className="space-y-6">{Array.from({ length: 4 }).map((_, i) => <div key={i} className="card p-8 skeleton rounded-xl" />)}</div></main>;
  if (creator.error) return <main className="max-w-5xl mx-auto p-10"><div className="card p-6 text-red-600">{(creator.error as Error).message}</div></main>;
  if (!creator.data) return null;
  const c = creator.data;

  return (
    <main className="max-w-5xl mx-auto p-6 md:p-10 page-transition">
      {/* Hero header */}
      <div className="flex flex-col md:flex-row md:items-end justify-between gap-6 mb-10">
        <div>
          <h1 className="text-4xl md:text-5xl font-bold tracking-tight text-stone-900 dark:text-stone-100"
            style={{ fontFamily: "'Playfair Display', Georgia, serif" }}>
            {c.display_name || c.name}
          </h1>
          {c.display_name && c.display_name !== c.name && (
            <p className="text-stone-500 dark:text-stone-400 text-sm mt-1 font-mono">{c.name}</p>
          )}
          <div className="flex items-center gap-3 mt-3">
            <StatusBadge status={c.is_active ? "up" : "down"} />
            {c.is_favorite && <span className="text-amber-500 text-lg">★</span>}
            {c.danbooru_artist_id && (
              <a href={`https://danbooru.donmai.us/artists/${c.danbooru_artist_id}`} target="_blank" rel="noopener"
                className="text-xs text-amber-700 dark:text-amber-400 hover:underline font-mono">danbooru #{c.danbooru_artist_id}</a>
            )}
          </div>
        </div>
        <div className="flex gap-2 shrink-0">
          <button onClick={() => toggleFavorite.mutate(id)}
            className={`text-xl px-3 py-2 rounded-lg border transition-all ${c.is_favorite ? "border-amber-300 bg-amber-50 dark:bg-amber-900/20 text-amber-600" : "border-stone-300 dark:border-stone-600 text-stone-400 hover:text-amber-500"}`}>
            {c.is_favorite ? "★" : "☆"}
          </button>
          <button onClick={openEdit} className="btn-primary">✎ {t("creator_detail.edit")}</button>
        </div>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-8 mb-10">
        {/* Details */}
        <div className="lg:col-span-1 space-y-6">
          <div className="card-elevated p-6">
            <h3 className="text-sm font-semibold uppercase tracking-wider text-stone-500 dark:text-stone-400 mb-4">{t("creator_detail.details")}</h3>
            <dl className="space-y-3 text-sm">
              {c.description && <div><dt className="text-xs text-stone-400 uppercase tracking-wide mb-1">{t("creator_detail.description")}</dt><dd className="text-stone-700 dark:text-stone-300 leading-relaxed whitespace-pre-wrap">{c.description}</dd></div>}
              <div className="flex justify-between py-1 divider"><dt className="text-stone-500">{t("creator_detail.status")}</dt><dd><StatusBadge status={c.is_active ? "up" : "down"} /></dd></div>
              <div className="flex justify-between py-1"><dt className="text-stone-500">{t("creator_detail.created")}</dt><dd className="text-xs text-stone-600 dark:text-stone-400">{new Date(c.created_at).toLocaleDateString()}</dd></div>
            </dl>
          </div>

          {/* Links */}
          <div className="card-elevated p-6">
            <div className="flex items-center justify-between mb-4">
              <h3 className="text-sm font-semibold uppercase tracking-wider text-stone-500 dark:text-stone-400">{t("creator_detail.links")} ({links.data?.length || 0})</h3>
              <button onClick={() => setShowAddLink(true)} className="text-xs text-amber-700 dark:text-amber-400 hover:underline">+ Add</button>
            </div>
            {links.data?.length ? (
              <div className="space-y-2">
                {links.data.map((l: CreatorLinkType) => (
                  <div key={l.id} className="flex items-center gap-2 text-xs py-1">
                    <SourceBadge source={l.link_type} />
                    <a href={l.url} target="_blank" className="text-blue-600 dark:text-blue-400 hover:underline truncate flex-1">{l.url}</a>
                    <span className="text-stone-400">{l.confidence.toFixed(1)}</span>
                  </div>
                ))}
              </div>
            ) : <p className="text-xs text-stone-400">{t("creator_detail.no_links")}</p>}
          </div>

          {/* Danbooru Reference — clickable alias chips to set display name */}
          {c.danbooru_artist_id ? (
            <DanbooruAliases artistId={c.danbooru_artist_id} creatorName={c.name} currentDisplay={c.display_name}
              onSelectAlias={(alias) => { setEditName(c.name); setEditDisplay(alias); setEditDesc(c.description || ""); setEditing(true); }} />
          ) : null}

          {/* Source Creators */}
          {sourceCreators.data?.length ? (
            <div className="card-elevated p-6">
              <h3 className="text-sm font-semibold uppercase tracking-wider text-stone-500 dark:text-stone-400 mb-4">Source Accounts</h3>
              <div className="space-y-2">
                {sourceCreators.data.map((sc: any) => (
                  <div key={sc.id} className="flex items-center gap-2 text-xs">
                    <SourceBadge source={sc.source} />
                    <span className="font-mono text-stone-500">{sc.source_creator_id}</span>
                    {sc.display_name && <span className="text-stone-600 dark:text-stone-400">{sc.display_name}</span>}
                  </div>
                ))}
              </div>
            </div>
          ) : null}
        </div>

        {/* Works Timeline */}
        <div className="lg:col-span-2">
          <div className="card-elevated p-6">
            <div className="flex items-center justify-between mb-6">
              <h2 className="text-xl font-bold text-stone-900 dark:text-stone-100" style={{ fontFamily: "'Playfair Display', Georgia, serif" }}>
                {t("creator_detail.works_timeline")}
              </h2>
              <button onClick={() => router.push(`/admin/works?creator=${id}`)}
                className="text-xs text-amber-700 dark:text-amber-400 hover:underline">{t("creator_detail.view_all_works")} →</button>
            </div>
            <WorkGrid data={timeline.data} loading={timeline.isLoading} />
          </div>
        </div>
      </div>

      <Modal open={showAddLink} onClose={() => setShowAddLink(false)} title="Add Link">
        <AddLinkForm creatorId={id} onClose={() => setShowAddLink(false)} />
      </Modal>
      <Modal open={editing} onClose={() => setEditing(false)} title={t("creator_detail.edit_title")}>
        <div className="space-y-4">
          <div><label className="block text-sm font-medium mb-1">{t("creator_detail.name_field")}</label><input value={editName} onChange={(e) => setEditName(e.target.value)} className="w-full border rounded-lg px-3 py-2 text-sm dark:bg-stone-800 dark:text-white dark:border-stone-600" /></div>
          <div><label className="block text-sm font-medium mb-1">{t("creator_detail.display_name_field")}</label><input value={editDisplay} onChange={(e) => setEditDisplay(e.target.value)} className="w-full border rounded-lg px-3 py-2 text-sm dark:bg-stone-800 dark:text-white dark:border-stone-600" /></div>
          <div><label className="block text-sm font-medium mb-1">{t("creator_detail.description_field")}</label><textarea value={editDesc} onChange={(e) => setEditDesc(e.target.value)} className="w-full border rounded-lg px-3 py-2 text-sm dark:bg-stone-800 dark:text-white dark:border-stone-600" rows={3} /></div>
          <div className="flex justify-end gap-3 pt-2">
            <button onClick={() => setEditing(false)} className="btn-ghost">{t("creator_detail.cancel")}</button>
            <button onClick={() => update.mutate()} disabled={update.isPending} className="btn-primary">{t("creator_detail.save")}</button>
          </div>
        </div>
      </Modal>
    </main>
  );
}

function DanbooruAliases({ artistId, creatorName, currentDisplay, onSelectAlias }: {
  artistId: number; creatorName: string; currentDisplay?: string; onSelectAlias: (alias: string) => void;
}) {
  const t = useT();
  const aliases = useQuery({
    queryKey: ["danbooru-artist", artistId],
    queryFn: () => api.previewDanbooruArtist({ name: creatorName }),
    staleTime: 5 * 60 * 1000,
  });

  if (aliases.isLoading) {
    return (
      <div className="card-elevated p-6">
        <div className="animate-pulse space-y-2">
          <div className="h-4 bg-stone-200 dark:bg-stone-700 rounded w-1/3" />
          <div className="flex gap-2"><div className="h-6 bg-stone-200 dark:bg-stone-700 rounded w-16" /><div className="h-6 bg-stone-200 dark:bg-stone-700 rounded w-20" /></div>
        </div>
      </div>
    );
  }

  if (!aliases.data?.artist) {
    return (
      <div className="card-elevated p-6">
        <h3 className="text-sm font-semibold uppercase tracking-wider text-stone-500 dark:text-stone-400 mb-2">{t("creator_detail.danbooru_ref")}</h3>
        <p className="text-xs text-stone-400">Danbooru #{artistId}</p>
      </div>
    );
  }

  const artist = aliases.data.artist;
  const names = [
    ...(artist.pixiv_display_name ? [{ label: artist.pixiv_display_name, type: "pixiv" as const }] : []),
    ...(artist.other_names || []).map((n: string) => ({ label: n, type: "danbooru" as const })),
  ];

  if (names.length === 0) return null;

  return (
    <div className="card-elevated p-6">
      <h3 className="text-sm font-semibold uppercase tracking-wider text-stone-500 dark:text-stone-400 mb-2">{t("creator_detail.danbooru_ref")}</h3>
      <p className="text-[10px] text-stone-400 mb-3">
        {t("creator_detail.danbooru_aliases_hint")}
      </p>
      <div className="flex flex-wrap gap-1.5">
        {names.map(({ label, type }) => {
          const isActive = currentDisplay === label;
          return (
            <button
              key={label}
              type="button"
              onClick={() => onSelectAlias(label)}
              title={t("creator_detail.set_display_name_as").replace("{name}", label)}
              className={`text-xs px-2 py-1 rounded-full border transition-colors cursor-pointer ${
                isActive
                  ? "bg-amber-100 border-amber-400 text-amber-800 dark:bg-amber-900/40 dark:border-amber-600 dark:text-amber-300"
                  : type === "pixiv"
                    ? "bg-blue-50 border-blue-200 text-blue-700 hover:bg-blue-100 dark:bg-blue-900/20 dark:border-blue-800 dark:text-blue-300 dark:hover:bg-blue-900/40"
                    : "bg-stone-100 border-stone-300 text-stone-700 hover:bg-stone-200 dark:bg-stone-700 dark:border-stone-600 dark:text-stone-300 dark:hover:bg-stone-600"
              }`}
            >
              {type === "pixiv" && <span className="opacity-60 mr-0.5">P</span>}
              {label}
              {isActive && <span className="ml-1 text-[10px]">✓</span>}
            </button>
          );
        })}
      </div>
    </div>
  );
}

function AddLinkForm({ creatorId, onClose }: { creatorId: string; onClose: () => void }) {
  const t = useT();
  const [url, setUrl] = useState(""); const [linkType, setLinkType] = useState("website");
  const toast = useToast(); const qc = useQueryClient();
  const create = useMutation({
    mutationFn: () => api.createCreatorLink(creatorId, { url, link_type: linkType }),
    onSuccess: () => { qc.invalidateQueries({ queryKey: queryKeys.creators.links(creatorId) }); onClose(); toast.success("Link added"); },
    onError: (e: Error) => toast.error(e.message),
  });
  return (
    <div className="space-y-4">
      <div><label className="block text-sm font-medium mb-1">URL</label><input value={url} onChange={(e) => setUrl(e.target.value)} className="w-full border rounded-lg px-3 py-2 text-sm dark:bg-stone-800 dark:text-white dark:border-stone-600" placeholder="https://..." /></div>
      <div><label className="block text-sm font-medium mb-1">Type</label>
        <select value={linkType} onChange={(e) => setLinkType(e.target.value)} className="w-full border rounded-lg px-3 py-2 text-sm dark:bg-stone-800 dark:text-white dark:border-stone-600">
          {["website", "pixiv", "x", "iwara", "danbooru", "other"].map(o => <option key={o} value={o}>{o}</option>)}
        </select>
      </div>
      <div className="flex justify-end gap-3 pt-2">
        <button onClick={onClose} className="btn-ghost">Cancel</button>
        <button onClick={() => create.mutate()} disabled={!url || create.isPending} className="btn-primary">Add</button>
      </div>
    </div>
  );
}

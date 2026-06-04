"use client";
import { useState, useMemo } from "react";
import { useParams, useRouter } from "next/navigation";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api, queryKeys, CreatorLink as CreatorLinkType } from "@/lib/api";
import { StatusBadge, SourceBadge, Modal, WorkGrid } from "@/components";
import { useToast } from "@/components/Toast";
import { useT } from "@/lib/i18n";

import { SOURCE_COLORS, CHART_COLORS } from "@/lib/sourceColors";

function AnimatedNumber({ value }: { value: number }) {
  return (
    <span className="tabular-nums font-bold" style={{ fontVariantNumeric: "tabular-nums" }}>
      {value.toLocaleString()}
    </span>
  );
}

function ProgressBar({ value, max, color, className }: { value: number; max: number; color: string; className?: string }) {
  const pct = max > 0 ? Math.min((value / max) * 100, 100) : 0;
  return (
    <div className={`w-full h-1.5 bg-stone-100 dark:bg-stone-700 rounded-full overflow-hidden ${className || ""}`}>
      <div className="h-full rounded-full transition-all duration-700 ease-out animate-bar-grow"
        style={{ width: `${pct}%`, backgroundColor: color }} />
    </div>
  );
}

function HorizontalBarChart({ data, maxKey, labelKey, colorFn, className }: {
  data: { [k: string]: any }[];
  maxKey: string;
  labelKey: string;
  colorFn: (item: any, i: number) => string;
  className?: string;
}) {
  const max = data.length > 0 ? Math.max(...data.map((d) => d[maxKey] as number)) : 1;
  return (
    <div className={`space-y-1.5 ${className || ""}`}>
      {data.map((item, i) => (
        <div key={i} className="flex items-center gap-2 text-xs page-item" style={{ animationDelay: `${i * 40}ms` }}>
          <span className="w-20 truncate text-right text-stone-600 dark:text-stone-400 shrink-0">{String(item[labelKey])}</span>
          <div className="flex-1 h-2 bg-stone-100 dark:bg-stone-700 rounded-full overflow-hidden">
            <div className="h-full rounded-full transition-all duration-700 ease-out animate-bar-grow"
              style={{ width: `${((item[maxKey] as number) / max) * 100}%`, backgroundColor: colorFn(item, i) }} />
          </div>
          <span className="w-10 text-right tabular-nums text-stone-500 dark:text-stone-400 shrink-0">{(item[maxKey] as number).toLocaleString()}</span>
        </div>
      ))}
    </div>
  );
}

// Mini monthly posting heat strip
function MonthStrip({ data, className }: { data: { month: string; count: number }[]; className?: string }) {
  if (!data.length) return null;
  const max = Math.max(...data.map((d) => d.count), 1);
  return (
    <div className={`flex items-end gap-[2px] h-12 ${className || ""}`}>
      {data.slice(-48).map((d, i) => {
        const h = Math.max(2, (d.count / max) * 100);
        return (
          <div key={d.month} className="flex-1 min-w-[3px] rounded-t-sm transition-all duration-300 hover:opacity-70 cursor-default"
            style={{ height: `${h}%`, backgroundColor: "#0066FF", opacity: 0.3 + (d.count / max) * 0.7 }}
            title={`${d.month}: ${d.count} works`} />
        );
      })}
    </div>
  );
}

export default function CreatorDetailPage() {
  const t = useT(); const toast = useToast();
  const params = useParams(); const router = useRouter(); const qc = useQueryClient();
  const id = params.id as string;
  const creator = useQuery({ queryKey: queryKeys.creators.detail(id), queryFn: () => api.getCreator(id) });
  const links = useQuery({ queryKey: queryKeys.creators.links(id), queryFn: () => api.listCreatorLinks(id) });
  const timeline = useQuery({ queryKey: ["creator-timeline", id], queryFn: () => api.getCreatorTimeline(id) });
  const stats = useQuery({ queryKey: ["creator-stats", id], queryFn: () => api.getCreatorStats(id) });
  const [showAddLink, setShowAddLink] = useState(false);
  const [editing, setEditing] = useState(false);
  const [editName, setEditName] = useState(""); const [editDisplay, setEditDisplay] = useState(""); const [editDesc, setEditDesc] = useState("");

  // Debounce edit modal values
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

  const st = stats.data;

  if (creator.isLoading) return <main className="max-w-6xl mx-auto p-6 md:p-10"><div className="space-y-6">{Array.from({ length: 4 }).map((_, i) => <div key={i} className="rounded-2xl bg-white dark:bg-stone-800 p-8 animate-pulse" />)}</div></main>;
  if (creator.error) return <main className="max-w-6xl mx-auto p-10"><div className="rounded-2xl bg-white dark:bg-stone-800 p-6 text-red-600">{(creator.error as Error).message}</div></main>;
  if (!creator.data) return null;
  const c = creator.data;

  return (
    <main className="max-w-6xl mx-auto p-4 md:p-8 page-transition">
      {/* ═══ Hero ═══ */}
      <div className="flex flex-col md:flex-row md:items-end justify-between gap-4 mb-8 page-item">
        <div>
          <h1 className="text-3xl md:text-4xl font-bold tracking-tight text-stone-900 dark:text-stone-100"
            style={{ fontFamily: "'Playfair Display', Georgia, serif" }}>
            {c.display_name || c.name}
          </h1>
          {c.display_name && c.display_name !== c.name && (
            <p className="text-stone-400 dark:text-stone-500 text-sm mt-0.5 font-mono">{c.name}</p>
          )}
          <div className="flex items-center gap-3 mt-2">
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
            className={`text-xl px-3 py-2 rounded-xl border transition-all duration-200 ${c.is_favorite ? "border-amber-300 bg-amber-50 dark:bg-amber-900/20 text-amber-600 shadow-sm" : "border-stone-200 dark:border-stone-600 text-stone-400 hover:text-amber-500 hover:border-amber-200"}`}>
            {c.is_favorite ? "★" : "☆"}
          </button>
          <button onClick={() => router.push(`/admin/subscriptions?q=${encodeURIComponent(c.display_name || c.name)}`)}
            className="px-4 py-2 border border-stone-200 dark:border-stone-600 rounded-xl text-sm font-medium hover:bg-stone-50 dark:hover:bg-stone-700/50 transition-colors text-stone-700 dark:text-stone-300">
            {t("creator_detail.view_subscription") || "Subscription"} ↗
          </button>
          <button onClick={openEdit} className="px-4 py-2 bg-stone-900 dark:bg-stone-100 text-white dark:text-stone-900 rounded-xl text-sm font-medium hover:bg-stone-800 dark:hover:bg-stone-200 transition-colors">✎ {t("creator_detail.edit")}</button>
        </div>
      </div>

      {/* ═══ Quick Stats Bar ═══ */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-3 mb-8 page-item" style={{ animationDelay: "100ms" }}>
        {[
          { label: t("creator_detail.stat_works") || "Works", value: st?.total_works, color: "text-blue-600" },
          { label: t("creator_detail.stat_assets") || "Assets", value: st?.total_assets, color: "text-green-600" },
          { label: t("creator_detail.stat_tags") || "Tags", value: st?.total_tags, color: "text-purple-600" },
          { label: t("creator_detail.stat_sources") || "Sources", value: st?.source_breakdown?.length, color: "text-amber-600" },
        ].map((s, i) => (
          <div key={i} className="rounded-2xl bg-white dark:bg-stone-800 p-4 shadow-sm border border-stone-100 dark:border-stone-700/50 hover:shadow-md transition-shadow">
            <div className={`text-2xl font-bold tabular-nums ${s.color}`}>
              {s.value !== undefined ? s.value.toLocaleString() : "—"}
            </div>
            <div className="text-xs text-stone-500 dark:text-stone-400 mt-0.5">{s.label}</div>
          </div>
        ))}
      </div>

      {/* ═══ Main Grid ═══ */}
      <div className="grid grid-cols-1 lg:grid-cols-5 gap-6 mb-10">
        {/* Left Sidebar (2/5) */}
        <div className="lg:col-span-2 space-y-5 page-item" style={{ animationDelay: "150ms" }}>
          {/* Details */}
          <div className="rounded-2xl bg-white dark:bg-stone-800 p-5 shadow-sm border border-stone-100 dark:border-stone-700/50">
            <h3 className="text-xs font-semibold uppercase tracking-wider text-stone-400 dark:text-stone-500 mb-4">{t("creator_detail.details")}</h3>
            {c.description && (
              <p className="text-sm text-stone-700 dark:text-stone-300 leading-relaxed whitespace-pre-wrap mb-4">{c.description}</p>
            )}
            <div className="flex items-center justify-between text-xs py-1.5 border-t border-stone-100 dark:border-stone-700/50">
              <span className="text-stone-500">{t("creator_detail.status")}</span><StatusBadge status={c.is_active ? "up" : "down"} />
            </div>
            <div className="flex items-center justify-between text-xs py-1.5">
              <span className="text-stone-500">{t("creator_detail.created")}</span>
              <span className="text-stone-600 dark:text-stone-400">{new Date(c.created_at).toLocaleDateString()}</span>
            </div>
          </div>

          {/* Links */}
          <div className="rounded-2xl bg-white dark:bg-stone-800 p-5 shadow-sm border border-stone-100 dark:border-stone-700/50">
            <div className="flex items-center justify-between mb-3">
              <h3 className="text-xs font-semibold uppercase tracking-wider text-stone-400 dark:text-stone-500">{t("creator_detail.links").replace("{count}", String(links.data?.length || 0))}</h3>
              <button onClick={() => setShowAddLink(true)} className="text-xs text-amber-700 dark:text-amber-400 hover:underline">+ Add</button>
            </div>
            {links.data?.length ? (
              <div className="space-y-1.5">
                {links.data.map((l: CreatorLinkType) => (
                  <div key={l.id} className="flex items-center gap-2 text-xs py-1 px-2 rounded-lg hover:bg-stone-50 dark:hover:bg-stone-700/30 transition-colors">
                    <SourceBadge source={l.link_type} />
                    <a href={l.url} target="_blank" className="text-blue-600 dark:text-blue-400 hover:underline truncate flex-1">{l.url}</a>
                    <span className="text-stone-400 font-mono">{l.confidence.toFixed(1)}</span>
                  </div>
                ))}
              </div>
            ) : <p className="text-xs text-stone-400">{t("creator_detail.no_links")}</p>}
          </div>

          {/* Danbooru Aliases */}
          {c.danbooru_artist_id && (
            <div className="page-item" style={{ animationDelay: "200ms" }}>
              <DanbooruAliases artistId={c.danbooru_artist_id} currentDisplay={c.display_name}
                onSelectAlias={(alias) => { setEditName(c.name); setEditDisplay(alias); setEditDesc(c.description || ""); setEditing(true); }} />
            </div>
          )}


        </div>

        {/* Right Charts (3/5) */}
        <div className="lg:col-span-3 space-y-5 page-item" style={{ animationDelay: "200ms" }}>
          {/* Source Breakdown */}
          {st?.source_breakdown && st.source_breakdown.length > 0 && (
            <div className="rounded-2xl bg-white dark:bg-stone-800 p-5 shadow-sm border border-stone-100 dark:border-stone-700/50">
              <h3 className="text-xs font-semibold uppercase tracking-wider text-stone-400 dark:text-stone-500 mb-4">{t("creator_detail.source_breakdown") || "Source Breakdown"}</h3>
              <div className="flex flex-wrap gap-4">
                <div className="flex-1 min-w-[140px]">
                  <HorizontalBarChart
                    data={st.source_breakdown}
                    maxKey="count" labelKey="source"
                    colorFn={(d) => SOURCE_COLORS[d.source as string] || CHART_COLORS[0]}
                  />
                </div>
                <div className="flex flex-col gap-1.5 justify-center text-xs">
                  {st.source_breakdown.map((s, i) => (
                    <div key={s.source} className="flex items-center gap-2">
                      <span className="w-2.5 h-2.5 rounded-full shrink-0" style={{ backgroundColor: SOURCE_COLORS[s.source] || CHART_COLORS[i % CHART_COLORS.length] }} />
                      <span className="capitalize text-stone-600 dark:text-stone-400">{s.source}</span>
                      <span className="font-mono tabular-nums text-stone-500">{s.count}</span>
                    </div>
                  ))}
                </div>
              </div>
            </div>
          )}

          {/* Tag Distribution */}
          {st?.tag_distribution && st.tag_distribution.length > 0 && (
            <div className="rounded-2xl bg-white dark:bg-stone-800 p-5 shadow-sm border border-stone-100 dark:border-stone-700/50">
              <h3 className="text-xs font-semibold uppercase tracking-wider text-stone-400 dark:text-stone-500 mb-4">{t("creator_detail.tag_distribution") || "Top Tags"}</h3>
              <HorizontalBarChart
                data={st.tag_distribution.slice(0, 12)}
                maxKey="count" labelKey="tag"
                colorFn={(_, i) => CHART_COLORS[i % CHART_COLORS.length]}
              />
            </div>
          )}

          {/* Monthly Posting Heat Strip */}
          {st?.monthly_frequency && st.monthly_frequency.length > 0 && (
            <div className="rounded-2xl bg-white dark:bg-stone-800 p-5 shadow-sm border border-stone-100 dark:border-stone-700/50">
              <div className="flex items-center justify-between mb-3">
                <h3 className="text-xs font-semibold uppercase tracking-wider text-stone-400 dark:text-stone-500">{t("creator_detail.posting_frequency") || "Posting Frequency"}</h3>
                <span className="text-[10px] text-stone-400">{st.monthly_frequency.length} months</span>
              </div>
              <MonthStrip data={st.monthly_frequency} />
              <div className="flex justify-between text-[10px] text-stone-400 mt-1.5">
                <span>{st.monthly_frequency[0]?.month}</span>
                <span>{st.monthly_frequency[st.monthly_frequency.length - 1]?.month}</span>
              </div>
            </div>
          )}
        </div>
      </div>

      {/* ═══ Works Timeline ═══ */}
      <div className="rounded-2xl bg-white dark:bg-stone-800 p-5 shadow-sm border border-stone-100 dark:border-stone-700/50 mb-10 page-item" style={{ animationDelay: "300ms" }}>
        <div className="flex items-center justify-between mb-5">
          <h2 className="text-lg font-bold text-stone-900 dark:text-stone-100" style={{ fontFamily: "'Playfair Display', Georgia, serif" }}>
            {t("creator_detail.works_timeline")}
          </h2>
          <button onClick={() => router.push(`/admin/works?creator=${id}`)}
            className="text-xs text-amber-700 dark:text-amber-400 hover:underline transition-colors">{t("creator_detail.view_all_works")} →</button>
        </div>
        <WorkGrid data={timeline.data} loading={timeline.isLoading} />
      </div>

      {/* Modals */}
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

// ── DanbooruAliases Component ──
function DanbooruAliases({ artistId, currentDisplay, onSelectAlias }: {
  artistId: number; currentDisplay?: string; onSelectAlias: (alias: string) => void;
}) {
  const t = useT();
  const aliases = useQuery({
    queryKey: ["danbooru-artist", artistId],
    queryFn: () => api.getDanbooruArtist(artistId),
    staleTime: 10 * 60 * 1000,
  });

  if (aliases.isLoading) {
    return (
      <div className="rounded-2xl bg-white dark:bg-stone-800 p-5 shadow-sm border border-stone-100 dark:border-stone-700/50">
        <div className="animate-pulse space-y-2">
          <div className="h-3 bg-stone-200 dark:bg-stone-700 rounded w-1/3" />
          <div className="flex gap-2"><div className="h-5 bg-stone-200 dark:bg-stone-700 rounded w-14" /><div className="h-5 bg-stone-200 dark:bg-stone-700 rounded w-18" /></div>
        </div>
      </div>
    );
  }

  if (!aliases.data?.artist) {
    return (
      <div className="rounded-2xl bg-white dark:bg-stone-800 p-5 shadow-sm border border-stone-100 dark:border-stone-700/50">
        <h3 className="text-xs font-semibold uppercase tracking-wider text-stone-400 dark:text-stone-500 mb-2">{t("creator_detail.danbooru_ref")}</h3>
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
    <div className="rounded-2xl bg-white dark:bg-stone-800 p-5 shadow-sm border border-stone-100 dark:border-stone-700/50">
      <h3 className="text-xs font-semibold uppercase tracking-wider text-stone-400 dark:text-stone-500 mb-2">{t("creator_detail.danbooru_ref")}</h3>
      <p className="text-[10px] text-stone-400 mb-3">{t("creator_detail.danbooru_aliases_hint")}</p>
      <div className="flex flex-wrap gap-1.5">
        {names.map(({ label, type }) => {
          const isActive = currentDisplay === label;
          return (
            <button key={label} type="button" onClick={() => onSelectAlias(label)}
              title={t("creator_detail.set_display_name_as").replace("{name}", label)}
              className={`text-[11px] px-2.5 py-1 rounded-full border transition-all duration-150 cursor-pointer ${
                isActive
                  ? "bg-amber-100 border-amber-400 text-amber-800 dark:bg-amber-900/40 dark:border-amber-600 dark:text-amber-300 scale-105"
                  : type === "pixiv"
                    ? "bg-blue-50 border-blue-200 text-blue-700 hover:bg-blue-100 dark:bg-blue-900/20 dark:border-blue-800 dark:text-blue-300 dark:hover:bg-blue-900/40 hover:scale-105"
                    : "bg-stone-100 border-stone-200 text-stone-600 hover:bg-stone-200 dark:bg-stone-700 dark:border-stone-600 dark:text-stone-300 dark:hover:bg-stone-600 hover:scale-105"
              }`}>
              {type === "pixiv" && <span className="opacity-50 mr-0.5 text-[10px]">P</span>}
              {label}
              {isActive && <span className="ml-1">✓</span>}
            </button>
          );
        })}
      </div>
    </div>
  );
}

// ── AddLinkForm Component ──
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

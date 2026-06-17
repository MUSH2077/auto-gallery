"use client";
import Link from "next/link";
import { useEffect, useState } from "react";
import { useT } from "@/lib/i18n";
import { useParams, useRouter } from "next/navigation";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api, queryKeys } from "@/lib/api";
import { PageHeader, SourceBadge, ErrorState, EmptyState } from "@/components";

interface WorkSourceData {
  id: string;
  source: string;
  source_work_id: string;
  source_creator_id: string;
  source_url?: string;
  title?: string;
  description?: string;
  posted_at?: string;
  raw_metadata?: Record<string, unknown>;
}

interface AssetData {
  id: string;
  file_name: string;
  file_path: string;
  width?: number;
  height?: number;
  mime_type?: string;
  thumb_sm_path?: string;
  thumb_url?: string;
  preview_url?: string;
  original_url?: string;
  created_at: string;
}

function isArchiveAsset(asset: AssetData | null | undefined) {
  if (!asset) return false;
  return asset.mime_type === "application/zip" || asset.file_name.toLowerCase().endsWith(".zip");
}

function FullImageLightbox({ asset, onClose }: { asset: AssetData | null; onClose: () => void }) {
  const t = useT();
  useEffect(() => {
    if (!asset) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [asset, onClose]);

  if (!asset || isArchiveAsset(asset)) return null;

  return (
    <div
      className="fixed inset-0 z-50 flex flex-col bg-black/95"
      role="dialog"
      aria-modal="true"
      aria-label={asset.file_name}
      onClick={onClose}
    >
      <div className="flex items-center justify-between gap-3 border-b border-white/10 px-4 py-3 text-white">
        <div className="min-w-0">
          <div className="truncate text-sm font-medium">{asset.file_name}</div>
          {asset.width && asset.height && <div className="text-xs text-white/60">{asset.width} &times; {asset.height}</div>}
        </div>
        <div className="flex shrink-0 items-center gap-2">
          <a
            href={asset.original_url || ""}
            target="_blank"
            rel="noopener noreferrer"
            className="rounded-md border border-white/20 px-3 py-1.5 text-sm text-white hover:bg-white/10"
            onClick={(e) => e.stopPropagation()}
          >
            {t("work_detail.open_original")}
          </a>
          <button onClick={onClose} className="rounded-md border border-white/20 px-3 py-1.5 text-sm text-white hover:bg-white/10" aria-label={t("common.close")}>
            {t("common.close")}
          </button>
        </div>
      </div>
      <div className="min-h-0 flex-1 overflow-auto p-4" onClick={(e) => e.stopPropagation()}>
        <img
          src={asset.original_url || ""}
          alt={asset.file_name}
          className="mx-auto h-auto max-h-none max-w-full object-contain"
        />
      </div>
    </div>
  );
}

function AssetThumb({ asset, active, index, onClick }: { asset: AssetData; active: boolean; index: number; onClick: () => void }) {
  const t = useT();
  const [mode, setMode] = useState<"thumb" | "preview" | "error">("thumb");
  useEffect(() => setMode("thumb"), [asset.id]);
  const archive = isArchiveAsset(asset);
  return (
    <button
      onClick={onClick}
      className={`h-16 w-16 shrink-0 overflow-hidden rounded-md border-2 ${active ? "border-[#0969da] dark:border-[#58a6ff]" : "border-ag-border hover:border-[#8c959f] dark:border-ag-border"}`}
      title={asset.file_name}
    >
      {archive || mode === "error" ? (
        <span className="flex h-full w-full items-center justify-center bg-[#f6f8fa] text-[10px] font-medium text-[#57606a] dark:bg-[#21262d] dark:text-[#8b949e]">
          {archive ? t("work_detail.archive_short") : t("works.na")}
        </span>
      ) : (
        <img
          src={mode === "thumb" ? (asset.thumb_url || api.mediaUrl(asset.id, "thumb")) : asset.preview_url || ""}
          alt={`Page ${index + 1}`}
          className="h-full w-full object-cover"
          onError={() => setMode(mode === "thumb" ? "preview" : "error")}
        />
      )}
    </button>
  );
}

function AllPages({ workId, sources }: { workId: string; sources: WorkSourceData[] }) {
  const t = useT();
  const assets = useQuery({ queryKey: ["works", workId, "assets"], queryFn: () => api.getWorkAssets(workId) });
  const [activeIndex, setActiveIndex] = useState(0);
  const [fullAsset, setFullAsset] = useState<AssetData | null>(null);

  if (assets.isLoading) return <div className="card p-4 animate-pulse"><div className="h-24 rounded-md bg-[#eaeef2] dark:bg-[#21262d]" /></div>;
  if (!assets.data || !assets.data.length) return <div className="card p-4"><EmptyState title={t("work_detail.no_assets_title", "No assets")} description={t("work_detail.no_assets")} /></div>;

  const current = assets.data[activeIndex] as AssetData;
  const totalPages = assets.data.length;
  const ws = sources[0];
  const currentIsArchive = isArchiveAsset(current);

  return (
    <div className="card p-4">
      <h3 className="font-medium mb-2 text-sm">{t("work_detail.pages_section", { count: totalPages })}</h3>
      {current && (
        <div className="mb-3">
          {currentIsArchive ? (
            <div className="rounded-md border border-ag-border bg-[#f6f8fa] p-6 text-center dark:border-ag-border dark:bg-[#21262d]">
              <div className="mx-auto mb-2 flex h-16 w-16 items-center justify-center rounded-md border border-ag-border bg-white font-mono text-sm font-semibold text-[#57606a] dark:border-ag-border dark:bg-ag-bg dark:text-[#8b949e]">ZIP</div>
              <div className="text-sm font-medium text-[#24292f] dark:text-ag-text">{t("work_detail.archive_asset")}</div>
              <div className="mt-1 truncate text-xs text-[#57606a] dark:text-[#8b949e]">{current.file_name}</div>
              <a href={current.original_url || ""} className="btn-ghost mt-3 inline-flex text-xs" target="_blank" rel="noopener noreferrer">
                {t("work_detail.download_original")}
              </a>
            </div>
          ) : (
            <button
              type="button"
              onClick={() => setFullAsset(current)}
              className="group relative block w-full overflow-hidden rounded-md bg-[#f6f8fa] dark:bg-[#21262d]"
              title={t("work_detail.view_full", "View full image")}
            >
              <img src={current.preview_url || ""} alt={current.file_name}
                className="max-h-96 w-full object-contain transition-transform group-hover:scale-[1.01]" />
              <span className="absolute bottom-2 right-2 rounded-md bg-black/70 px-2 py-1 text-xs font-medium text-white opacity-0 transition-opacity group-hover:opacity-100">
                {t("work_detail.view_full")}
              </span>
            </button>
          )}
          <p className="mt-1 text-center text-xs text-[#57606a] dark:text-[#8b949e]">{activeIndex + 1} / {totalPages}</p>
        </div>
      )}
      <div className="flex gap-2 overflow-x-auto pb-1">
        {assets.data.map((a, i) => (
          <AssetThumb key={a.id} asset={a as AssetData} active={i === activeIndex} index={i} onClick={() => setActiveIndex(i)} />
        ))}
      </div>

      {/* Asset metadata for current page */}
      {current && (
        <div className="mt-4 space-y-1 border-t border-ag-border pt-3 text-xs text-[#57606a] dark:border-ag-border dark:text-[#8b949e]">
          <p className="mb-1 font-medium text-[#24292f] dark:text-ag-text">{t("work_detail.current_page")}</p>
          <div className="flex justify-between"><span>{t("work_detail.file")}</span><span className="font-mono">{current.file_name}</span></div>
          {current.width && current.height && (
            <div className="flex justify-between"><span>{t("work_detail.dimensions")}</span><span>{current.width} &times; {current.height}</span></div>
          )}
          {current.mime_type && (
            <div className="flex justify-between"><span>{t("work_detail.format")}</span><span>{current.mime_type}</span></div>
          )}
        </div>
      )}
      <FullImageLightbox asset={fullAsset} onClose={() => setFullAsset(null)} />
    </div>
  );
}

function WorkHistory({ workId }: { workId: string }) {
  const commits = useQuery({
    queryKey: queryKeys.curation.subject("work", workId),
    queryFn: () => api.listCurationCommits({ subject_type: "work", subject_id: workId, limit: 20 }),
  });
  if (commits.isLoading) return <div className="card p-4 animate-pulse"><div className="h-20 rounded bg-[#eaeef2] dark:bg-[#21262d]" /></div>;
  if (!commits.data?.items.length) return <div className="card p-4"><EmptyState title="No history yet" description="Curation commits touching this work will appear here." /></div>;
  return (
    <div className="card p-4">
      <div className="mb-3 flex items-center justify-between gap-3">
        <h3 className="text-sm font-medium uppercase tracking-wide text-gray-400 dark:text-gray-500">History</h3>
        <Link href={`/admin/curation?subject_type=work&subject_id=${workId}`} className="text-xs text-blue-600 hover:underline">Open curation</Link>
      </div>
      <div className="space-y-3">
        {commits.data.items.map((commit) => (
          <div key={commit.id} className="border-l-2 border-[#0969da] pl-3 text-sm dark:border-[#58a6ff]">
            <div className="font-medium">{commit.message}</div>
            <div className="mt-0.5 text-xs text-[#57606a] dark:text-[#8b949e]">
              <span className="font-mono">{commit.id.slice(0, 8)}</span>
              <span className="mx-1.5">·</span>
              <span>{commit.trigger}</span>
              <span className="mx-1.5">·</span>
              <span>{new Date(commit.occurred_at).toLocaleString()}</span>
            </div>
            <div className="mt-1 flex flex-wrap gap-1.5">
              {commit.changes.filter((c) => c.subject_id === workId).map((change) => (
                <span key={change.id} className="rounded-full border border-ag-border px-2 py-0.5 text-[11px] text-[#57606a] dark:border-ag-border dark:text-[#8b949e]">{change.action.replaceAll("_", " ")}</span>
              ))}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

export default function WorkDetailPage() {
  const t = useT();
  const params = useParams();
  const router = useRouter();
  const id = params.id as string;
  const qc = useQueryClient();

  const work = useQuery({ queryKey: queryKeys.works.detail(id), queryFn: () => api.getWork(id) });
  const toggleFavorite = useMutation({
    mutationFn: (workId: string) => api.toggleWorkFavorite(workId),
    onSuccess: (updated) => {
      qc.setQueryData(queryKeys.works.detail(id), updated);
    },
  });
  const sources = useQuery({ queryKey: queryKeys.works.sources(id), queryFn: () => api.getWorkSources(id) });
  const workTags = useQuery({ queryKey: ["works", id, "tags"], queryFn: () => api.getWorkTags(id) });
  const curateWork = useMutation({
    mutationFn: (action: "trash" | "restore") => api.batchCurateWorks([id], action),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: queryKeys.works.detail(id) });
      qc.invalidateQueries({ queryKey: queryKeys.curation.all });
      qc.invalidateQueries({ queryKey: queryKeys.works.all });
    },
  });

  if (work.isLoading) return <main className="max-w-6xl mx-auto p-6"><div className="animate-pulse space-y-4"><div className="h-8 w-1/3 rounded bg-[#eaeef2] dark:bg-[#21262d]" /><div className="h-64 rounded bg-[#eaeef2] dark:bg-[#21262d]" /></div></main>;
  if (work.error) return <main className="max-w-6xl mx-auto p-6"><ErrorState message={(work.error as Error).message} onRetry={() => work.refetch()} /></main>;
  if (!work.data) return null;
  const w = work.data;
  const wsList: WorkSourceData[] = (sources.data || []) as WorkSourceData[];
  const primaryWs = wsList[0];
  const raw = (primaryWs?.raw_metadata || {}) as Record<string, unknown>;

  // Extract rich metadata from raw
  const rawTags: string[] = Array.isArray(raw.tags) ? raw.tags as string[] : [];
  const rawTools: string[] = Array.isArray(raw.tools) ? raw.tools as string[] : [];
  const pageCount = (raw.page_count as number) || w.asset_count || 1;
  const rawWidth = raw.width as number | undefined;
  const rawHeight = raw.height as number | undefined;
  const illustType = raw.type as string | undefined;
  const rating = raw.rating as string | undefined;
  const totalView = raw.total_view as number | undefined;
  const totalBookmarks = raw.total_bookmarks as number | undefined;
  const createDate = (raw.create_date as string) || w.posted_at;
  const aiType = raw.illust_ai_type as number | undefined;
  const series = raw.series as string | undefined;

  const isAiGenerated = aiType !== undefined && aiType == 2;
  const hasStats = totalView !== undefined || totalBookmarks !== undefined;
  const visibility = w.curation_state?.visibility || "visible";

  return (
    <main className="max-w-6xl mx-auto p-6">
      {/* Header */}
      <div className="mb-6 flex items-start gap-3">
        <div className="flex-1 min-w-0">
          <Link href="/admin/works" className="text-sm text-blue-600 hover:underline inline-flex items-center gap-1">&larr; 返回</Link>
      <PageHeader title={w.title || t("work_detail.untitled")}
            description={
              <span className="flex items-center gap-3 flex-wrap">
                {primaryWs && <SourceBadge source={primaryWs.source} />}
                {primaryWs?.source_creator_id && (
                  <span className="text-sm">{t("work_detail.creator_id")} <span className="font-mono">{primaryWs.source_creator_id}</span></span>
                )}
                {rating && <span className="badge border-ag-border bg-[#f6f8fa] text-[#57606a] dark:border-ag-border dark:bg-[#21262d] dark:text-[#8b949e]">{rating}</span>}
                {illustType && <span className="text-xs px-2 py-0.5 rounded bg-purple-100 text-purple-700">{illustType}</span>}
                {w.is_nsfw && <span className="text-xs px-2 py-0.5 rounded bg-red-100 dark:bg-red-900/30 text-red-700 dark:text-red-400">{t("work_detail.nsfw")}</span>}
                {isAiGenerated && <span className="text-xs px-2 py-0.5 rounded bg-yellow-100 text-yellow-700">{t("work_detail.ai_generated")}</span>}
                {visibility !== "visible" && <span className="text-xs px-2 py-0.5 rounded bg-[#cf222e]/10 text-[#cf222e]">{visibility}</span>}
              </span>
            }
          />
        </div>
        <div className="mt-1 flex shrink-0 gap-2">
          {visibility === "visible" ? (
            <button onClick={() => curateWork.mutate("trash")} disabled={curateWork.isPending}
              className="rounded-md border border-ag-border px-3 py-1.5 text-xs font-medium hover:bg-[#f6f8fa] disabled:opacity-50 dark:border-ag-border dark:hover:bg-[#21262d]">
              Move to Trash
            </button>
          ) : visibility === "trashed" ? (
            <button onClick={() => curateWork.mutate("restore")} disabled={curateWork.isPending}
              className="rounded-md border border-ag-border px-3 py-1.5 text-xs font-medium hover:bg-[#f6f8fa] disabled:opacity-50 dark:border-ag-border dark:hover:bg-[#21262d]">
              Restore
            </button>
          ) : null}
        </div>
        <button onClick={() => toggleFavorite.mutate(id)}
          className={`text-2xl shrink-0 mt-1 ${work.data?.is_favorite ? "text-yellow-500" : "text-gray-300 hover:text-yellow-400"}`}
          title={work.data?.is_favorite ? t("common.unfavorite") : t("common.favorite")}>
          {work.data?.is_favorite ? "★" : "☆"}
        </button>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-3 gap-6">
        {/* Left column — metadata */}
        <div className="md:col-span-2 space-y-4">
          {/* Summary Card */}
          <div className="card p-4">
            <h3 className="font-medium mb-3 text-sm uppercase tracking-wide text-gray-400 dark:text-gray-500">{t("work_detail.summary")}</h3>
            <dl className="text-sm space-y-2">
              <div className="flex gap-2"><dt className="text-gray-500 dark:text-gray-400 w-24 shrink-0">{t("work_detail.title")}</dt><dd className="font-medium">{w.title || t("work_detail.untitled")}</dd></div>
              {primaryWs?.source_work_id && (
                <div className="flex gap-2"><dt className="text-gray-500 dark:text-gray-400 w-24 shrink-0">{t("work_detail.work_id")}</dt><dd className="font-mono text-xs">{primaryWs.source_work_id}</dd></div>
              )}
              <div className="flex gap-2"><dt className="text-gray-500 dark:text-gray-400 w-24 shrink-0">{t("work_detail.posted")}</dt><dd>{createDate ? new Date(createDate).toLocaleDateString("en-US", { year: "numeric", month: "long", day: "numeric" }) : t("work_detail.unknown")}</dd></div>
              {rawWidth && rawHeight && (
                <div className="flex gap-2"><dt className="text-gray-500 dark:text-gray-400 w-24 shrink-0">{t("work_detail.dimensions")}</dt><dd>{rawWidth} &times; {rawHeight}</dd></div>
              )}
              <div className="flex gap-2"><dt className="text-gray-500 dark:text-gray-400 w-24 shrink-0">{t("work_detail.pages")}</dt><dd>{pageCount}</dd></div>
              {illustType && (
                <div className="flex gap-2"><dt className="text-gray-500 dark:text-gray-400 w-24 shrink-0">{t("work_detail.type")}</dt><dd className="capitalize">{illustType}</dd></div>
              )}
              <div className="flex gap-2"><dt className="text-gray-500 dark:text-gray-400 w-24 shrink-0">{t("work_detail.imported")}</dt><dd className="text-xs">{new Date(w.created_at).toLocaleString()}</dd></div>
            </dl>
          </div>

          {/* Stats Card */}
          {hasStats && (
            <div className="card p-4">
              <h3 className="font-medium mb-3 text-sm uppercase tracking-wide text-gray-400 dark:text-gray-500">{t("work_detail.stats")}</h3>
              <div className="grid grid-cols-2 gap-4">
                {totalView !== undefined && (
                  <div className="text-center p-3 bg-blue-50 dark:bg-blue-900/30 rounded-lg">
                    <div className="text-2xl font-bold text-blue-700">{totalView.toLocaleString()}</div>
                    <div className="text-xs text-blue-500 mt-1">{t("work_detail.views")}</div>
                  </div>
                )}
                {totalBookmarks !== undefined && (
                  <div className="text-center p-3 bg-pink-50 rounded-lg">
                    <div className="text-2xl font-bold text-pink-700">{totalBookmarks.toLocaleString()}</div>
                    <div className="text-xs text-pink-500 mt-1">{t("work_detail.bookmarks")}</div>
                  </div>
                )}
              </div>
            </div>
          )}

          {/* Tags */}
          {(rawTags.length > 0 || (workTags.data && workTags.data.length > 0)) && (
            <div className="card p-4">
              <h3 className="font-medium mb-3 text-sm uppercase tracking-wide text-gray-400 dark:text-gray-500">
                {t("work_detail.tags")} ({rawTags.length} {t("work_detail.source_tags")}{workTags.data?.length ? ` · ${workTags.data.length} ${t("work_detail.normalized_tags")}` : ""})
              </h3>
              {/* Normalized (work-level) tags */}
              {workTags.data && workTags.data.length > 0 && (
                <div className="flex flex-wrap gap-2 mb-3">
                  {workTags.data.map((t) => (
                    <span key={t.id} onClick={() => router.push(`/admin/search?q=${encodeURIComponent(t.normalized_name)}`)}
                      className="px-3 py-1 bg-blue-50 dark:bg-blue-900/30 text-blue-700 rounded-full text-sm border border-blue-200 dark:border-blue-800 cursor-pointer hover:bg-blue-100 dark:hover:bg-blue-800/50 transition-colors"
                      title={`Search: ${t.normalized_name}${t.category ? ` (${t.category})` : ""}`}>
                      {t.normalized_name}
                      {t.category && <span className="text-xs text-blue-400 ml-1">({t.category})</span>}
                    </span>
                  ))}
                </div>
              )}
              {/* Source tags */}
              {rawTags.length > 0 && (
                <div className="flex flex-wrap gap-2">
                  {rawTags.map((tag, i) => (
                    <span key={i} onClick={() => router.push(`/admin/search?q=${encodeURIComponent(tag)}`)}
                      className="px-3 py-1 bg-gray-100 dark:bg-slate-700 rounded-full text-sm hover:bg-blue-100 hover:text-blue-700 dark:hover:bg-blue-900/30 dark:hover:text-blue-300 cursor-pointer transition-colors"
                      title={`Search source tag: ${tag}`}>
                      {tag}
                    </span>
                  ))}
                </div>
              )}
            </div>
          )}

          {/* Tools */}
          {rawTools.length > 0 && (
            <div className="card p-4">
              <h3 className="font-medium mb-3 text-sm uppercase tracking-wide text-gray-400 dark:text-gray-500">{t("work_detail.tools")}</h3>
              <div className="flex flex-wrap gap-2">
                {rawTools.map((tool, i) => (
                  <span key={i} className="px-3 py-1 bg-indigo-50 text-indigo-700 rounded-full text-sm">{tool}</span>
                ))}
              </div>
            </div>
          )}

          {/* Description */}
          {w.description && (
            <div className="card p-4">
              <h3 className="font-medium mb-2 text-sm uppercase tracking-wide text-gray-400 dark:text-gray-500">{t("work_detail.description")}</h3>
              <p className="text-sm whitespace-pre-wrap leading-relaxed">{w.description}</p>
            </div>
          )}

          {/* Source Records */}
          <div className="card p-4">
            <h3 className="font-medium mb-3 text-sm uppercase tracking-wide text-gray-400 dark:text-gray-500">
              {t("work_detail.source_records").replace("{count}", String(wsList.length))}
            </h3>
            {wsList.length > 0 ? (
              <div className="space-y-3">
                {wsList.map((s) => (
                  <SourceRecord key={s.id} source={s} />
                ))}
              </div>
            ) : <EmptyState title={t("work_detail.no_source_records")} description={t("work_detail.no_source_records_desc", "Source metadata is created during import.")} />}
          </div>

          <WorkHistory workId={id} />
        </div>

        {/* Right column — assets gallery */}
        <div className="space-y-4">
          <AllPages workId={id} sources={wsList} />

          {/* Series info */}
          {series && (
            <div className="card p-4">
              <h3 className="font-medium mb-2 text-sm text-gray-500 dark:text-gray-400">{t("work_detail.series")}</h3>
              <p className="text-sm">{series}</p>
            </div>
          )}

          {/* Quick links */}
          {primaryWs?.source_url && (
            <div className="card p-4">
              <h3 className="font-medium mb-2 text-sm text-gray-500 dark:text-gray-400">{t("work_detail.links")}</h3>
              <a href={primaryWs.source_url} target="_blank" rel="noopener noreferrer"
                className="text-sm text-blue-600 hover:underline break-all">
                {t("work_detail.view_on").replace("{source}", primaryWs.source)}
              </a>
            </div>
          )}
        </div>
      </div>
    </main>
  );
}

function SourceRecord({ source: s }: { source: WorkSourceData }) {
  const t = useT();
  const [showRaw, setShowRaw] = useState(false);
  return (
    <div className="border rounded-lg p-3 text-sm">
      <div className="flex items-center gap-2 mb-2">
        <SourceBadge source={s.source} />
        <span className="font-mono text-xs text-gray-500 dark:text-gray-400">{s.source_work_id}</span>
        {s.source_creator_id && (
          <span className="text-xs text-gray-400 dark:text-gray-500">{t("work_detail.by")} {s.source_creator_id}</span>
        )}
      </div>
      {s.source_url && (
        <div className="text-xs mb-1">
          <a href={s.source_url} target="_blank" rel="noopener noreferrer" className="text-blue-600 hover:underline break-all">
            {s.source_url}
          </a>
        </div>
      )}
      {s.title && <p className="text-xs text-gray-600 dark:text-gray-300 mt-1">{t("work_detail.source_title")} {s.title}</p>}
      {s.posted_at && <p className="text-xs text-gray-400 dark:text-gray-500 mt-1">{t("work_detail.source_posted")} {s.posted_at}</p>}
      {s.raw_metadata && (
        <div className="mt-2">
          <button onClick={() => setShowRaw(!showRaw)}
            className="text-xs text-gray-400 dark:text-gray-500 hover:text-gray-600 dark:text-gray-300 underline">
            {showRaw ? t("work_detail.hide_metadata") : t("work_detail.show_metadata")}
          </button>
          {showRaw && (
            <pre className="mt-2 text-xs font-mono bg-gray-50 dark:bg-slate-800/50 p-3 rounded max-h-64 overflow-auto">
              {JSON.stringify(s.raw_metadata, null, 2)}
            </pre>
          )}
        </div>
      )}
    </div>
  );
}

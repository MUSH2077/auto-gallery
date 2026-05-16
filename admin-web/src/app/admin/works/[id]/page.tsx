"use client";
import { useState } from "react";
import { useParams, useRouter } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
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
  created_at: string;
}

function AllPages({ workId, sources }: { workId: string; sources: WorkSourceData[] }) {
  const assets = useQuery({ queryKey: ["works", workId, "assets"], queryFn: () => api.getWorkAssets(workId) });
  const [activeIndex, setActiveIndex] = useState(0);

  if (assets.isLoading) return <div className="bg-white dark:bg-slate-800 rounded-lg shadow p-4 animate-pulse"><div className="h-24 bg-gray-100 dark:bg-slate-700 rounded" /></div>;
  if (!assets.data || !assets.data.length) return <div className="bg-white dark:bg-slate-800 rounded-lg shadow p-4"><EmptyState title="No assets" description="No image files found for this work." /></div>;

  const current = assets.data[activeIndex] as AssetData;
  const totalPages = assets.data.length;
  const ws = sources[0];

  return (
    <div className="bg-white dark:bg-slate-800 rounded-lg shadow p-4">
      <h3 className="font-medium mb-2 text-sm">Pages ({totalPages})</h3>
      {current && (
        <div className="mb-3">
          <img src={api.mediaUrl(current.id, "preview")} alt={current.file_name}
            className="w-full rounded-lg object-contain max-h-96 bg-gray-100 dark:bg-slate-700" />
          <p className="text-xs text-gray-400 dark:text-gray-500 mt-1 text-center">{activeIndex + 1} / {totalPages}</p>
        </div>
      )}
      <div className="flex gap-2 overflow-x-auto pb-1">
        {assets.data.map((a, i) => (
          <button key={a.id} onClick={() => setActiveIndex(i)}
            className={`shrink-0 w-16 h-16 rounded border-2 overflow-hidden ${i === activeIndex ? "border-blue-500" : "border-gray-200 hover:border-gray-400"}`}>
            <img src={api.mediaUrl(a.id, "thumb")} alt={`Page ${i + 1}`} className="w-full h-full object-cover" />
          </button>
        ))}
      </div>

      {/* Asset metadata for current page */}
      {current && (
        <div className="mt-4 pt-3 border-t text-xs text-gray-500 dark:text-gray-400 space-y-1">
          <p className="font-medium text-gray-700 dark:text-gray-300 mb-1">Current Page</p>
          <div className="flex justify-between"><span>File:</span><span className="font-mono">{current.file_name}</span></div>
          {current.width && current.height && (
            <div className="flex justify-between"><span>Dimensions:</span><span>{current.width} &times; {current.height}</span></div>
          )}
          {current.mime_type && (
            <div className="flex justify-between"><span>Format:</span><span>{current.mime_type}</span></div>
          )}
        </div>
      )}
    </div>
  );
}

export default function WorkDetailPage() {
  const params = useParams();
  const router = useRouter();
  const id = params.id as string;

  const work = useQuery({ queryKey: queryKeys.works.detail(id), queryFn: () => api.getWork(id) });
  const sources = useQuery({ queryKey: queryKeys.works.sources(id), queryFn: () => api.getWorkSources(id) });
  const workTags = useQuery({ queryKey: ["works", id, "tags"], queryFn: () => api.getWorkTags(id) });

  if (work.isLoading) return <main className="max-w-6xl mx-auto p-6"><div className="animate-pulse space-y-4"><div className="h-8 bg-gray-200 rounded w-1/3" /><div className="h-64 bg-gray-200 rounded" /></div></main>;
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

  const isAiGenerated = aiType !== undefined && aiType > 0;
  const hasStats = totalView !== undefined || totalBookmarks !== undefined;

  return (
    <main className="max-w-6xl mx-auto p-6">
      {/* Header */}
      <div className="mb-6">
        <PageHeader title={w.title || "Untitled"}
          description={
            <span className="flex items-center gap-3 flex-wrap">
              {primaryWs && <SourceBadge source={primaryWs.source} />}
              {primaryWs?.source_creator_id && (
                <span className="text-sm">Creator ID: <span className="font-mono">{primaryWs.source_creator_id}</span></span>
              )}
              {rating && <span className="text-xs px-2 py-0.5 rounded bg-gray-100 dark:bg-slate-700">{rating}</span>}
              {illustType && <span className="text-xs px-2 py-0.5 rounded bg-purple-100 text-purple-700">{illustType}</span>}
              {w.is_nsfw && <span className="text-xs px-2 py-0.5 rounded bg-red-100 dark:bg-red-900/30 text-red-700 dark:text-red-400">NSFW</span>}
              {isAiGenerated && <span className="text-xs px-2 py-0.5 rounded bg-yellow-100 text-yellow-700">AI Generated</span>}
            </span>
          }
        />
      </div>

      <div className="grid grid-cols-1 md:grid-cols-3 gap-6">
        {/* Left column — metadata */}
        <div className="md:col-span-2 space-y-4">
          {/* Summary Card */}
          <div className="bg-white dark:bg-slate-800 rounded-lg shadow p-4">
            <h3 className="font-medium mb-3 text-sm uppercase tracking-wide text-gray-400 dark:text-gray-500">Summary</h3>
            <dl className="text-sm space-y-2">
              <div className="flex gap-2"><dt className="text-gray-500 dark:text-gray-400 w-24 shrink-0">Title:</dt><dd className="font-medium">{w.title || "Untitled"}</dd></div>
              {primaryWs?.source_work_id && (
                <div className="flex gap-2"><dt className="text-gray-500 dark:text-gray-400 w-24 shrink-0">Work ID:</dt><dd className="font-mono text-xs">{primaryWs.source_work_id}</dd></div>
              )}
              <div className="flex gap-2"><dt className="text-gray-500 dark:text-gray-400 w-24 shrink-0">Posted:</dt><dd>{createDate ? new Date(createDate).toLocaleDateString("en-US", { year: "numeric", month: "long", day: "numeric" }) : "Unknown"}</dd></div>
              {rawWidth && rawHeight && (
                <div className="flex gap-2"><dt className="text-gray-500 dark:text-gray-400 w-24 shrink-0">Dimensions:</dt><dd>{rawWidth} &times; {rawHeight}</dd></div>
              )}
              <div className="flex gap-2"><dt className="text-gray-500 dark:text-gray-400 w-24 shrink-0">Pages:</dt><dd>{pageCount}</dd></div>
              {illustType && (
                <div className="flex gap-2"><dt className="text-gray-500 dark:text-gray-400 w-24 shrink-0">Type:</dt><dd className="capitalize">{illustType}</dd></div>
              )}
              <div className="flex gap-2"><dt className="text-gray-500 dark:text-gray-400 w-24 shrink-0">Imported:</dt><dd className="text-xs">{new Date(w.created_at).toLocaleString()}</dd></div>
            </dl>
          </div>

          {/* Stats Card */}
          {hasStats && (
            <div className="bg-white dark:bg-slate-800 rounded-lg shadow p-4">
              <h3 className="font-medium mb-3 text-sm uppercase tracking-wide text-gray-400 dark:text-gray-500">Stats</h3>
              <div className="grid grid-cols-2 gap-4">
                {totalView !== undefined && (
                  <div className="text-center p-3 bg-blue-50 dark:bg-blue-900/30 rounded-lg">
                    <div className="text-2xl font-bold text-blue-700">{totalView.toLocaleString()}</div>
                    <div className="text-xs text-blue-500 mt-1">Views</div>
                  </div>
                )}
                {totalBookmarks !== undefined && (
                  <div className="text-center p-3 bg-pink-50 rounded-lg">
                    <div className="text-2xl font-bold text-pink-700">{totalBookmarks.toLocaleString()}</div>
                    <div className="text-xs text-pink-500 mt-1">Bookmarks</div>
                  </div>
                )}
              </div>
            </div>
          )}

          {/* Tags */}
          {(rawTags.length > 0 || (workTags.data && workTags.data.length > 0)) && (
            <div className="bg-white dark:bg-slate-800 rounded-lg shadow p-4">
              <h3 className="font-medium mb-3 text-sm uppercase tracking-wide text-gray-400 dark:text-gray-500">
                Tags ({rawTags.length} source{workTags.data?.length ? ` · ${workTags.data.length} normalized` : ""})
              </h3>
              {/* Normalized (work-level) tags */}
              {workTags.data && workTags.data.length > 0 && (
                <div className="flex flex-wrap gap-2 mb-3">
                  {workTags.data.map((t) => (
                    <span key={t.id} className="px-3 py-1 bg-blue-50 dark:bg-blue-900/30 text-blue-700 rounded-full text-sm border border-blue-200 dark:border-blue-800"
                      title={`Normalized: ${t.normalized_name}${t.category ? ` (${t.category})` : ""}`}>
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
                    <span key={i} className="px-3 py-1 bg-gray-100 dark:bg-slate-700 rounded-full text-sm hover:bg-gray-200 cursor-default"
                      title={`Source tag: ${tag}`}>
                      {tag}
                    </span>
                  ))}
                </div>
              )}
            </div>
          )}

          {/* Tools */}
          {rawTools.length > 0 && (
            <div className="bg-white dark:bg-slate-800 rounded-lg shadow p-4">
              <h3 className="font-medium mb-3 text-sm uppercase tracking-wide text-gray-400 dark:text-gray-500">Tools</h3>
              <div className="flex flex-wrap gap-2">
                {rawTools.map((tool, i) => (
                  <span key={i} className="px-3 py-1 bg-indigo-50 text-indigo-700 rounded-full text-sm">{tool}</span>
                ))}
              </div>
            </div>
          )}

          {/* Description */}
          {w.description && (
            <div className="bg-white dark:bg-slate-800 rounded-lg shadow p-4">
              <h3 className="font-medium mb-2 text-sm uppercase tracking-wide text-gray-400 dark:text-gray-500">Description</h3>
              <p className="text-sm whitespace-pre-wrap leading-relaxed">{w.description}</p>
            </div>
          )}

          {/* Source Records */}
          <div className="bg-white dark:bg-slate-800 rounded-lg shadow p-4">
            <h3 className="font-medium mb-3 text-sm uppercase tracking-wide text-gray-400 dark:text-gray-500">
              Source Records ({wsList.length})
            </h3>
            {wsList.length > 0 ? (
              <div className="space-y-3">
                {wsList.map((s) => (
                  <SourceRecord key={s.id} source={s} />
                ))}
              </div>
            ) : <EmptyState title="No source records" description="Source metadata is created during import." />}
          </div>
        </div>

        {/* Right column — assets gallery */}
        <div className="space-y-4">
          <AllPages workId={id} sources={wsList} />

          {/* Series info */}
          {series && (
            <div className="bg-white dark:bg-slate-800 rounded-lg shadow p-4">
              <h3 className="font-medium mb-2 text-sm text-gray-500 dark:text-gray-400">Series</h3>
              <p className="text-sm">{series}</p>
            </div>
          )}

          {/* Quick links */}
          {primaryWs?.source_url && (
            <div className="bg-white dark:bg-slate-800 rounded-lg shadow p-4">
              <h3 className="font-medium mb-2 text-sm text-gray-500 dark:text-gray-400">Links</h3>
              <a href={primaryWs.source_url} target="_blank" rel="noopener noreferrer"
                className="text-sm text-blue-600 hover:underline break-all">
                View on {primaryWs.source} &rarr;
              </a>
            </div>
          )}
        </div>
      </div>
    </main>
  );
}

function SourceRecord({ source: s }: { source: WorkSourceData }) {
  const [showRaw, setShowRaw] = useState(false);
  return (
    <div className="border rounded-lg p-3 text-sm">
      <div className="flex items-center gap-2 mb-2">
        <SourceBadge source={s.source} />
        <span className="font-mono text-xs text-gray-500 dark:text-gray-400">{s.source_work_id}</span>
        {s.source_creator_id && (
          <span className="text-xs text-gray-400 dark:text-gray-500">by {s.source_creator_id}</span>
        )}
      </div>
      {s.source_url && (
        <div className="text-xs mb-1">
          <a href={s.source_url} target="_blank" rel="noopener noreferrer" className="text-blue-600 hover:underline break-all">
            {s.source_url}
          </a>
        </div>
      )}
      {s.title && <p className="text-xs text-gray-600 dark:text-gray-300 mt-1">Source title: {s.title}</p>}
      {s.posted_at && <p className="text-xs text-gray-400 dark:text-gray-500 mt-1">Posted: {s.posted_at}</p>}
      {s.raw_metadata && (
        <div className="mt-2">
          <button onClick={() => setShowRaw(!showRaw)}
            className="text-xs text-gray-400 dark:text-gray-500 hover:text-gray-600 dark:text-gray-300 underline">
            {showRaw ? "Hide raw metadata" : "Show raw metadata"}
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

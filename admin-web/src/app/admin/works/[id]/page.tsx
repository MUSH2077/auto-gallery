"use client";
import Link from "next/link";
import { useEffect, useRef, useState } from "react";
import { useT } from "@/lib/i18n";
import { useParams, useRouter } from "next/navigation";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api, queryKeys } from "@/lib/api";
import { AssetFilmstrip, AssetImage, PageHeader, SourceBadge, ErrorState, EmptyState, isArchiveAsset } from "@/components";
import { Breadcrumb } from "@/components/Breadcrumb";
import { usePermissions } from "@/lib/usePermissions";
import { FullImageLightbox, ArrowIcon, DisclosurePanel, type AssetData } from "@/components/WorkViewerParts";

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

function WorkViewerShell({ workId }: { workId: string }) {
  const t = useT();
  const assets = useQuery({ queryKey: ["works", workId, "assets"], queryFn: () => api.getWorkAssets(workId) });
  const [activeIndex, setActiveIndex] = useState(0);
  const [fullAsset, setFullAsset] = useState<AssetData | null>(null);
  const wheelDelta = useRef(0);

  const totalPages = assets.data?.length || 0;
  const changePage = (delta: number) => {
    if (!totalPages) return;
    setActiveIndex((current) => (current + delta + totalPages) % totalPages);
  };

  useEffect(() => {
    if (!totalPages) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "ArrowLeft") changePage(-1);
      if (event.key === "ArrowRight") changePage(1);
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [totalPages]);

  if (assets.isLoading) return <div className="card p-4 animate-pulse"><div className="h-[60vh] rounded-md bg-subtle" /></div>;
  if (!assets.data || !assets.data.length) return <div className="card p-4"><EmptyState title={t("work_detail.no_assets_title", "No assets")} description={t("work_detail.no_assets")} /></div>;

  const current = assets.data[activeIndex] as AssetData;
  const currentIsArchive = isArchiveAsset(current);

  return (
    <section className="card overflow-hidden">
      <div
        className="relative flex min-h-[58vh] items-center justify-center bg-canvas"
        onWheel={(event) => {
          if (totalPages <= 1) return;
          wheelDelta.current += event.deltaY;
          if (Math.abs(wheelDelta.current) < 80) return;
          event.preventDefault();
          changePage(wheelDelta.current > 0 ? 1 : -1);
          wheelDelta.current = 0;
        }}
      >
        {currentIsArchive ? (
          <div className="mx-4 rounded-md border border-border bg-surface p-8 text-center">
            <div className="mx-auto mb-3 flex h-20 w-20 items-center justify-center rounded-md border border-border bg-subtle font-mono text-base font-semibold text-muted">ZIP</div>
            <div className="text-sm font-medium text-fg">{t("work_detail.archive_asset")}</div>
            <div className="mt-1 max-w-md truncate text-xs text-muted">{current.file_name}</div>
            <a href={current.original_url || ""} className="btn-ghost mt-4 inline-flex text-xs" target="_blank" rel="noopener noreferrer">
              {t("work_detail.download_original")}
            </a>
          </div>
        ) : (
          <button
            type="button"
            onClick={() => setFullAsset(current)}
            className="group flex h-full min-h-[58vh] w-full items-center justify-center p-3"
            title={t("work_detail.view_full", "View full image")}
          >
            <AssetImage
              key={current.id}
              src={current.preview_url}
              assetId={current.id}
              size="preview"
              alt={current.file_name}
              className="fade-in max-h-[72vh] max-w-full object-contain no-outline transition-transform duration-150 group-hover:scale-[1.005]"
            />
          </button>
        )}

        {totalPages > 1 && (
          <>
            <button type="button" onClick={() => changePage(-1)} className="absolute left-3 top-1/2 flex h-10 w-10 -translate-y-1/2 items-center justify-center rounded-md border border-border bg-surface/90 text-fg shadow-overlay hover:bg-subtle" aria-label={t("works.prev")}>
              <ArrowIcon direction="left" />
            </button>
            <button type="button" onClick={() => changePage(1)} className="absolute right-3 top-1/2 flex h-10 w-10 -translate-y-1/2 items-center justify-center rounded-md border border-border bg-surface/90 text-fg shadow-overlay hover:bg-subtle" aria-label={t("works.next")}>
              <ArrowIcon direction="right" />
            </button>
            <div className="absolute bottom-3 left-1/2 -translate-x-1/2 rounded-md bg-black/70 px-2 py-1 text-xs font-medium text-white">
              {activeIndex + 1} / {totalPages}
            </div>
          </>
        )}
      </div>

      <div className="border-t border-border p-3">
        <AssetFilmstrip assets={assets.data as AssetData[]} activeIndex={activeIndex} onSelect={setActiveIndex} />
        <div className="mt-3 grid gap-2 text-xs text-muted sm:grid-cols-3">
          <div className="min-w-0"><span className="font-medium text-fg">{t("work_detail.file")}</span><div className="truncate font-mono">{current.file_name}</div></div>
          {current.width && current.height && (
            <div><span className="font-medium text-fg">{t("work_detail.dimensions")}</span><div>{current.width} &times; {current.height}</div></div>
          )}
          {current.mime_type && (
            <div><span className="font-medium text-fg">{t("work_detail.format")}</span><div>{current.mime_type}</div></div>
          )}
        </div>
      </div>
      <FullImageLightbox asset={fullAsset} onClose={() => setFullAsset(null)} />
    </section>
  );
}

function WorkHistory({ workId }: { workId: string }) {
  const commits = useQuery({
    queryKey: queryKeys.curation.subject("work", workId),
    queryFn: () => api.listCurationCommits({ subject_type: "work", subject_id: workId, limit: 20 }),
  });
  if (commits.isLoading) return <div className="animate-pulse"><div className="h-20 rounded-md bg-subtle" /></div>;
  if (!commits.data?.items.length) return <EmptyState title="No history yet" description="Curation commits touching this work will appear here." />;
  return (
    <div>
      <div className="mb-3 flex items-center justify-between gap-3">
        <h3 className="text-sm font-medium uppercase tracking-wide text-muted">History</h3>
        <Link href={`/admin/curation?subject_type=work&subject_id=${workId}`} className="text-xs text-accent hover:underline">Open curation</Link>
      </div>
      <div className="space-y-3">
        {commits.data.items.map((commit) => (
          <div key={commit.id} className="border-l-2 border-accent pl-3 text-sm">
            <div className="font-medium">{commit.message}</div>
            <div className="mt-0.5 text-xs text-muted">
              <span className="font-mono">{commit.id.slice(0, 8)}</span>
              <span className="mx-1.5">·</span>
              <span>{commit.trigger}</span>
              <span className="mx-1.5">·</span>
              <span>{new Date(commit.occurred_at).toLocaleString()}</span>
            </div>
            <div className="mt-1 flex flex-wrap gap-1.5">
              {commit.changes.filter((c) => c.subject_id === workId).map((change) => (
                <span key={change.id} className="rounded-full border border-border px-2 py-0.5 text-[11px] text-muted">{change.action.replaceAll("_", " ")}</span>
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
  const { has } = usePermissions();
  const canCurate = has("curation");

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

  if (work.isLoading) return <main className="page"><div className="animate-pulse space-y-4"><div className="h-8 w-1/3 rounded-md bg-subtle" /><div className="h-64 rounded-md bg-subtle" /></div></main>;
  if (work.error) return <main className="page"><ErrorState message={(work.error as Error).message} onRetry={() => work.refetch()} /></main>;
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
    <main className="page">
      <Breadcrumb items={[
        { label: t("works.title"), href: "/admin/works" },
        ...(w.creator_name && w.creator_id ? [{ label: w.creator_name, href: `/admin/creators/${w.creator_id}` }] : []),
        { label: w.title || t("work_detail.untitled") },
      ]} />

      <PageHeader
        title={w.title || t("work_detail.untitled")}
        description={
          <span className="flex flex-wrap items-center gap-3">
            {primaryWs && <SourceBadge source={primaryWs.source} href={`/admin/works?source=${primaryWs.source}`} />}
            {w.creator_name && w.creator_id && (
              <span className="text-sm">
                {t("work_detail.by")}{" "}
                <Link href={`/admin/creators/${w.creator_id}`} className="font-medium text-accent hover:underline">
                  {w.creator_name}
                </Link>
              </span>
            )}
            {!w.creator_name && primaryWs?.source_creator_id && (
              <span className="text-sm">{t("work_detail.creator_id")} <span className="font-mono">{primaryWs.source_creator_id}</span></span>
            )}
            {rating && <span className="badge border-border bg-subtle text-muted">{rating}</span>}
            {illustType && <span className="badge border-accent/30 bg-accent-subtle text-accent">{illustType}</span>}
            {w.is_nsfw && <span className="badge border-danger/30 bg-danger-subtle text-danger">{t("work_detail.nsfw")}</span>}
            {isAiGenerated && <span className="badge border-warning/30 bg-warning-subtle text-warning">{t("work_detail.ai_generated")}</span>}
            {visibility !== "visible" && <span className="badge border-danger/30 bg-danger-subtle text-danger">{visibility}</span>}
          </span>
        }
      />

      <div className="grid grid-cols-1 gap-5 lg:grid-cols-[minmax(0,1fr)_320px]">
        <div className="min-w-0 space-y-4">
          <WorkViewerShell workId={id} />

          {(rawTags.length > 0 || (workTags.data && workTags.data.length > 0)) && (
            <DisclosurePanel storageKey={`work-detail:${id}:tags`} title={t("work_detail.tags")} count={(workTags.data?.length || 0) + rawTags.length} defaultOpen>
              {workTags.data && workTags.data.length > 0 && (
                <div className="mb-3 flex flex-wrap gap-2">
                  {workTags.data.map((tag) => (
                    <button key={tag.id} onClick={() => router.push(`/admin/tags/${tag.id}`)}
                      className="badge border-accent/30 bg-accent-subtle text-accent hover:border-accent"
                      title={`Search: ${tag.normalized_name}${tag.category ? ` (${tag.category})` : ""}`}>
                      {tag.normalized_name}
                      {tag.category && <span className="text-xs text-muted">({tag.category})</span>}
                    </button>
                  ))}
                </div>
              )}
              {rawTags.length > 0 && (
                <div className="flex flex-wrap gap-2">
                  {rawTags.map((tag, i) => (
                    <button key={`${tag}-${i}`} onClick={() => router.push(`/admin/search?q=${encodeURIComponent(tag)}`)}
                      className="badge border-border bg-subtle text-muted hover:border-accent hover:text-accent"
                      title={`Search source tag: ${tag}`}>
                      {tag}
                    </button>
                  ))}
                </div>
              )}
            </DisclosurePanel>
          )}

          {w.description && (
            <DisclosurePanel storageKey={`work-detail:${id}:description`} title={t("work_detail.description")} defaultOpen>
              <p className="whitespace-pre-wrap text-sm leading-relaxed">{w.description}</p>
            </DisclosurePanel>
          )}

          {rawTools.length > 0 && (
            <DisclosurePanel storageKey={`work-detail:${id}:tools`} title={t("work_detail.tools")} count={rawTools.length}>
              <div className="flex flex-wrap gap-2">
                {rawTools.map((tool, i) => (
                  <span key={`${tool}-${i}`} className="badge border-accent/30 bg-accent-subtle text-accent">{tool}</span>
                ))}
              </div>
            </DisclosurePanel>
          )}

          <DisclosurePanel storageKey={`work-detail:${id}:sources`} title={t("work_detail.source_records").replace("{count}", String(wsList.length))} count={wsList.length}>
            {wsList.length > 0 ? (
              <div className="space-y-3">
                {wsList.map((s) => <SourceRecord key={s.id} source={s} />)}
              </div>
            ) : <EmptyState title={t("work_detail.no_source_records")} description={t("work_detail.no_source_records_desc", "Source metadata is created during import.")} />}
          </DisclosurePanel>

          <DisclosurePanel storageKey={`work-detail:${id}:history`} title="History">
            <WorkHistory workId={id} />
          </DisclosurePanel>
        </div>

        <aside className="space-y-4 lg:sticky lg:top-6 lg:self-start">
          <div className="card p-4">
            <div className="mb-3 flex items-start justify-between gap-3">
              <div className="min-w-0">
                <h2 className="truncate text-base font-semibold">{w.title || t("work_detail.untitled")}</h2>
                {w.creator_name && w.creator_id && (
                  <Link href={`/admin/creators/${w.creator_id}`} className="mt-1 block truncate text-sm text-accent hover:underline">
                    {w.creator_name}
                  </Link>
                )}
              </div>
              {canCurate && (
                <button onClick={() => toggleFavorite.mutate(id)}
                  className={`shrink-0 text-2xl ${work.data?.is_favorite ? "text-warning" : "text-muted hover:text-warning"}`}
                  title={work.data?.is_favorite ? t("common.unfavorite") : t("common.favorite")}>
                  {work.data?.is_favorite ? "★" : "☆"}
                </button>
              )}
            </div>
            <dl className="space-y-2 text-sm">
              {primaryWs?.source_work_id && <div className="flex justify-between gap-3"><dt className="text-muted">{t("work_detail.work_id")}</dt><dd className="truncate font-mono text-xs">{primaryWs.source_work_id}</dd></div>}
              <div className="flex justify-between gap-3"><dt className="text-muted">{t("work_detail.posted")}</dt><dd>{createDate ? new Date(createDate).toLocaleDateString() : t("work_detail.unknown")}</dd></div>
              {rawWidth && rawHeight && <div className="flex justify-between gap-3"><dt className="text-muted">{t("work_detail.dimensions")}</dt><dd>{rawWidth} &times; {rawHeight}</dd></div>}
              <div className="flex justify-between gap-3"><dt className="text-muted">{t("work_detail.pages")}</dt><dd>{pageCount}</dd></div>
              {illustType && <div className="flex justify-between gap-3"><dt className="text-muted">{t("work_detail.type")}</dt><dd className="capitalize">{illustType}</dd></div>}
              <div className="flex justify-between gap-3"><dt className="text-muted">{t("work_detail.imported")}</dt><dd className="text-right text-xs">{new Date(w.created_at).toLocaleString()}</dd></div>
            </dl>
            <div className="mt-4 flex flex-wrap gap-2">
              {canCurate && visibility === "visible" ? (
                <button onClick={() => curateWork.mutate("trash")} disabled={curateWork.isPending} className="btn-danger text-xs">
                  Move to Trash
                </button>
              ) : canCurate && visibility === "trashed" ? (
                <button onClick={() => curateWork.mutate("restore")} disabled={curateWork.isPending} className="btn-ghost text-xs">
                  Restore
                </button>
              ) : null}
              {primaryWs?.source_url && (
                <a href={primaryWs.source_url} target="_blank" rel="noopener noreferrer" className="btn-ghost text-xs">
                  {t("work_detail.view_on").replace("{source}", primaryWs.source)}
                </a>
              )}
            </div>
          </div>

          {hasStats && (
            <div className="card grid grid-cols-2 gap-2 p-3">
              {totalView !== undefined && (
                <div className="rounded-md bg-accent-subtle p-3 text-center text-accent">
                  <div className="tabular text-xl font-semibold">{totalView.toLocaleString()}</div>
                  <div className="text-xs">{t("work_detail.views")}</div>
                </div>
              )}
              {totalBookmarks !== undefined && (
                <div className="rounded-md bg-warning-subtle p-3 text-center text-warning">
                  <div className="tabular text-xl font-semibold">{totalBookmarks.toLocaleString()}</div>
                  <div className="text-xs">{t("work_detail.bookmarks")}</div>
                </div>
              )}
            </div>
          )}

          {w.creator_id && <MoreFromCreator creatorId={w.creator_id} currentWorkId={id} />}

          {series && (
            <div className="card p-4">
              <h3 className="mb-2 text-sm font-medium text-muted">{t("work_detail.series")}</h3>
              <p className="text-sm">{series}</p>
            </div>
          )}
        </aside>
      </div>
    </main>
  );
}

function MoreFromCreator({ creatorId, currentWorkId }: { creatorId: string; currentWorkId: string }) {
  const t = useT();
  const works = useQuery({
    queryKey: ["more-from-creator", creatorId],
    queryFn: () => api.listWorks(0, 6, { creator_id: creatorId, sort_by: "posted_at", sort_order: "desc" }),
  });
  if (works.isLoading || !works.data?.items?.length) return null;
  const others = works.data.items.filter(w => w.id !== currentWorkId).slice(0, 5);
  if (!others.length) return null;
  return (
    <div className="card p-4">
      <h3 className="font-medium mb-3 text-sm">{t("work_detail.more_from_creator")}</h3>
      <div className="grid grid-cols-2 gap-2">
        {others.map(w => (
          <Link key={w.id} href={`/admin/works/${w.id}`} className="group">
            <div className="aspect-[4/3] overflow-hidden rounded-md bg-subtle">
              <AssetImage assetId={w.thumbnail_asset_id} alt={w.title || ""} className="h-full w-full object-cover" fallback={t("works.na")} />
            </div>
            <p className="text-xs mt-1 truncate group-hover:text-accent">{w.title || t("works.untitled")}</p>
          </Link>
        ))}
      </div>
      <Link href={`/admin/works?creator=${creatorId}`} className="text-xs text-accent hover:underline mt-2 inline-block">
        {t("work_detail.view_all_from_creator")}
      </Link>
    </div>
  );
}

function SourceRecord({ source: s }: { source: WorkSourceData }) {
  const t = useT();
  const [showRaw, setShowRaw] = useState(false);
  return (
    <div className="rounded-md border border-border bg-subtle p-3 text-sm">
      <div className="flex items-center gap-2 mb-2">
        <SourceBadge source={s.source} />
        <span className="font-mono text-xs text-muted">{s.source_work_id}</span>
        {s.source_creator_id && (
          <span className="text-xs text-muted">{t("work_detail.by")} {s.source_creator_id}</span>
        )}
      </div>
      {s.source_url && (
        <div className="text-xs mb-1">
          <a href={s.source_url} target="_blank" rel="noopener noreferrer" className="break-all text-accent hover:underline">
            {s.source_url}
          </a>
        </div>
      )}
      {s.title && <p className="text-xs text-fg mt-1">{t("work_detail.source_title")} {s.title}</p>}
      {s.posted_at && <p className="text-xs text-muted mt-1">{t("work_detail.source_posted")} {s.posted_at}</p>}
      {s.source === "manual" && typeof s.raw_metadata?.uploaded_by === "string" && s.raw_metadata.uploaded_by && (
        <p className="text-xs text-muted mt-1">{t("work_detail.uploaded_by")} {s.raw_metadata.uploaded_by}</p>
      )}
      {s.raw_metadata && (
        <div className="mt-2">
          <button onClick={() => setShowRaw(!showRaw)}
            className="text-xs text-muted hover:text-fg underline">
            {showRaw ? t("work_detail.hide_metadata") : t("work_detail.show_metadata")}
          </button>
          {showRaw && (
            <pre className="mt-2 max-h-64 overflow-auto rounded-md border border-border bg-surface p-3 font-mono text-xs">
              {JSON.stringify(s.raw_metadata, null, 2)}
            </pre>
          )}
        </div>
      )}
    </div>
  );
}

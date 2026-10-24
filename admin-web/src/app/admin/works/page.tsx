"use client";
import { useWorksLibrary } from "@/features/works/useWorksLibrary";
import { useState, useEffect, useMemo, useRef, Suspense } from "react";
import dynamic from "next/dynamic";
import { useRouter, useSearchParams, usePathname } from "next/navigation";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useT } from "@/lib/i18n";
import { api, queryKeys, WorkListItem, type SearchQualifierToken, type SearchResponse } from "@/lib/api";
import type { MediaDerivativeProgress, WorkAsset } from "@/lib/api/endpoints/works";
import { useAppearanceSettings } from "@/lib/appearance";
import { useStaggeredEntrance } from "@/lib/motion";
import PageHeader from "@/components/PageHeader";
import EmptyState from "@/components/EmptyState";
import ErrorState from "@/components/ErrorState";
import PageShell from "@/components/PageShell";
import SelectionBar from "@/components/SelectionBar";
import { SmartSearchInput, type SearchComposeRequest, useSearchBatchComposer, useSearchComposer, } from "@/components/SmartSearchInput";
import PermissionGuard from "@/components/PermissionGuard";
import type { SlideItem } from "@/components/SlideshowPlayer";
import { useSlideshow } from "@/lib/useSlideshow";
import { usePermissions } from "@/lib/usePermissions";
import { useI18nFormat } from "@/lib/i18n-format";
import { resolveMediaKind } from "@/lib/media";
import DomainDangerZone from "@/components/DomainDangerZone";
import { useToast } from "@/components/Toast";
import { pollInterval } from "@/lib/polling";
import { WorksControlSurface, WorksDisplayPanel } from "./WorksControlSurface";
import { WorksFilterPanel } from "./WorksFilterPanel";
import { WorksSortPanel } from "./WorksSortPanel";
import { WorkCard as SharedWorkCard } from "./WorkCard";
import { WorksLayout, WorksLayoutSkeleton } from "./WorksLayout";
import type { WorksFilterValue, WorksMediaFilter, WorksSortValue, WorkCardPreview } from "@/features/works/types";
const WorkPreviewOverlay = dynamic(() => import("@/components/work-interactions").then((module) => module.WorkPreviewOverlay), { ssr: false });
type PreviewState = WorkCardPreview;
const LEGACY_WORK_QUERY_KEYS = [
    "source",
    "creator",
    "creator_id",
    "tag",
    "nsfw",
    "fav",
    "favorite",
    "ai",
    "sort",
    "order",
    "filter",
    "search",
];
function stripLegacyWorkQuery(params: URLSearchParams): boolean {
    let changed = false;
    for (const key of LEGACY_WORK_QUERY_KEYS) {
        if (params.has(key)) {
            params.delete(key);
            changed = true;
        }
    }
    return changed;
}
function DerivativeProgressCard({ progress, refreshing, onRefresh, }: {
    progress?: MediaDerivativeProgress;
    refreshing: boolean;
    onRefresh: () => void;
}) {
    const t = useT();
    const fmt = useI18nFormat();
    if (!progress || progress.total === 0 || progress.remaining === 0)
        return null;
    const statusKey = progress.status === "stalled"
        ? "works.derivative_progress_stalled"
        : progress.status === "failed"
            ? "works.derivative_progress_failed"
            : progress.status === "waiting"
                ? "works.derivative_progress_waiting"
                : "works.derivative_progress_running";
    const percent = Math.min(100, Math.max(0, progress.completion_percent));
    const stalled = progress.status === "stalled";
    return (<section className={`mb-4 rounded-lg border p-4 ${stalled ? "border-warning/40 bg-warning-subtle" : "border-border bg-surface"}`} role="region" aria-label={t("works.derivative_progress_region")}>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="text-sm font-semibold text-fg">{t("works.derivative_progress_title")}</p>
          <p className={`mt-0.5 text-xs font-medium ${stalled ? "text-warning" : "text-accent"}`} role="status">
            {t(statusKey)}
          </p>
        </div>
        <button type="button" className="btn-ghost shrink-0" onClick={onRefresh} disabled={refreshing} aria-label={t("works.derivative_progress_refresh")}>
          {refreshing ? t("common.refreshing") : t("works.derivative_progress_refresh")}
        </button>
      </div>
      <div className="mt-3 h-2 overflow-hidden rounded-full bg-border" role="progressbar" aria-label={t("works.derivative_progress_title")} aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(percent)}>
        <div className={`h-full w-full rounded-full transition-transform duration-slow ease-out ${stalled ? "bg-warning" : "bg-accent"}`} style={{ transform: `scaleX(${percent / 100})`, transformOrigin: "left" }}/>
      </div>
      <div className="mt-2 flex flex-wrap items-center justify-between gap-x-4 gap-y-1 text-xs text-muted">
        <span className="font-mono tabular-nums text-fg">
          {t("works.derivative_progress_count", {
            completed: fmt.number(progress.completed),
            total: fmt.number(progress.total),
        })}
        </span>
        <span>
          {t("works.derivative_progress_remaining", {
            remaining: fmt.number(progress.remaining),
            works: fmt.number(progress.affected_works),
        })}
        </span>
        {progress.last_completed_at ? (<span>{t("works.derivative_progress_last", { time: fmt.relative(progress.last_completed_at) })}</span>) : null}
      </div>
    </section>);
}
function WorksContent() {
    const { t, router, qc, sp, pathname, isAdmin, has, canCurate, toast, appearance, updateSettings, selectedWorkIds, setSelectedWorkIds, preview, setPreview, closePreviewTimer, previewEnabled, wheelThreshold, search, requestedPage, page, requestedSeed, seed, legacyView, viewMode, limit, navigationParamsRef, inputVal, setInputVal, updateParams, setSearchQuery, clearSearch, scheduleClosePreview, cancelClosePreview, worksQueryKey, worksQuery, works, derivativeProgress, qualifierTokens, qualifierValues, sourceFilter, creatorFilter, isValues, nsfwFilter, isFavoriteFilter, aiFilter, curationVisibility, explicitSortValue, activeFilterCount, composer, batchComposer, sourceFilters, mediaFilters, filterValue, parsedHasText, knownSorts, defaultSort, currentSort, sortSummary, displaySummary, applyFilterDraft, selectSort, updateDisplayAppearance, filters, slideshow, slideItems, workItems, workEntrance, previewAssets, previewAssetIds, restoreWork, purgeWork, batchTrash, pageWorkIds, selectedCount, pageAllSelected, toggleSelectWork, toggleSelectPage, moveSelectedToTrash } = useWorksLibrary();
    return (<PageShell>
      <PageHeader title={t("works.title")} description={t("works.count").replace("{count}", String(works.data?.total ?? 0))} secondaryActions={slideItems.length > 0 ? (<button type="button" onClick={() => slideshow.open(slideItems)} className="btn-ghost">
            {t("slideshow.open")}
          </button>) : undefined}/>

      <DerivativeProgressCard progress={derivativeProgress.data} refreshing={derivativeProgress.isFetching} onRefresh={() => { void derivativeProgress.refetch(); }}/>

      {worksQuery.isFetching && !worksQuery.isLoading && (<div className="mb-2 h-0.5 w-full overflow-hidden rounded bg-subtle" role="status" aria-label={t("common.loading")}>
          <div className="h-full w-1/3 animate-pulse rounded bg-accent"/>
        </div>)}

      <WorksControlSurface filterCount={activeFilterCount} sortSummary={sortSummary} displaySummary={displaySummary} search={(<SmartSearchInput value={inputVal} onChange={setInputVal} onEditStart={composer.discardPendingResult} onClear={clearSearch} scope="works" ariaLabel={t("works.search_title")} placeholder={t("works.search_title")} showTokens={false} className="w-full"/>)} renderFilter={(close) => (<WorksFilterPanel value={filterValue} applying={batchComposer.isPending} onApply={(next) => applyFilterDraft(next, close)} onCancel={close}/>)} renderSort={() => (<WorksSortPanel value={currentSort} hasText={parsedHasText} onChange={selectSort} onReshuffle={() => updateParams({ seed: String(crypto.getRandomValues(new Uint32Array(1))[0]) })}/>)} renderDisplay={() => (<WorksDisplayPanel appearance={{ ...appearance, worksViewMode: viewMode }} updateAppearance={updateDisplayAppearance}/>)}/>

      {canCurate && appearance.workCardShowCheckbox && works.data && works.data.items?.length > 0 && curationVisibility === "visible" && (selectedCount > 0 ? (<SelectionBar className="" count={selectedCount} label={t("works.selected_count", { count: selectedCount })} clearLabel={t("works.clear_selection")} onClear={() => setSelectedWorkIds(new Set())}>
            <button onClick={toggleSelectPage} className="btn-ghost text-xs">
              {pageAllSelected ? t("works.deselect_page") : t("works.select_page")}
            </button>
            <button onClick={moveSelectedToTrash} disabled={batchTrash.isPending} className="btn-danger text-xs disabled:opacity-50">
              {batchTrash.isPending ? t("works.moving_to_trash") : t("works.move_to_trash")}
            </button>
          </SelectionBar>) : (<div className="mb-4 flex flex-wrap items-center gap-2 rounded-md border border-border bg-surface px-3 py-2" aria-live="polite">
            <button onClick={toggleSelectPage} className="btn-ghost text-xs">
              {pageAllSelected ? t("works.deselect_page") : t("works.select_page")}
            </button>
          </div>))}

      {works.isLoading ? <WorksLayoutSkeleton mode={viewMode} size={appearance.workCardSize}/> : null}

      {/* Error */}
      {works.error && <ErrorState message={(works.error as Error).message} onRetry={() => works.refetch()}/>}

      {/* Empty */}
      {works.data && !works.data.items?.length && (<EmptyState title={t("works.no_works")} description={search || sourceFilter || creatorFilter ? t("works.no_works_filter") : t("works.no_works_desc")}/>)}

      {works.data && works.data.items?.length > 0 ? (<WorksLayout mode={viewMode} size={appearance.workCardSize}>
          {works.data.items.map((work: WorkListItem, index: number) => (<SharedWorkCard key={work.id} entrance={workEntrance(work.id, index)} work={work} presentation={{
                    layout: viewMode,
                    size: appearance.workCardSize,
                    showCheckbox: appearance.workCardShowCheckbox,
                    showAi: appearance.workCardShowAi,
                    showNsfw: appearance.workCardShowNsfw,
                    showFavorite: appearance.workCardShowFavorite,
                    blurNsfw: appearance.blurNsfw,
                }} previewEnabled={previewEnabled} previewDelayMs={appearance.workPreviewDelayMs} wheelThreshold={wheelThreshold} onOpenPreview={setPreview} onScheduleClosePreview={scheduleClosePreview} onCancelClosePreview={cancelClosePreview} onPreviewPage={(workId, pageIndex) => setPreview((current) => current?.work.id === workId ? { ...current, pageIndex } : current)} trashMode={curationVisibility === "trashed"} selectable={canCurate && curationVisibility === "visible"} selected={selectedWorkIds.has(work.id)} onToggleSelect={toggleSelectWork} onRestore={(id) => restoreWork.mutate(id)} onPurge={(id) => {
                    if (window.confirm(t("works.purge_confirm")))
                        purgeWork.mutate(id);
                }} canCurate={canCurate} canPurge={isAdmin} eager={index < 8}/>))}
        </WorksLayout>) : null}

      {preview && previewEnabled && (<WorkPreviewOverlay anchor={preview.anchor} title={preview.work.title} creatorName={preview.work.creator_name} source={preview.work.source} assetIds={previewAssetIds} assets={previewAssets.data} isLoading={previewAssets.isLoading || (previewAssets.isFetching && !previewAssets.data)} isError={previewAssets.isError} previewSize={appearance.workPreviewSize} pageIndex={preview.pageIndex} assetCount={preview.work.asset_count} blurred={preview.work.is_nsfw && appearance.blurNsfw} onMouseEnter={cancelClosePreview} onMouseLeave={scheduleClosePreview} onWheelPage={(delta) => {
                if (previewAssetIds.length <= 1)
                    return;
                setPreview((current) => {
                    if (!current)
                        return current;
                    const next = (current.pageIndex + delta + previewAssetIds.length) % previewAssetIds.length;
                    return { ...current, pageIndex: next };
                });
            }} onRefreshAssets={() => previewAssets.refetch()}/>)}

      {/* Pagination */}
      {(works.data?.total ?? 0) > 0 && (<div className="flex gap-2 justify-center">
          <button disabled={page === 0} onClick={() => updateParams({ p: page <= 1 ? null : String(page - 1) }, false, "push")} className="px-3 py-1 text-sm border rounded disabled:opacity-30 dark:border-border dark:text-muted">{t("works.prev")}</button>
          <span className="px-3 py-1 text-sm text-muted">{t("works.page").replace("{page}", String(page + 1))}</span>
          <button onClick={() => updateParams({ p: String(page + 1) }, false, "push")} disabled={!works.data || (page + 1) * limit >= works.data.total} className="px-3 py-1 text-sm border rounded disabled:opacity-30 dark:border-border dark:text-muted">{t("works.next")}</button>
        </div>)}
      {slideshow.node}
      <DomainDangerZone entity="works" title={t("datamgmt.danger_clear_works")} description={t("datamgmt.danger_clear_works_desc")}/>
    </PageShell>);
}
export default function WorksPage() {
    return (<PermissionGuard module="library">
      <Suspense>
        <WorksContent />
      </Suspense>
    </PermissionGuard>);
}

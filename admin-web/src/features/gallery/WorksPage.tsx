"use client";

import { useEffect, useRef, useState } from "react";
import { WorkMediaThumbnail } from "@/components/MediaAssetRenderer";
import { api, queryKeys, type WorkListItem } from "@/lib/api";
import type { AppearanceSettings } from "@/lib/appearance";
import { useT } from "@/lib/i18n";
import ErrorState from "@/components/ErrorState";
import WorkDetailDialog from "./WorkDetailDialog";
import { refreshWorkVisibility } from "@/features/works/refreshWorkVisibility";
import { useWorksLibrary } from "@/features/works/useWorksLibrary";
import { useMutationState } from "@tanstack/react-query";
import { WORK_FAVORITE_MUTATION_KEY, useWorkFavorite } from "@/features/works/useWorkFavorite";
import type { WorksSortValue, WorksFilterValue } from "@/features/works/types";
import type { MediaDerivativeProgress } from "@/lib/api/endpoints/works";
import { useGalleryFormat } from "@/features/gallery-ui/format";
import { hasWorkDetailListOrigin, markWorkDetailListEntry } from "./detailHistory";
import { WorksView, WorkCollection, type ScopeLink } from "@/features/gallery-ui/CollectionViews";
import { ToolbarSelect, DisplayOptions, SearchField } from "@/features/gallery-ui/Controls";
import { WorkItem } from "@/features/gallery-ui/WorkItem";
import { Modal } from "@/features/gallery-ui/Modal";
import { useGalleryCopy } from "@/features/gallery-ui/copy";
import { useToast } from "@/components/Toast";
import ClearDomain from "@/features/gallery-ui/ClearDomain";

export default function WorksPage() {
  const c = useWorksLibrary({ pageSize: 8 });
  const t = c.t;
  const { copy } = useGalleryCopy();
  const toast = useToast();
  const [moving, setMoving] = useState(new Set<string>());
  const [showSelection, setShowSelection] = useState(false);
  const [filterOpen, setFilterOpen] = useState(false);
  const [appearanceOpen, setAppearanceOpen] = useState(false);
  const [filterDraft, setFilterDraft] = useState<WorksFilterValue | null>(null);
  const viewMode = c.viewMode;
  const scope = c.curationVisibility === "trashed" ? "trash" : c.isFavoriteFilter ? "favorite" : "all";
  const detail = c.sp.get("work");
  const [confirm, setConfirm] = useState<{ kind: "batch" | "purge"; ids: string[] } | null>(null);
  const [confirmError, setConfirmError] = useState<string | null>(null);
  const confirmToken = useRef(0);
  const curatingRef = useRef(new Set<string>());
  const restoringRef = useRef(new Set<string>());
  const [curating, setCurating] = useState(new Set<string>());
  const [restoring, setRestoring] = useState(new Set<string>());
  const [operationErrors, setOperationErrors] = useState<Record<string, string>>({});
  const favoriteIds = useMutationState({
    filters: { mutationKey: WORK_FAVORITE_MUTATION_KEY, status: "pending" },
    select: mutation => String(mutation.options.mutationKey?.[2] || ""),
  });
  const detailCurations = useMutationState({
    filters: { mutationKey: ["works", "curation"] },
    select: mutation => ({
      id: String(mutation.options.mutationKey?.[2] || ""),
      status: mutation.state.status,
      error: mutation.state.error,
      action: mutation.state.variables as "trash" | "restore" | undefined,
    }),
  });
  const latestCurations = new Map(detailCurations.map(state => [state.id, state]));
  const detailCurationIds = [...latestCurations.values()].filter(state => state.status === "pending").map(state => state.id);
  const selectedBusy = [...c.selectedWorkIds].some(id =>
    favoriteIds.includes(id) || detailCurationIds.includes(id) || curating.has(id) || restoring.has(id)
  );

  const closeConfirm = () => {
    confirmToken.current += 1;
    setConfirm(null);
    setConfirmError(null);
  };

  const restore = async (id: string) => {
    if (restoringRef.current.has(id) || curatingRef.current.has(id) || c.qc.isMutating({ mutationKey: ["works", "curation", id] }) > 0) return;
    restoringRef.current.add(id);
    setRestoring(new Set(restoringRef.current));
    setOperationErrors(previous => {
      const next = { ...previous };
      delete next[id];
      return next;
    });
    try {
      await api.batchCurateWorks([id], "restore", "restored from trash");
      await refreshWorkVisibility(c.qc, [id]);
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      setOperationErrors(previous => ({ ...previous, [id]: message }));
      toast.error(message);
    } finally {
      restoringRef.current.delete(id);
      setRestoring(new Set(restoringRef.current));
    }
  };

  const confirmPending = Boolean(confirm?.ids.some(id => curating.has(id)));
  const requestBatchTrash = () => {
    if (!c.queryTransitioning && !selectedBusy && !confirmPending && c.selectedWorkIds.size) {
      confirmToken.current += 1;
      setConfirmError(null);
      setConfirm({ kind: "batch", ids: [...c.selectedWorkIds] });
    }
  };
  const requestPurge = (id: string) => {
    if (!curatingRef.current.has(id) && !restoringRef.current.has(id) && !detailCurationIds.includes(id)) {
      confirmToken.current += 1;
      setConfirmError(null);
      setConfirm({ kind: "purge", ids: [id] });
    }
  };
  const runConfirm = async () => {
    if (!confirm || confirmPending || confirm.ids.some(id =>
      curatingRef.current.has(id) ||
      restoringRef.current.has(id) ||
      c.qc.isMutating({ mutationKey: ["works", "curation", id] }) > 0 ||
      c.qc.isMutating({ mutationKey: [...WORK_FAVORITE_MUTATION_KEY, id] }) > 0
    )) return;
    const current = confirm;
    const token = confirmToken.current;
    current.ids.forEach(id => curatingRef.current.add(id));
    setCurating(new Set(curatingRef.current));
    setOperationErrors(previous => Object.fromEntries(Object.entries(previous).filter(([id]) => !current.ids.includes(id))));
    if (current.kind === "batch") { setMoving(previous => new Set([...previous, ...current.ids])); setConfirm(null); }
    try {
      if (current.kind === "batch") await c.batchTrash.mutateAsync(current.ids);
      else await c.purgeWork.mutateAsync(current.ids[0]);
      if (token === confirmToken.current) setConfirm(null);
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      setOperationErrors(previous => ({ ...previous, ...Object.fromEntries(current.ids.map(id => [id, message])) }));
      if (current.kind === "batch") toast.error(message);
      else if (token === confirmToken.current) setConfirmError(message);
    } finally {
      if (current.kind === "batch") setMoving(previous => new Set([...previous].filter(id => !current.ids.includes(id))));
      current.ids.forEach(id => curatingRef.current.delete(id));
      setCurating(new Set(curatingRef.current));
    }
  };

  function href(patch: Record<string, string | null>) {
    const params = new URLSearchParams(c.sp.toString());
    for (const [key, value] of Object.entries(patch)) {
      if (value === null) params.delete(key);
      else params.set(key, value);
    }
    return `${c.pathname}?${params}`;
  }
  const detailOrigin = useRef<"list" | "direct" | null>(detail ? "direct" : null);
  const open = (id: string) => {
    detailOrigin.current = "list";
    c.updateParams({ work: id }, false, "push");
  };
  const close = () => {
    const fromList = detailOrigin.current === "list" || hasWorkDetailListOrigin();
    if (fromList) {
      // Keep the marker on the detail history entry. The back navigation
      // consumes the list entry; clearing it here breaks browser forward.
      detailOrigin.current = null;
      c.router.back();
    } else {
      c.updateParams({ work: null }, false);
    }
  };
  useEffect(() => {
    if (!detail) detailOrigin.current = null;
    else if (detailOrigin.current === "list") markWorkDetailListEntry();
  }, [detail]);

  function scopeQ(next: string) {
    const rest = c.search.replace(/(^|\s)is:(favorite|trashed|visible)(?=\s|$)/g, " ").trim();
    return [rest, next === "favorite" ? "is:favorite" : next === "trash" ? "is:trashed" : ""].filter(Boolean).join(" ");
  }

  const total = c.works.data?.total ?? 0;
  const pages = Math.max(1, Math.ceil(total / 8));
  const scopes: ScopeLink[] = [
    { value: "all", label: t("gallery.allWorks") },
    { value: "favorite", label: t("gallery.favorites") },
    { value: "trash", label: t("works.trash") },
  ].map(item => ({
    ...item,
    href: href({ q: scopeQ(item.value), p: null, work: null }),
    active: scope === item.value,
    count: scope === item.value && !c.works.isPlaceholderData ? String(total) : undefined,
    countSuffix: undefined,
    onClick: event => {
      if (event.button || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
      event.preventDefault();
      c.batchComposer.mutate([
        { key: "is", value: item.value === "trash" ? "trashed" : null, operation: "replace-group", replace_values: ["visible", "trashed"] },
        { key: "is", value: item.value === "favorite" ? "favorite" : null, operation: "replace-group", replace_values: ["favorite"] },
      ]);
    },
  }));

  const visibleWorkItems = c.workItems.filter(work => {
    const state = latestCurations.get(work.id);
    return !moving.has(work.id) && !restoring.has(work.id) && !(state?.status === "pending" && (scope === "trash" ? state.action === "restore" : state.action === "trash"));
  });
  const available = visibleWorkItems.length > 0 && !c.works.isLoading && !c.works.isError;
  const selectable = c.canCurate && scope !== "trash" && c.appearance.workCardShowCheckbox;
  const selectionVisible = selectable && showSelection;
  const openFilters = () => { setFilterDraft(c.filterValue); setFilterOpen(true); };
  const applyFilters = () => { if (filterDraft) c.applyFilterDraft(filterDraft, () => setFilterOpen(false)); };
  const toolbar = <>
    <SearchField value={c.inputVal} onChange={c.setInputVal} onComposingChange={c.setIsComposing} placeholder={copy("workSearch")} />
    <div className="layout-toolbar-actions">
      <div className="layout-toolbar-filters">
        <ToolbarSelect
          label={t("gallery.source")}
          value={c.filterValue.sources.length>1 ? "__multiple__" : c.sourceFilter}
          onValueChange={source => { if(source!=="__multiple__") c.composer.mutate({ key: "source", value: source || null, operation: "set" }); }}
          onMore={openFilters}
          moreLabel={t("works.filter_button")}
          options={[
            { value: "", label: t("gallery.allSources") },
            { value: "pixiv", label: "Pixiv" },
            { value: "manual", label: t("gallery.localImport") },
            ...c.filterValue.sources.length>1 ? [{value:"__multiple__",label:t("gallery.sourcesCount",{count:c.filterValue.sources.length})}] : [],
            ...["x","iwara","danbooru","pinterest","lofter","weibo","bilibili"].map(value=>({value,label:value})),
            ...(c.sourceFilter && !["pixiv", "manual", "x", "iwara", "danbooru", "pinterest", "lofter", "weibo", "bilibili"].includes(c.sourceFilter) ? [{ value: c.sourceFilter, label: c.sourceFilter }] : []),
          ]}
        />
        <ToolbarSelect
          label={t("gallery.sort")}
          value={c.currentSort}
          onValueChange={sort => c.selectSort(sort as WorksSortValue)}
          onMore={c.currentSort === "random" ? () => c.updateParams({ seed: String(crypto.getRandomValues(new Uint32Array(1))[0]) }) : undefined}
          moreLabel={c.currentSort === "random" ? t("works.reshuffle") : undefined}
          options={[
            { value: "created-desc", label: t("gallery.sortNewest") },
            { value: "heat-desc", label: t("gallery.sortHeat") },
            { value: "title-asc", label: t("gallery.sortTitle") },
            { value: "random", label: t("gallery.sortRandom") },
            ...[{value:"created-asc",label:t("works.sort_imported_asc")},{value:"posted-desc",label:t("works.sort_posted_desc")},{value:"posted-asc",label:t("works.sort_posted_asc")},{value:"updated-desc",label:t("works.sort_updated_desc")},{value:"updated-asc",label:t("works.sort_updated_asc")},{value:"title-desc",label:t("works.sort_title_desc")}],
            ...(c.currentSort === "relevance" ? [{ value: "relevance", label: t("works.sort_relevance") }] : []),
          ]}
        />
      </div>
      <div className="layout-toolbar-utilities">
        <DisplayOptions
          view={viewMode}
          onViewChange={view => { c.updateDisplayAppearance({ worksViewMode: view }); }}
          blurNsfw={c.appearance.blurNsfw}
          onBlurChange={blurNsfw => c.updateSettings({ blurNsfw })}
          onAdvanced={() => setAppearanceOpen(true)}
          advancedLabel={copy("displaySettings")}
        />
        {selectable && available ? <button
          type="button"
          className="layout-tool-trigger layout-selection-mode"
          aria-label={selectionVisible ? copy("selectionDone") : copy("selectWorks")}
          aria-pressed={selectionVisible}
          disabled={c.queryTransitioning}
          onClick={() => {
            setShowSelection(!selectionVisible);
            if (selectionVisible) c.setSelectedWorkIds(new Set());
          }}
        >{selectionVisible ? copy("completed") : copy("choose")}</button> : null}
      </div>
    </div>
  </>;

  const selection = selectionVisible && available ? <div className={'layout-selection-bar' + (c.selectedCount ? ' has-selection' : '')}>
    <button className="text-button" disabled={c.queryTransitioning} onClick={c.toggleSelectPage}>
      {t(c.pageAllSelected ? "gallery.deselectPage" : "works.select_page")}
    </button>
    {c.selectedCount ? <>
      <span>{t("gallery.selectedCount", { count: c.selectedCount })}</span>
      <button className="text-button" onClick={() => c.setSelectedWorkIds(new Set())}>{t("gallery.clearSelection")}</button>
      <button className="button danger-button" disabled={c.queryTransitioning || selectedBusy} onClick={requestBatchTrash}>{t("works.move_to_trash")}</button>
    </> : <span>{t("works.select_page")}</span>}
  </div> : undefined;

  return <div className="gallery-app">
        {c.derivativeProgress.data && c.derivativeProgress.data.total > 0 && c.derivativeProgress.data.remaining > 0 ? <DerivativeProgressCard progress={c.derivativeProgress.data} refreshing={c.derivativeProgress.isFetching} onRefresh={() => void c.derivativeProgress.refetch()} /> : null}
    <WorksView
      heading={scope === "trash" ? t("works.trash") : scope === "favorite" ? t("gallery.favoritesHeading") : t("gallery.worksHeading")}
      scopes={scopes}
      toolbar={toolbar}
      selection={selection}
      pagination={available && total > 0 && !c.works.isPlaceholderData ? <nav className="pagination" aria-label={t("gallery.pagination")}>
        <button className="button" disabled={c.page === 0 || c.queryTransitioning || c.works.isFetching} onClick={() => c.updateParams({ p: String(c.page - 1), work: null }, false, "push")}>{t("works.prev")}</button>
        <span>{t("gallery.pageCount", { page: c.page + 1, total: pages })}</span>
        <button className="button" disabled={c.page + 1 >= pages || c.queryTransitioning || c.works.isFetching} onClick={() => c.updateParams({ p: String(c.page + 1), work: null }, false, "push")}>{t("works.next")}</button>
      </nav> : undefined}
    >
      {c.works.isError ? <ErrorState error={c.works.error} onRetry={() => void c.works.refetch()} /> :
        c.works.isLoading ? <div className="state-view" role="status">{t("common.loading")}</div> :
        visibleWorkItems.length ? <><div aria-busy={c.works.isPlaceholderData} inert={c.works.isPlaceholderData || undefined}><WorkCollection mode={viewMode} size={c.appearance.workCardSize}>
          {visibleWorkItems.map((work, index) => <GalleryWorkItem
            key={work.id}
            work={work}
            settings={c.appearance}
            selected={c.selectedWorkIds.has(work.id)}
            canCurate={c.canCurate}
            onSelect={selectionVisible ? () => c.toggleSelectWork(work.id) : undefined}
            trashMode={scope === "trash"}
            canPurge={c.isAdmin}
            restoreBusy={restoring.has(work.id) || curating.has(work.id) || detailCurationIds.includes(work.id)}
            restoreError={operationErrors[work.id] || (latestCurations.get(work.id)?.status === "error" ? String(latestCurations.get(work.id)?.error || "") : undefined)}
            onRestore={() => void restore(work.id)}
            onPurge={() => requestPurge(work.id)}
            href={href({ work: work.id })}
            open={() => open(work.id)}
            index={index}
          />)}
        </WorkCollection></div>{c.works.isPlaceholderData && <p role="status">{t("common.loading")}</p>}</> :
        c.works.isPlaceholderData ? <div className="state-view" role="status">{t("common.loading")}</div> :
        moving.size || restoring.size || detailCurationIds.length ? <div className="state-view" role="status">{t("common.processing")}</div> :
        <div className="state-view"><h2>{t("gallery.noResults")}</h2><button className="button" onClick={c.clearSearch}>{t("gallery.clearFilters")}</button></div>}
    </WorksView>
    {filterOpen && filterDraft ? <WorksFilterDialog value={filterDraft} onChange={setFilterDraft} onApply={applyFilters} onClose={() => setFilterOpen(false)} /> : null}
    {c.slideshow.node}
    {appearanceOpen ? <AppearanceDialog value={c.appearance} onChange={patch => c.updateSettings(patch)} canSlideshow={c.slideItems.length>0} canMaintain={c.has("system")} onSlideshow={() => { setAppearanceOpen(false); c.slideshow.open(c.slideItems); }} onClose={() => setAppearanceOpen(false)} /> : null}
    {detail ? <WorkDetailDialog key={detail} id={detail} operationBusy={curating.has(detail) || restoring.has(detail)} onClose={close} /> : null}
    {confirm ? <Modal
      title={t(confirm.kind === "batch" ? "works.move_to_trash" : "deletion.permanent_title")}
      onClose={closeConfirm}
    >
      <p className="confirm-message">{t(confirm.kind === "batch" ? "works.batch_trash_confirm" : "works.purge_confirm", { count: confirm.ids.length })}</p>
      {confirmPending ? <p className="confirm-pending" role="status">{t("common.processing")}</p> : null}
      {confirmError ? <div className="confirm-error" role="alert"><p>{confirmError}</p></div> : null}
      <div className="confirm-actions">
        <button type="button" className="button" onClick={closeConfirm}>{confirmPending ? t("common.close") : t("common.cancel")}</button>
        <button type="button" className="button primary" aria-disabled={confirmPending || undefined} aria-busy={confirmPending || undefined} onClick={() => { if (!confirmPending) void runConfirm(); }}>{confirmError ? t("common.retry") : t("common.confirm")}</button>
      </div>
    </Modal> : null}
  </div>;
}


function AppearanceDialog({ value, onChange, onSlideshow, canSlideshow, canMaintain, onClose }: { value: AppearanceSettings; onChange: (patch: Partial<AppearanceSettings>) => void; onSlideshow: () => void; canSlideshow:boolean; canMaintain:boolean; onClose: () => void }) {
  const { copy } = useGalleryCopy();
  const t = useT();
  return <Modal title={copy("displaySettings")} onClose={onClose}>
    <div className="gallery-filter-dialog">
      <div className="gallery-setting-row"><span>{t("works.card_size")}</span><ToolbarSelect label={t("works.card_size")} value={value.workCardSize} onValueChange={workCardSize=>onChange({workCardSize:workCardSize as AppearanceSettings["workCardSize"]})} options={["small","medium","large"].map(size=>({value:size,label:t(`works.card_size_${size}`)}))}/></div>
      <label className="gallery-filter-checkbox"><input type="checkbox" checked={value.workCardShowCheckbox} onChange={event => onChange({ workCardShowCheckbox: event.target.checked })} />{t("gallery.showSelectionControls")}</label>
      <label className="gallery-filter-checkbox"><input type="checkbox" checked={value.workCardShowAi} onChange={event => onChange({ workCardShowAi: event.target.checked })} />{t("works.ai_badge")}</label>
      <label className="gallery-filter-checkbox"><input type="checkbox" checked={value.workCardShowNsfw} onChange={event => onChange({ workCardShowNsfw: event.target.checked })} />{t("works.nsfw_badge")}</label>
      <label className="gallery-filter-checkbox"><input type="checkbox" checked={value.workCardShowFavorite} onChange={event => onChange({ workCardShowFavorite: event.target.checked })} />{t("gallery.showFavoriteMarks")}</label>
      {canMaintain?<details><summary>{t("datamgmt.danger_clear_works")}</summary><ClearDomain embedded entity="works" title={t("datamgmt.danger_clear_works")} description={t("datamgmt.danger_clear_works_desc")} /></details>:null}
      <div className="confirm-actions"><button type="button" className="button" disabled={!canSlideshow} onClick={onSlideshow}>{t("slideshow.open")}</button><button type="button" className="button primary" onClick={onClose}>{copy("completed")}</button></div>
    </div>
  </Modal>;
}

function DerivativeProgressCard({ progress, refreshing, onRefresh }: { progress: MediaDerivativeProgress; refreshing: boolean; onRefresh: () => void }) {
  const t = useT();
  const fmt = useGalleryFormat();
  const percent = Math.min(100, Math.max(0, progress.completion_percent));
  const statusKey = progress.status === "stalled" ? "works.derivative_progress_stalled" : progress.status === "failed" ? "works.derivative_progress_failed" : progress.status === "waiting" ? "works.derivative_progress_waiting" : "works.derivative_progress_running";
  return <section className="layout-progress-card" role="region" aria-label={t("works.derivative_progress_region")}>
    <div className="layout-progress-heading"><div><strong>{t("works.derivative_progress_title")}</strong><p role="status">{t(statusKey)}</p></div><button type="button" className="text-button" onClick={onRefresh} disabled={refreshing}>{refreshing ? t("common.refreshing") : t("works.derivative_progress_refresh")}</button></div>
    <div className="layout-progress-track" role="progressbar" aria-label={t("works.derivative_progress_title")} aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(percent)}><span style={{transform:`scaleX(${percent / 100})`}} /></div>
    <div className="layout-progress-meta"><span>{t("works.derivative_progress_count", { completed: fmt.number(progress.completed), total: fmt.number(progress.total) })}</span><span>{t("works.derivative_progress_remaining", { remaining: fmt.number(progress.remaining), works: fmt.number(progress.affected_works) })}</span>{progress.last_completed_at ? <span>{t("works.derivative_progress_last", { time: fmt.date(progress.last_completed_at) })}</span> : null}</div>
  </section>;
}

function WorksFilterDialog({ value, onChange, onApply, onClose }: { value: WorksFilterValue; onChange: (value: WorksFilterValue) => void; onApply: () => void; onClose: () => void }) {
  const t = useT();
  const set = (patch: Partial<WorksFilterValue>) => onChange({ ...value, ...patch });
  return <Modal title={t("works.filter_panel_title")} onClose={onClose}>
    <div className="gallery-filter-dialog">
      <fieldset><legend>{t("works.filter_source")}</legend>{[{value:"pixiv",label:"Pixiv"},{value:"x",label:"X"},{value:"iwara",label:"Iwara"},{value:"danbooru",label:"Danbooru"},{value:"pinterest",label:"Pinterest"},{value:"lofter",label:"Lofter"},{value:"weibo",label:"Weibo"},{value:"bilibili",label:"Bilibili"},{value:"manual",label:t("gallery.localImport")}].map(option => <label key={option.value} className="gallery-filter-checkbox"><input type="checkbox" checked={value.sources.includes(option.value)} onChange={event => onChange({ ...value, sources: event.target.checked ? [...value.sources, option.value] : value.sources.filter(item => item !== option.value) })} />{option.label}</label>)}</fieldset>
      <div className="gallery-setting-row"><span>{t("works.filters")}</span><ToolbarSelect label={t("works.filters")} value={value.safety} onValueChange={safety=>set({safety:safety as WorksFilterValue["safety"]})} options={[{value:"all",label:t("works.filter_all")},{value:"sfw",label:t("works.filter_sfw")},{value:"nsfw",label:t("works.filter_nsfw")}]}/></div>
      <div className="gallery-setting-row"><span>{t("works.ai_section")}</span><ToolbarSelect label={t("works.ai_section")} value={value.ai} onValueChange={ai=>set({ai:ai as WorksFilterValue["ai"]})} options={[{value:"all",label:t("works.ai_filter_all")},{value:"human",label:t("works.ai_filter_human")},{value:"ai",label:t("works.ai_filter_ai")}]}/></div>
      <label className="gallery-filter-checkbox"><input type="checkbox" checked={value.favorite} onChange={event => set({ favorite: event.target.checked })} />{t("works.filter_favorites")}</label>
      <fieldset><legend>{t("works.media_type")}</legend>{(["image", "animation", "video", "multiple-assets"] as const).map(kind => <label key={kind} className="gallery-filter-checkbox"><input type="checkbox" checked={value.media.includes(kind)} onChange={event => set({ media: event.target.checked ? [...value.media, kind] : value.media.filter(item => item !== kind) })} />{t(kind === "multiple-assets" ? "works.media_multiple" : `works.media_${kind}`)}</label>)}</fieldset>
      <div className="confirm-actions"><button type="button" className="button" onClick={onClose}>{t("common.cancel")}</button><button type="button" className="button primary" onClick={onApply}>{t("common.apply")}</button></div>
    </div>
  </Modal>;
}

function GalleryWorkItem({ work: w, settings, selected, canCurate, onSelect, trashMode = false, canPurge, restoreBusy = false, restoreError, onRestore, onPurge, href, open, index }: {
  work: WorkListItem;
  settings: AppearanceSettings;
  selected: boolean;
  canCurate: boolean;
  onSelect?: () => void;
  trashMode?: boolean;
  canPurge: boolean;
  restoreBusy?: boolean;
  restoreError?: string;
  onRestore?: () => void;
  onPurge?: () => void;
  href: string;
  open: () => void;
  index: number;
}) {
  const t = useT();
  const fmt = useGalleryFormat();
  const favorite = useWorkFavorite(w.id);
  const ratio = (w.thumbnail_width || 1440) / (w.thumbnail_height || 900);
  const ratioName = ratio < .85 ? "portrait" : ratio <= 1.1 ? "square" : "landscape";
  const favoriteError = favorite.isError ? (favorite.error instanceof Error ? favorite.error.message : String(favorite.error)) : undefined;
  const intercept = (event: React.MouseEvent<HTMLAnchorElement>) => {
    if (event.button || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    open();
  };
  return <WorkItem
    id={w.id}
    title={w.title || t("works.untitled")}
    creator={w.creator_name || "—"}
    source={w.source === "pixiv" ? "Pixiv" : w.source === "manual" ? t("gallery.localImport") : w.source || "—"}
    dateTime={w.created_at}
    dateLabel={fmt.date(w.created_at)}
    ratio={ratioName}
    assets={w.asset_count}
    adult={w.is_nsfw && settings.workCardShowNsfw}
    ai={w.is_ai_generated && settings.workCardShowAi}
    href={href}
    onOpen={intercept}
    media={<WorkMediaThumbnail assetId={w.thumbnail_asset_id || undefined} hasVideo={w.has_video} alt={w.title || ""} className={w.is_nsfw && settings.blurNsfw ? "blurred" : ""} eager={index < 3} />}
    selected={selected}
    onSelect={onSelect}
    favorite={canCurate && !trashMode && settings.workCardShowFavorite ? {
      active: favorite.optimistic ?? Boolean(w.is_favorite),
      busy: favorite.isPending,
      disabled: restoreBusy,
      error: favoriteError,
      onToggle: () => favorite.toggle(),
    } : undefined}
    trashActions={trashMode && canCurate ? <div className="trash-actions">
      <button className="text-button" disabled={restoreBusy} onClick={() => onRestore?.()}>{t("works.restore")}</button>
      {canPurge ? <button className="text-button danger-text" disabled={restoreBusy} onClick={() => onPurge?.()}>{t("deletion.permanent_title")}</button> : null}
      {restoreError ? <p className="operation-feedback is-error" role="alert">{restoreError}</p> : null}
    </div> : restoreError ? <p className="operation-feedback is-error" role="alert">{restoreError}</p> : undefined}
  />;
}

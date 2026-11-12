"use client";

import Link from "next/link";
import dynamic from "next/dynamic";
import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { api, queryKeys, type TaskRun } from "@/lib/api";
import { useT } from "@/lib/i18n";
import { statusLabel } from "@/lib/i18n-format";
import { hasTaskAction, actionErrorReason, partitionTaskAction, reconcileTaskBulkResult, repeatSyncConflict, type TaskAction } from "@/lib/task-actions";
import { useTaskCommands } from "@/lib/useTaskCommand";
import { POLL_ACTIVE_MS } from "@/lib/polling";
import { taskDisplayStatus, taskCreator, taskReason, taskProgress, terminalTaskStatuses } from "@/lib/taskPresentation";
import ErrorState from "@/components/ErrorState";
import DownloadConflictDialog from "@/components/DownloadConflictDialog";
import { ToolbarSelect, SearchField } from "@/features/gallery-ui/Controls";
import { JobsView, TaskTable, type ScopeLink } from "@/features/gallery-ui/CollectionViews";
import { Modal } from "@/features/gallery-ui/Modal";
import { TaskDetailView } from "@/features/gallery-ui/DetailViews";
import { useGalleryCopy } from "@/features/gallery-ui/copy";
import { TaskStatus } from "@/features/gallery-ui/TaskPrimitives";
import { useTaskList, type TaskListController } from "./useTaskList";
import { useGalleryFormat } from "@/features/gallery-ui/format";
import { useJobEvents } from "@/lib/useWebSocket";
import { aggregateTaskGroup, buildTaskTree, flattenTaskNode, isAttentionStatus } from "@/lib/jobTree";
const LegacyJobDrawer = dynamic(() => import("./legacyJobDrawer"));

function taskKindKey(task: TaskRun) {
  return ["download", "import", "admin"].includes(task.kind) ? `gallery.taskKind_${task.kind}` : "common.unknown";
}

const ACTIONS: TaskAction[] = ["retry", "pause", "resume", "cancel", "repeat_sync", "delete", "acknowledge"];
const BATCH_ACTIONS: TaskAction[] = ["retry", "pause", "resume", "cancel", "delete"];

function actionLabel(t: ReturnType<typeof useT>, action: TaskAction) {
  if (action === "retry") return t("jobs.retry");
  if (action === "pause") return t("jobs.pause");
  if (action === "resume") return t("jobs.resume");
  if (action === "cancel") return t("jobs.batch_action_cancel");
  if (action === "repeat_sync") return t("jobs.repeat_sync");
  if (action === "delete") return t("jobs.delete_task_title");
  return t("operations.acknowledge");
}

type TaskSurface = "task" | "download" | "import";
type ConfirmState =
  | { kind: "task"; task: TaskRun; action: TaskAction; detailOrigin: boolean; surface: TaskSurface }
  | { kind: "batch"; tasks: TaskRun[]; action: TaskAction; ineligible: Array<{ row: TaskRun; reason: string }>; surface: TaskSurface };

type ActionProps = {
  task: TaskRun;
  pendingAction?: TaskAction;
  notice?: string;
  error?: string;
  surface: TaskSurface;
  onAction: (action: TaskAction, surface: TaskSurface) => void;
  onConfirm: (action: TaskAction, surface: TaskSurface) => void;
};

function TaskActionButtons({ task, pendingAction, notice, error, surface, onAction, onConfirm }: ActionProps) {
  const t = useT();
  const initiatingControl = useRef<HTMLButtonElement | null>(null);
  const recoveryTarget = useRef<HTMLElement | null>(null);
  useEffect(() => {
    if (!initiatingControl.current || initiatingControl.current.isConnected) return;
    if (document.activeElement === document.body && document.hasFocus() && recoveryTarget.current?.isConnected && !recoveryTarget.current.closest("[inert]")) {
      recoveryTarget.current.focus();
    }
    initiatingControl.current = null;
    recoveryTarget.current = null;
  });
  const available = ACTIONS.filter(action => hasTaskAction(task, action) && (action !== "repeat_sync" || (task.subject_type === "download_job" && Boolean(task.subject_id))));
  const repeatUnavailable = hasTaskAction(task, "repeat_sync") && !available.includes("repeat_sync");
  return <>
    {available.map(action => {
      const busy = pendingAction === action;
      const prominent = ["retry", "repeat_sync", "pause", "resume"].includes(action);
      const muted = ["cancel", "delete", "acknowledge"].includes(action);
      return <button key={action} type="button" className={`text-button ${prominent ? "prominent-action" : muted ? "muted-action" : ""}`} aria-disabled={pendingAction ? "true" : undefined} aria-busy={busy || undefined} aria-label={busy ? `${actionLabel(t, action)}：${t("common.processing")}` : undefined} onClick={event => {
        if (pendingAction) return;
        initiatingControl.current = event.currentTarget;
        recoveryTarget.current = event.currentTarget.closest("[data-task-id]")?.querySelector<HTMLElement>("a") || event.currentTarget.closest("[role=dialog], dialog")?.querySelector<HTMLElement>("button") || null;
        if (["delete", "repeat_sync", "cancel"].includes(action)) onConfirm(action, surface); else onAction(action, surface);
      }}>{actionLabel(t, action)}</button>;
    })}
    {repeatUnavailable && <span className="operation-feedback muted">{t("jobs.repeat_original_unavailable")}</span>}
    {notice && <p role="status" className="operation-feedback task-operation-feedback">{notice}</p>}
    {error && <p role="alert" className="operation-feedback is-error task-operation-feedback">{error}</p>}
  </>;
}

function TaskDetailDialog({ id, c, pendingAction, notice, error, onAction, onConfirm }: { id: string; c: TaskListController; pendingAction?: TaskAction; notice?: string; error?: string; onAction: (task: TaskRun, action: TaskAction) => void; onConfirm: (task: TaskRun, action: TaskAction) => void }) {
  const t = c.t;
  const fmt = useGalleryFormat();
  const [showConflict, setShowConflict] = useState(false);
  const query = useQuery({ queryKey: queryKeys.tasks.detail(id), queryFn: () => api.getTask(id), refetchInterval: q => q.state.data && !terminalTaskStatuses.has(q.state.data.status) ? POLL_ACTIVE_MS : false, refetchIntervalInBackground: false });
  if (query.isPending) return <Modal title={t("jobs.task_detail")} onClose={c.closeTask} sheet><div className="state-view" role="status">{t("common.loading")}</div></Modal>;
  if (query.isError) return <Modal title={t("jobs.task_detail")} onClose={c.closeTask} sheet><ErrorState error={query.error} onRetry={() => void query.refetch()} /></Modal>;
  const task = query.data;
  const existingResolution = task.meta?.staging_conflict_resolution as { resolution_id: string; expires_at?: string } | undefined;
  const hasConflictWorkflow = task.reason_code === "download_staging_conflict" || !!existingResolution;
  const status = taskDisplayStatus(task);
  const progress = taskProgress(task);
  const reason = taskReason(t, task);
  const facts = [
    { label: t("gallery.taskKindLabel"), value: t(taskKindKey(task)) },
    { label: t("gallery.creator"), value: taskCreator(task) },
    { label: t("gallery.completedAssets"), value: `${progress.current} / ${progress.total}` },
    { label: t("gallery.created"), value: fmt.date(task.created_at) },
  ];
  return <><Modal title={t("jobs.task_detail")} onClose={c.closeTask} sheet><TaskDetailView beforeActions={hasConflictWorkflow ? <button type="button" className="text-button prominent-action" onClick={() => setShowConflict(true)}>{t(existingResolution ? "jobs.conflict.view_resolution" : "jobs.conflict.compare")}</button> : undefined} id={task.id} title={task.title || task.operation_type || task.id} status={<TaskStatus value={status} label={t(`gallery.taskStatus_${status}`)} />} facts={facts} reason={reason || undefined} actions={<div className="task-actions"><TaskActionButtons task={task} surface="task" pendingAction={pendingAction} notice={notice} error={error} onAction={action => onAction(task, action)} onConfirm={action => onConfirm(task, action)} /></div>}><details><summary>{t("gallery.techDetails")}</summary><pre translate="no">{JSON.stringify(task, null, 2)}</pre></details>{!task.available_actions?.length && task.disabled_reasons && <p className="operation-feedback muted">{Object.values(task.disabled_reasons).filter(Boolean).join("；") || t("gallery.noActions")}</p>}</TaskDetailView></Modal>{showConflict && <DownloadConflictDialog open taskId={id} existingResolution={existingResolution} onClose={() => setShowConflict(false)} />}</>;
}

export default function JobsPage() {
  const { copy } = useGalleryCopy();
  const c = useTaskList();
  useJobEvents({ enabled: c.allowed, onProgress: () => { void c.query.refetch(); } });
  const t = c.t;
  const fmt = useGalleryFormat();
  const summary = c.query.data?.summary;
  const commands = useTaskCommands(c.permissions.user?.id);
  const workbench = useQuery({ queryKey: queryKeys.workbench, queryFn: api.workbench, enabled: c.permissions.has("system"), staleTime: 15000 });
  const [maintenanceAction, setMaintenanceAction] = useState<"clear" | "retryAll" | "killStuck" | "filterRetry" | "compact" | null>(null);
  const [maintenanceResult, setMaintenanceResult] = useState<{ matched?: number; succeeded?: number; failed?: number; deleted?: number; errors?: Array<{ id?: string; error?: unknown }> } | null>(null);
  const maintenanceFiltersRef = useRef<Record<string, string>>({});
  const clearJobs = useMutation({ mutationFn: () => api.clearDownloadJobs(["complete"]), onSuccess: result => { const value = result as { total_matched?: number; matched?: number; succeeded?: number; failed?: number; deleted?: number; errors?: Array<{ id?: string; error?: unknown }> }; setMaintenanceResult({ matched: value.total_matched ?? value.matched, succeeded: value.succeeded, failed: value.failed, deleted: value.deleted, errors: value.errors }); if (!(value.failed || value.errors?.length)) setMaintenanceAction(null); }, onSettled: commands.invalidate });
  const retryAllFailed = useMutation({ mutationFn: () => api.retryAllFailedJobs(), onSuccess: result => { const value = result as { total_matched?: number; matched?: number; succeeded?: number; failed?: number; deleted?: number; errors?: Array<{ id?: string; error?: unknown }> }; setMaintenanceResult({ matched: value.total_matched ?? value.matched, succeeded: value.succeeded, failed: value.failed, deleted: value.deleted, errors: value.errors }); if (!(value.failed || value.errors?.length)) setMaintenanceAction(null); }, onSettled: commands.invalidate });
  const killStuck = useMutation({ mutationFn: () => api.killStuckJobs(), onSuccess: () => setMaintenanceAction(null), onSettled: commands.invalidate });
  const filterRetry = useMutation({ mutationFn: () => commands.byFilter(maintenanceFiltersRef.current, "retry"), onSuccess: result => { const value = result as { total_matched?: number; matched?: number; succeeded?: number; failed?: number; errors?: Array<{ id?: string; error?: unknown }> }; setMaintenanceResult({ matched: value.total_matched ?? value.matched, succeeded: value.succeeded, failed: value.failed, errors: value.errors }); if (!(value.failed || value.errors?.length)) setMaintenanceAction(null); }, onSettled: commands.invalidate });
  const filterRetryAllowed = c.records === "download" && !c.q && Boolean(c.status || c.source || c.subscriptionSourceId);
  const compactPreview = useMutation({ mutationFn: () => api.compactTasks(true) });
  function openMaintenance(action: "clear" | "retryAll" | "killStuck" | "filterRetry" | "compact") {
    if (action === "filterRetry" && !filterRetryAllowed) return;
    maintenanceFiltersRef.current = Object.fromEntries(Object.entries({ status: c.status, source: c.source, subscription_source_id: c.subscriptionSourceId }).filter(([, value]) => Boolean(value))) as Record<string, string>;
    clearJobs.reset(); retryAllFailed.reset(); killStuck.reset(); filterRetry.reset(); compactPreview.reset(); compactNow.reset();
    setMaintenanceResult(null); setMaintenanceAction(action);
  }
  const compactNow = useMutation({ mutationFn: (token: string) => api.compactTasks(false, 1000, token), onSuccess: () => setMaintenanceAction(null), onSettled: commands.invalidate });
  const pendingRef = useRef<Record<string, TaskAction>>({});
  const confirmToken = useRef(0);
  const scopeRef = useRef(`${c.q}|${c.kind}|${c.records}|${c.status}|${c.scope}|${c.page}|${c.source}|${c.sort}|${c.order}|${c.downloadJobFilterId}|${c.subscriptionSourceId}`);
  const [selectionMode, setSelectionMode] = useState(false);
  useEffect(() => setSelectionMode(false), [c.records]);
  const [pending, setPending] = useState<Record<string, TaskAction>>({});
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [conflicts, setConflicts] = useState<Record<string, string>>({});
  const [notices, setNotices] = useState<Record<string, string>>({});
  const [confirm, setConfirm] = useState<ConfirmState | null>(null);
  const [batchAction, setBatchAction] = useState<TaskAction | "">("retry");
  const [batchPending, setBatchPending] = useState(false);
  const [batchOutcome, setBatchOutcome] = useState<Array<{ id: string; reason: string }> | null>(null);

  useEffect(() => {
    scopeRef.current = `${c.q}|${c.kind}|${c.records}|${c.status}|${c.scope}|${c.page}|${c.source}|${c.sort}|${c.order}|${c.downloadJobFilterId}|${c.subscriptionSourceId}`;
    setNotices({});
    setBatchOutcome(null);
  }, [c.taskId, c.q, c.kind, c.records, c.status, c.scope, c.page, c.source, c.sort, c.order, c.downloadJobFilterId, c.subscriptionSourceId]);

  function setTaskPending(id: string, action: TaskAction | null) {
    const next = { ...pendingRef.current };
    if (action) next[id] = action; else delete next[id];
    pendingRef.current = next;
    setPending(next);
  }

  async function runAction(task: TaskRun, action: TaskAction, detailOrigin = false, surface: TaskSurface = "task"): Promise<boolean> {
    const detailPath = typeof window === "undefined" ? "" : window.location.pathname;
    const isCurrentDetail = () => {
      if (typeof window === "undefined") return false;
      const url = new URL(window.location.href);
      return url.pathname === detailPath && (url.searchParams.get("task") || url.searchParams.get("detail")) === task.id;
    };
    if (pendingRef.current[task.id]) return false;
    setTaskPending(task.id, action);
    setErrors(previous => { const next = { ...previous }; delete next[task.id]; return next; });
    setConflicts(previous => { const next = { ...previous }; delete next[task.id]; return next; });
    try {
      const result = await commands.execute(task, action, surface);
      setNotices(previous => ({ ...previous, [task.id]: t("jobs.action_accepted") }));
      if (action === "repeat_sync" && detailOrigin && isCurrentDetail() && result && typeof result === "object" && "task_id" in result && typeof result.task_id === "string") {
        setNotices(previous => ({ ...previous, [result.task_id as string]: t("jobs.action_accepted") }));
        c.update({ task: result.task_id as string }, false);
      } else if (action === "delete" && detailOrigin && isCurrentDetail()) c.closeTask();
      return true;
    } catch (error) {
      const conflict = action === "repeat_sync" ? repeatSyncConflict(error) : null;
      const message = conflict?.kind === "existing" ? `${actionErrorReason(error)} (${conflict.existingJobId})` : actionErrorReason(error);
      setErrors(previous => ({ ...previous, [task.id]: message }));
      if (conflict?.kind === "existing") setConflicts(previous => ({ ...previous, [task.id]: conflict.existingJobId }));
      return false;
    } finally {
      setTaskPending(task.id, null);
      commands.invalidate();
    }
  }

  function handleAction(task: TaskRun, action: TaskAction, surface: TaskSurface = "task") { void runAction(task, action, c.taskId === task.id, surface); }
  function closeConfirm() { confirmToken.current += 1; setConfirm(null); }
  function requestAction(task: TaskRun, action: TaskAction, surface: TaskSurface = "task") {
    if (pendingRef.current[task.id]) return;
    if (["delete", "repeat_sync", "cancel"].includes(action)) {
      confirmToken.current += 1;
      setConfirm({ kind: "task", task, action, detailOrigin: c.taskId === task.id, surface });
    } else handleAction(task, action, surface);
  }

  async function runBatch(state: Extract<ConfirmState, { kind: "batch" }>, token: number) {
    if (batchPending || state.tasks.some(task => pendingRef.current[task.id])) return;
    const scopeAtSubmit = scopeRef.current;
    setBatchPending(true);
    setErrors({});
    state.tasks.forEach(task => setTaskPending(task.id, state.action));
    const submission = { selectedIds: [...state.tasks.map(task => task.id), ...state.ineligible.map(item => item.row.id)], eligibleIds: state.tasks.map(task => task.id), localRefusals: state.ineligible.map(item => ({ id: item.row.id, reason: item.reason })) };
    try {
      const response = await commands.batch(state.tasks, state.action, state.surface);
      const result = reconcileTaskBulkResult(submission, response as { succeeded?: number; failed?: number; errors?: Array<{ id: string; error: unknown }> });
      if (scopeRef.current === scopeAtSubmit) {
        setBatchOutcome(result.retained.map(item => ({ id: item.id, reason: item.reason })));
        c.setSelected(previous => new Set([...previous].filter(id => !result.confirmedSuccessIds.includes(id))));
        setNotices(previous => Object.fromEntries([...Object.entries(previous), ...result.confirmedSuccessIds.map(id => [id, t("jobs.action_accepted")])]));
      }
      if (confirmToken.current === token) setConfirm(null);
    } catch (error) {
      if (scopeRef.current === scopeAtSubmit) setBatchOutcome(state.ineligible.map(item => ({ id: item.row.id, reason: item.reason })).concat(state.tasks.map(task => ({ id: task.id, reason: actionErrorReason(error) }))));
    } finally {
      state.tasks.forEach(task => setTaskPending(task.id, null));
      setBatchPending(false);
      commands.invalidate();
    }
  }

  function prepareBatch() {
    const action = batchAction as TaskAction;
    if (!action || !c.selected.size || c.busy || c.isComposing) return;
    const selectedRows = c.rows.filter(task => c.selected.has(task.id));
    const busyRows = selectedRows.filter(task => Boolean(pendingRef.current[task.id]));
    const availableRows = selectedRows.filter(task => !pendingRef.current[task.id]);
    const preview = partitionTaskAction(availableRows, c.selected, action);
    const ineligible = [...preview.ineligible, ...busyRows.map(row => ({ row, reason: t("common.processing") }))];
    setBatchOutcome(ineligible.map(item => ({ id: item.row.id, reason: item.reason })));
    if (!preview.eligible.length) return;
    confirmToken.current += 1;
    setConfirm({ kind: "batch", tasks: preview.eligible, action, ineligible, surface: (c.recordKind || "task") as TaskSurface });
  }

  function intercept(event: React.MouseEvent<HTMLAnchorElement>, scope: string) {
    if (event.button || event.ctrlKey || event.metaKey || event.altKey || event.shiftKey) return;
    event.preventDefault();
    c.update({ scope: scope === "all" ? null : scope, status: null });
  }

  const leaveRecords = { kind: null, records: null, q: null, status: null, scope: null, source: null, sort: null, order: null, download_job_id: null, subscription_source_id: null, job: null, import_job: null };
  const scopes: ScopeLink[] = c.recordKind ? [{ value: "related", label: c.relatedRecord ? t(c.recordKind === "download" ? "gallery.relatedDownloads" : "gallery.relatedImports") : t(c.recordKind === "download" ? "jobs.download" : "jobs.imports"), href: c.href({ scope: null }, true), active: true, onClick: event => { if (event.button || event.ctrlKey || event.metaKey || event.altKey || event.shiftKey) return; event.preventDefault(); c.update({ scope: null }); } }, { value: "all", label: t("gallery.allTasks"), href: c.href(leaveRecords, true), active: false, onClick: event => { if (event.button || event.ctrlKey || event.metaKey || event.altKey || event.shiftKey) return; event.preventDefault(); c.update(leaveRecords); } }] : [["all", "gallery.allTasks"], ["attention", "gallery.attention"], ["running", "gallery.running"], ["waiting", "gallery.waiting"]].map(([value, key]) => ({
    value,
    label: t(key),
    href: c.href({ scope: value === "all" ? null : value, status: null }, true),
    active: c.scope === value && !c.status,
    count: String(summary?.[value as "all" | "attention" | "running" | "waiting"] ?? "—"),
    onClick: event => intercept(event, value),
  }));
  const available = c.rows.length > 0;
  const selecting = selectionMode && c.allowed;
  const toolbar = <>
    <SearchField value={c.input} onChange={value => c.setInput(value)} onComposingChange={composing => { c.setIsComposing(composing); if (composing) c.setSelected(new Set()); }} placeholder={copy("taskSearch")} />
    <div className="layout-toolbar-actions">
      <div className="layout-toolbar-filters">
        {!c.recordKind && <ToolbarSelect label={t("gallery.taskKindLabel")} value={c.kind} onValueChange={kind => c.update({ kind: kind || null })} options={[{ value: "", label: t("gallery.allKinds") }, ...["download", "import", "admin"].map(value => ({ value, label: t(`gallery.taskKind_${value}`) }))]} />}
        {c.recordKind ? <>
          <ToolbarSelect label={t("gallery.taskStatusLabel")} value={c.status} onValueChange={status => c.update({ status: status || null, scope: null })} options={[{ value: "", label: t("gallery.allStates") }, ...(c.recordKind === "download" ? ["enqueued", "downloading", "paused", "downloaded", "importing", "failed", "stale", "complete", "cancelled"] : ["enqueued", "running", "paused", "recovering", "failed", "stale", "complete", "cancelled"]).map(value => ({ value, label: value === "recovering" ? t("gallery.taskStatus_recovering") : statusLabel(t, value) }))]} />
          {c.recordKind === "download" && <ToolbarSelect label={t("gallery.source")} value={c.source} onValueChange={source => c.update({ source: source || null })} options={[{ value: "", label: t("gallery.allSources") }, ...["pixiv", "x", "iwara", "danbooru", "pinterest", "lofter", "weibo", "bilibili", "manual"].map(value => ({ value, label: value }))]} />}
          {c.recordKind === "download" && <ToolbarSelect label={t("gallery.sort")} value={c.sort} onValueChange={sort => c.update({ sort: sort || null })} options={[{ value: "created-desc", label: t("gallery.sortNewest") }, { value: "created-asc", label: t("gallery.sortOldest") }, { value: "updated-desc", label: t("gallery.sortUpdated") }]} />}
        </> : <ToolbarSelect label={t("gallery.taskStatusLabel")} value={c.scope === "all" ? c.status : c.scope} onValueChange={value => { const scoped = ["attention", "running", "waiting"].includes(value); c.update({ scope: scoped ? value : null, status: scoped ? null : value || null }); }} options={[{ value: "", label: t("gallery.allStates") }, { value: "attention", label: t("gallery.attention") }, ...["running", "waiting", "enqueued", "recovering", "paused", "failed", "stale", "complete", "cancelled"].map(value => ({ value, label: t(`gallery.taskStatus_${value}`) }))]} />}

      </div>
      {available && c.allowed && (!c.recordKind || c.permissions.has("tasks")) && <div className="layout-toolbar-utilities"><button type="button" className="layout-tool-trigger layout-selection-mode" aria-label={selecting ? copy("selectionDone") : copy("selectTasks")} aria-pressed={selecting} disabled={c.busy || c.isComposing} onClick={() => { setSelectionMode(value => !value); c.setSelected(new Set()); }}>{selecting ? copy("completed") : copy("choose")}</button></div>}
    </div>
  </>;

  const selection = selecting && available ? <div className={`layout-selection-bar${c.selected.size ? " has-selection" : ""}`}>
    <button type="button" className="text-button" disabled={c.busy || c.isComposing} onClick={c.togglePage}>{c.allSelected ? t("gallery.clearTaskSelection") : t("gallery.selectTasks")}</button>
    {c.selected.size ? <><span>{t("common.selected_count", { count: c.selected.size })}</span><ToolbarSelect label={t("jobs.batch_action")} value={batchAction} onValueChange={value => setBatchAction(value as TaskAction | "")} options={BATCH_ACTIONS.map(action => ({ value: action, label: actionLabel(t, action) }))} /><button type="button" className="button primary" disabled={!batchAction || c.busy || c.isComposing} aria-disabled={batchPending ? "true" : undefined} aria-busy={batchPending || undefined} onClick={() => { if (batchPending) return; prepareBatch(); }}>{copy("applyBatch")}</button><button type="button" className="text-button" onClick={() => c.setSelected(new Set())}>{t("gallery.clearTaskSelection")}</button></> : <span>{copy("selectionHint")}</span>}
  </div> : undefined;

  const groupedRoots = useMemo(() => buildTaskTree(c.query.isPlaceholderData ? [] : c.rows).filter(node => node.children.length > 0), [c.rows, c.query.isPlaceholderData]);
  const rows = c.rows.map(task => {
    const status = taskDisplayStatus(task);
    const progress = taskProgress(task);
    const reason = taskReason(t, task);
    const rawStatus = String(task.meta?.record_status || task.status);
    const label = c.recordKind ? rawStatus === "recovering" ? t("gallery.taskStatus_recovering") : statusLabel(t, rawStatus) : t(`gallery.taskStatus_${status}`);
    return { id: task.id, title: task.title || task.operation_type || task.id, href: c.href(c.recordKind === "download" ? { job: task.id } : c.recordKind === "import" ? { import_job: task.id } : { task: task.id }), onOpen: (event: React.MouseEvent<HTMLAnchorElement>) => { if (event.button || event.ctrlKey || event.metaKey || event.altKey || event.shiftKey) return; event.preventDefault(); c.update(c.recordKind === "download" ? { job: task.id } : c.recordKind === "import" ? { import_job: task.id } : { task: task.id }, false, true); }, kind: t(taskKindKey(task)), creator: taskCreator(task), dateTime: task.created_at || "", dateLabel: fmt.date(task.created_at), reason: reason || undefined, danger: ["failed", "stale"].includes(status), selected: c.selected.has(task.id), onSelect: () => { if (!c.busy && !c.isComposing) c.toggle(task.id); }, status: <TaskStatus value={status} label={label} />, statusLabel: label, progress: ((!c.recordKind && ["running", "recovering", "complete"].includes(status)) || (c.recordKind && (progress.total > 0 || progress.current > 0))) ? { value: progress.percent, resources: t("gallery.progressCount", { done: progress.current, total: progress.total }) } : undefined, emptyProgress: status === "waiting" ? t("gallery.waitExecution") : status === "paused" ? t("gallery.keepProgress") : "—", actions: <div className="task-actions"><TaskActionButtons task={task} surface={(c.recordKind || "task") as TaskSurface} pendingAction={pending[task.id]} notice={notices[task.id]} error={errors[task.id]} onAction={(action, surface) => handleAction(task, action, surface)} onConfirm={(action, surface) => requestAction(task, action, surface)} /></div> };
  });

  const maintenanceBusy = clearJobs.isPending || retryAllFailed.isPending || killStuck.isPending || filterRetry.isPending || compactNow.isPending;
  const maintenanceTitle = maintenanceAction === "clear" ? t("jobs.clear_complete") : maintenanceAction === "retryAll" ? t("jobs.retry_all_failed") : maintenanceAction === "killStuck" ? t("jobs.kill_stuck") : maintenanceAction === "filterRetry" ? t("jobs.retry_all_failed") : t("jobs.preview_compaction");
  const maintenanceError = maintenanceAction === "clear" ? clearJobs.error : maintenanceAction === "retryAll" ? retryAllFailed.error : maintenanceAction === "killStuck" ? killStuck.error : maintenanceAction === "filterRetry" ? filterRetry.error : compactPreview.error || compactNow.error;
  function runMaintenance() {
    if (maintenanceAction === "clear") clearJobs.mutate();
    else if (maintenanceAction === "retryAll") retryAllFailed.mutate();
    else if (maintenanceAction === "killStuck") killStuck.mutate();
    else if (maintenanceAction === "filterRetry") filterRetry.mutate();
    else if (maintenanceAction === "compact" && !compactPreview.isPending && compactPreview.data) compactNow.mutate(compactPreview.data.preview_token);
  }
  return <div className="gallery-app">
    {!c.allowed ? <div className="state-view" role="alert"><h1>{t("gallery.noTaskPermission")}</h1></div> : <JobsView scopes={scopes} toolbar={toolbar} selection={selection} failures={batchOutcome?.length ? <section className="batch-errors" role="alert"><h2>{t("jobs.batch_partial")}</h2><ul>{batchOutcome.map(item => <li key={item.id}><span translate="no">{item.id.slice(0, 8)}</span>：{item.reason}</li>)}</ul></section> : undefined}>
      {c.recordScopeUnsupported ? <div className="state-view" role="status"><p>{t("gallery.recordScopeUnsupported")}</p><button type="button" className="button" onClick={() => c.update({ scope: null })}>{t("gallery.showRelatedRecords")}</button></div> : c.query.isPending ? <div className="state-view" role="status">{t("common.loading")}</div> : c.query.isError ? <ErrorState error={c.query.error} onRetry={() => void c.query.refetch()} /> : c.rows.length ? <><div aria-busy={c.query.isPlaceholderData} inert={c.query.isPlaceholderData || undefined}><TaskTable selecting={selecting} selectionDisabled={c.busy || c.isComposing} rows={rows} /></div>{c.query.isPlaceholderData && <p role="status">{t("common.loading")}</p>}</> : c.query.isPlaceholderData ? <div className="state-view" role="status">{t("common.loading")}</div> : <div className="state-view"><h2>{t("gallery.emptyTasks")}</h2><button className="button" onClick={() => c.update(leaveRecords)}>{t("gallery.clearFilters")}</button></div>}
      {!c.recordScopeUnsupported && !c.query.isPlaceholderData && (c.totalKnown ? c.total > 12 : c.page > 0 || c.hasNext) && <nav className="pagination" aria-label={t("gallery.taskPagination")}><button className="button" disabled={c.page === 0 || c.busy} onClick={() => c.update({ p: String(c.page - 1) }, false, true)}>{t("works.prev")}</button><span>{c.totalKnown ? t("gallery.pageCount", { page: c.page + 1, total: c.pages }) : t("works.page", { page: c.page + 1 })}</span><button className="button" disabled={(c.totalKnown ? c.page + 1 >= c.pages : !c.hasNext) || c.busy} onClick={() => c.update({ p: String(c.page + 1) }, false, true)}>{t("works.next")}</button></nav>}
    </JobsView>}
    {workbench.data && <details className="jobs-workbench-summary"><summary>{t("gallery.workbenchSummary")}</summary><div className="jobs-workbench-grid" aria-live="polite"><span><b>{t("jobs.summary_active")}</b> {workbench.data.queue.active_download_count + workbench.data.queue.active_import_count}</span><span><b>{t("jobs.summary_queued")}</b> {workbench.data.queue.default + workbench.data.queue.scheduled}</span><span><b>{t("jobs.summary_failed")}</b> {workbench.data.queue.failed_download_count + workbench.data.queue.failed_import_count}</span><span><b>{t("jobs.summary_stale")}</b> {workbench.data.queue.stale_count}</span></div></details>}
    {c.allowed && (c.permissions.has("tasks") || c.permissions.has("system")) && <details className="jobs-workbench-summary"><summary>{t("gallery.recordTools")}</summary>{c.permissions.has("tasks") && <nav className="jobs-maintenance-actions" aria-label={t("gallery.recordTools")}>{(["download", "import"] as const).map(records => <Link key={records} className="text-link" href={c.href({ ...leaveRecords, records, source: null, sort: null, order: null }, true)}>{t(records === "download" ? "jobs.download" : "jobs.imports")}</Link>)}</nav>}<div className="jobs-maintenance-actions">{c.records === "download" && c.permissions.has("tasks") && <><button type="button" className="text-button" onClick={() => { openMaintenance("clear"); }} disabled={maintenanceBusy}>{t("jobs.clear_complete")}</button><button type="button" className="text-button" onClick={() => { openMaintenance("retryAll"); }} disabled={maintenanceBusy}>{t("jobs.retry_all_failed")}</button>{filterRetryAllowed && <button type="button" className="text-button" onClick={() => { openMaintenance("filterRetry"); }} disabled={maintenanceBusy}>{t("jobs.batch_filter_scope")}</button>}</>}{c.permissions.has("system") && <><button type="button" className="text-button" onClick={() => { openMaintenance("killStuck"); }} disabled={maintenanceBusy}>{t("jobs.kill_stuck")}</button><button type="button" className="text-button" onClick={() => { openMaintenance("compact"); compactPreview.mutate(); }} disabled={maintenanceBusy}>{t("jobs.preview_compaction")}</button></>}</div></details>}
    {groupedRoots.length > 0 && <section className="task-group-disclosures" aria-label={t("gallery.pageTaskGroups")}><p>{t("gallery.pageTaskGroups")}</p>{groupedRoots.map(root => { const aggregate = aggregateTaskGroup(root); const descendants = flattenTaskNode(root).slice(1); return <details key={root.task.id}><summary>{root.task.title || root.task.id} · {t("jobs.group_children", { count: descendants.length })} · {t("jobs.group_summary", { active: aggregate.active, failed: aggregate.failed, complete: aggregate.complete, total: aggregate.total })}</summary><ul>{descendants.map(child => <li key={child.id}><a href={c.href({ task: child.id })} onClick={event => { if (event.button || event.ctrlKey || event.metaKey || event.altKey || event.shiftKey) return; event.preventDefault(); c.update({ task: child.id }, false, true); }}>{child.title || child.id}</a><span> · {t(`gallery.taskStatus_${taskDisplayStatus(child)}`)}</span></li>)}</ul></details>; })}</section>}
    {c.taskId && <TaskDetailDialog key={c.taskId} id={c.taskId} c={c} pendingAction={pending[c.taskId]} notice={notices[c.taskId]} error={errors[c.taskId]} onAction={handleAction} onConfirm={requestAction} />}
    {!c.taskId && c.permissions.has("tasks") && c.importJobId && <LegacyJobDrawer key={"import:"+c.importJobId} kind="import" id={c.importJobId} onClose={c.closeLegacyJob} />}
    {!c.taskId && c.permissions.has("tasks") && !c.importJobId && c.downloadJobId && <LegacyJobDrawer key={"download:"+c.downloadJobId} kind="download" id={c.downloadJobId} onClose={c.closeLegacyJob} onRepeatAccepted={id => c.update({ job: id }, false, true)} />}
    {confirm?.kind === "task" && <Modal title={actionLabel(t, confirm.action)} onClose={closeConfirm}><div className="confirm-message"><strong>{confirm.task.title || confirm.task.id}</strong><br />{confirm.action === "repeat_sync" ? t("jobs.repeat_sync_confirm") : confirm.action === "delete" ? t("jobs.delete_task_confirm") : t("common.confirm")}</div>{pending[confirm.task.id] && <p className="confirm-pending" role="status">{t("jobs.action_submitted_close")}</p>}{errors[confirm.task.id] && <p className="confirm-error" role="alert">{errors[confirm.task.id]}</p>}{conflicts[confirm.task.id] && <Link className="text-link confirm-pending" href={`/admin/jobs?tab=downloads&job=${encodeURIComponent(conflicts[confirm.task.id])}`}>{t("jobs.open_existing_download")}</Link>}<div className="confirm-actions"><button type="button" className="button" onClick={closeConfirm}>{pending[confirm.task.id] ? t("common.close") : t("common.cancel")}</button><button type="button" className="button primary" aria-disabled={pending[confirm.task.id] ? "true" : undefined} aria-busy={Boolean(pending[confirm.task.id]) || undefined} onClick={async () => { if (pendingRef.current[confirm.task.id]) return; const token = confirmToken.current; const ok = await runAction(confirm.task, confirm.action, confirm.detailOrigin, confirm.surface); if (ok && confirmToken.current === token) closeConfirm(); }}>{t("common.confirm")}</button></div></Modal>}
    {maintenanceAction && <Modal title={maintenanceTitle} onClose={() => { if (!maintenanceBusy) setMaintenanceAction(null); }}><p className="confirm-message">{maintenanceAction === "compact" && compactPreview.data ? t("jobs.compaction_confirm", { count: compactPreview.data.matched }) : maintenanceAction === "killStuck" ? t("jobs.kill_stuck_confirm") : t("common.confirm")}</p>{maintenanceAction === "filterRetry" && <div className="confirm-message"><strong>{t("jobs.current_filter_scope")}</strong><p>{t("gallery.allMatchingRecords")}</p><dl className="gallery-impact-counts">{Object.entries(maintenanceFiltersRef.current).map(([key, value]) => <div key={key}><dt>{t(key === "status" ? "gallery.taskStatusLabel" : key === "source" ? "gallery.source" : "jobs.subscription")}</dt><dd>{key === "status" ? statusLabel(t, value) : value}</dd></div>)}</dl></div>}{maintenanceResult && <><p role="status" className="operation-feedback">{t("jobs.batch_filter_result", { matched: String(maintenanceResult.matched ?? "—"), succeeded: String(maintenanceResult.succeeded ?? "—"), failed: String(maintenanceResult.failed ?? "—") })}{maintenanceResult.deleted != null ? ` · ${maintenanceResult.deleted}` : ""}</p>{maintenanceResult.errors?.length ? <ul className="batch-errors">{maintenanceResult.errors.map((item, index) => <li key={`${item.id || "error"}-${index}`}><span translate="no">{item.id || "—"}</span>：{actionErrorReason(item.error)}</li>)}</ul> : null}</>}{maintenanceError && <p role="alert" className="confirm-error">{actionErrorReason(maintenanceError)}</p>}<div className="confirm-actions"><button type="button" className="button" onClick={() => setMaintenanceAction(null)} disabled={maintenanceBusy}>{t("common.cancel")}</button><button type="button" className="button primary" onClick={runMaintenance} disabled={maintenanceBusy || (maintenanceAction === "compact" && (compactPreview.isPending || !compactPreview.data))} aria-busy={maintenanceBusy || undefined}>{t("common.confirm")}</button></div></Modal>}
    {confirm?.kind === "batch" && <Modal title={t("jobs.batch_action")} onClose={closeConfirm}><div className="confirm-message">{t("jobs.batch_confirm", { eligible: confirm.tasks.length, ineligible: confirm.ineligible.length })}</div>{batchPending && <p className="confirm-pending" role="status">{t("jobs.action_submitted_close")}</p>}<div className="confirm-actions"><button type="button" className="button" onClick={closeConfirm}>{batchPending ? t("common.close") : t("common.cancel")}</button><button type="button" className="button primary" aria-disabled={batchPending ? "true" : undefined} aria-busy={batchPending || undefined} onClick={() => { if (batchPending) return; void runBatch(confirm, confirmToken.current); }}>{copy("confirmApply")}</button></div></Modal>}
  </div>;
}
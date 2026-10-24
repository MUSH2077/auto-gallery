"use client";

import Link from "next/link";
import { useEffect, useRef, useState, type ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, queryKeys, type TaskRun } from "@/lib/api";
import { useT } from "@/lib/i18n";
import { hasTaskAction, actionErrorReason, partitionTaskAction, reconcileTaskBulkResult, repeatSyncConflict, type TaskAction } from "@/lib/task-actions";
import { useTaskCommands } from "@/lib/useTaskCommand";
import { POLL_ACTIVE_MS } from "@/lib/polling";
import { taskDisplayStatus, taskCreator, taskReason, taskProgress, terminalTaskStatuses } from "@/lib/taskPresentation";
import ErrorState from "@/components/ErrorState";
import { ToolbarSelect, SearchField } from "@/features/gallery-ui/Controls";
import { JobsView, TaskTable, type ScopeLink } from "@/features/gallery-ui/CollectionViews";
import { Modal } from "@/features/gallery-ui/Modal";
import { TaskDetailView } from "@/features/gallery-ui/DetailViews";
import { useGalleryCopy } from "@/features/gallery-ui/copy";
import { TaskStatus } from "@/features/gallery-ui/TaskPrimitives";
import { useTaskList, type TaskListController } from "./useTaskList";
import { useGalleryFormat } from "@/features/gallery-ui/format";

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

type ConfirmState =
  | { kind: "task"; task: TaskRun; action: TaskAction; detailOrigin: boolean }
  | { kind: "batch"; tasks: TaskRun[]; action: TaskAction; ineligible: Array<{ row: TaskRun; reason: string }> };

type ActionProps = {
  task: TaskRun;
  pendingAction?: TaskAction;
  notice?: string;
  error?: string;
  onAction: (action: TaskAction) => void;
  onConfirm: (action: TaskAction) => void;
};

function RActionButtons({ task, pendingAction, notice, error, onAction, onConfirm }: ActionProps) {
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
        if (["delete", "repeat_sync", "cancel"].includes(action)) onConfirm(action); else onAction(action);
      }}>{actionLabel(t, action)}</button>;
    })}
    {repeatUnavailable && <span className="operation-feedback muted">{t("jobs.repeat_original_unavailable")}</span>}
    {notice && <p role="status" className="operation-feedback task-operation-feedback">{notice}</p>}
    {error && <p role="alert" className="operation-feedback is-error task-operation-feedback">{error}</p>}
  </>;
}

function RTaskDetail({ id, c, pendingAction, notice, error, onAction, onConfirm }: { id: string; c: TaskListController; pendingAction?: TaskAction; notice?: string; error?: string; onAction: (task: TaskRun, action: TaskAction) => void; onConfirm: (task: TaskRun, action: TaskAction) => void }) {
  const t = c.t;
  const fmt = useGalleryFormat();
  const query = useQuery({ queryKey: queryKeys.tasks.detail(id), queryFn: () => api.getTask(id), refetchInterval: q => q.state.data && !terminalTaskStatuses.has(q.state.data.status) ? POLL_ACTIVE_MS : false, refetchIntervalInBackground: false });
  const task = query.data;
  if (query.isPending) return <Modal title={t("jobs.task_detail")} onClose={c.closeTask} sheet><div className="state-view" role="status">{t("common.loading")}</div></Modal>;
  if (query.isError) return <Modal title={t("jobs.task_detail")} onClose={c.closeTask} sheet><ErrorState error={query.error} onRetry={() => void query.refetch()} /></Modal>;
  if (!task) return null;
  const status = taskDisplayStatus(task);
  const progress = taskProgress(task);
  const reason = taskReason(t, task);
  const facts = [
    { label: t("gallery.taskKindLabel"), value: t(taskKindKey(task)) },
    { label: t("gallery.creator"), value: taskCreator(task) },
    { label: t("gallery.completedAssets"), value: `${progress.current} / ${progress.total}` },
    { label: t("gallery.created"), value: fmt.date(task.created_at) },
  ];
  return <Modal title={t("jobs.task_detail")} onClose={c.closeTask} sheet><TaskDetailView id={task.id} title={task.title || task.operation_type || task.id} status={<TaskStatus value={status} label={t(`gallery.taskStatus_${status}`)} />} facts={facts} reason={reason || undefined} actions={<div className="task-actions"><RActionButtons task={task} pendingAction={pendingAction} notice={notice} error={error} onAction={action => onAction(task, action)} onConfirm={action => onConfirm(task, action)} /></div>}><details><summary>{t("gallery.techDetails")}</summary><pre translate="no">{JSON.stringify(task, null, 2)}</pre></details>{!task.available_actions?.length && task.disabled_reasons && <p className="operation-feedback muted">{Object.values(task.disabled_reasons).filter(Boolean).join("；") || t("gallery.noActions")}</p>}</TaskDetailView></Modal>;
}

export default function JobsPage() {
  const { copy } = useGalleryCopy();
  const c = useTaskList();
  const t = c.t;
  const fmt = useGalleryFormat();
  const summary = c.query.data?.summary;
  const commands = useTaskCommands(c.permissions.user?.id);
  const pendingRef = useRef<Record<string, TaskAction>>({});
  const confirmToken = useRef(0);
  const scopeRef = useRef(`${c.q}|${c.kind}|${c.status}|${c.scope}|${c.page}`);
  const [selectionMode, setSelectionMode] = useState(false);
  const [pending, setPending] = useState<Record<string, TaskAction>>({});
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [conflicts, setConflicts] = useState<Record<string, string>>({});
  const [notices, setNotices] = useState<Record<string, string>>({});
  const [confirm, setConfirm] = useState<ConfirmState | null>(null);
  const [batchAction, setBatchAction] = useState<TaskAction | "">("retry");
  const [batchPending, setBatchPending] = useState(false);
  const [batchOutcome, setBatchOutcome] = useState<Array<{ id: string; reason: string }> | null>(null);

  useEffect(() => {
    scopeRef.current = `${c.q}|${c.kind}|${c.status}|${c.scope}|${c.page}`;
    setNotices({});
    setBatchOutcome(null);
  }, [c.taskId, c.q, c.kind, c.status, c.scope, c.page]);

  function setTaskPending(id: string, action: TaskAction | null) {
    const next = { ...pendingRef.current };
    if (action) next[id] = action; else delete next[id];
    pendingRef.current = next;
    setPending(next);
  }

  async function runAction(task: TaskRun, action: TaskAction, detailOrigin = false): Promise<boolean> {
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
      const result = action === "repeat_sync" ? await commands.repeat(task) : await commands.execute(task, action);
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

  function handleAction(task: TaskRun, action: TaskAction) { void runAction(task, action, c.taskId === task.id); }
  function closeConfirm() { confirmToken.current += 1; setConfirm(null); }
  function requestAction(task: TaskRun, action: TaskAction) {
    if (pendingRef.current[task.id]) return;
    if (["delete", "repeat_sync", "cancel"].includes(action)) {
      confirmToken.current += 1;
      setConfirm({ kind: "task", task, action, detailOrigin: c.taskId === task.id });
    } else handleAction(task, action);
  }

  async function runBatch(state: Extract<ConfirmState, { kind: "batch" }>, token: number) {
    if (batchPending || state.tasks.some(task => pendingRef.current[task.id])) return;
    const scopeAtSubmit = scopeRef.current;
    setBatchPending(true);
    setErrors({});
    state.tasks.forEach(task => setTaskPending(task.id, state.action));
    const submission = { selectedIds: [...state.tasks.map(task => task.id), ...state.ineligible.map(item => item.row.id)], eligibleIds: state.tasks.map(task => task.id), localRefusals: state.ineligible.map(item => ({ id: item.row.id, reason: item.reason })) };
    try {
      const response = await commands.batch(state.tasks, state.action);
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
    setConfirm({ kind: "batch", tasks: preview.eligible, action, ineligible });
  }

  function intercept(event: React.MouseEvent<HTMLAnchorElement>, scope: string) {
    if (event.button || event.ctrlKey || event.metaKey || event.altKey || event.shiftKey) return;
    event.preventDefault();
    c.update({ scope: scope === "all" ? null : scope, status: null });
  }

  const scopes: ScopeLink[] = [["all", "gallery.allTasks"], ["attention", "gallery.attention"], ["running", "gallery.running"], ["waiting", "gallery.waiting"]].map(([value, key]) => ({
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
        <ToolbarSelect label={t("gallery.taskKindLabel")} value={c.kind} onValueChange={kind => c.update({ kind: kind || null })} options={[{ value: "", label: t("gallery.allKinds") }, ...["download", "import", "admin"].map(value => ({ value, label: t(`gallery.taskKind_${value}`) }))]} />
        <ToolbarSelect label={t("gallery.taskStatusLabel")} value={c.scope === "all" ? c.status : c.scope} onValueChange={value => { const scoped = ["attention", "running", "waiting"].includes(value); c.update({ scope: scoped ? value : null, status: scoped ? null : value || null }); }} options={[{ value: "", label: t("gallery.allStates") }, { value: "attention", label: t("gallery.attention") }, ...["running", "waiting", "enqueued", "recovering", "paused", "failed", "stale", "complete", "cancelled"].map(value => ({ value, label: t(`gallery.taskStatus_${value}`) }))]} />
      </div>
      {available && <div className="layout-toolbar-utilities"><button type="button" className="layout-tool-trigger layout-selection-mode" aria-label={selecting ? copy("selectionDone") : copy("selectTasks")} aria-pressed={selecting} disabled={c.busy || c.isComposing} onClick={() => { setSelectionMode(value => !value); c.setSelected(new Set()); }}>{selecting ? copy("completed") : copy("choose")}</button></div>}
    </div>
  </>;

  const selection = selecting && available ? <div className={`layout-selection-bar${c.selected.size ? " has-selection" : ""}`}>
    <button type="button" className="text-button" disabled={c.busy || c.isComposing} onClick={c.togglePage}>{c.allSelected ? t("gallery.clearTaskSelection") : t("gallery.selectTasks")}</button>
    {c.selected.size ? <><span>{t("common.selected_count", { count: c.selected.size })}</span><ToolbarSelect label={t("jobs.batch_action")} value={batchAction} onValueChange={value => setBatchAction(value as TaskAction | "")} options={BATCH_ACTIONS.map(action => ({ value: action, label: actionLabel(t, action) }))} /><button type="button" className="button primary" disabled={!batchAction || c.busy || c.isComposing} aria-disabled={batchPending ? "true" : undefined} aria-busy={batchPending || undefined} onClick={() => { if (batchPending) return; prepareBatch(); }}>{copy("applyBatch")}</button><button type="button" className="text-button" onClick={() => c.setSelected(new Set())}>{t("gallery.clearTaskSelection")}</button></> : <span>{copy("selectionHint")}</span>}
  </div> : undefined;

  const rows = c.rows.map(task => {
    const status = taskDisplayStatus(task);
    const progress = taskProgress(task);
    const reason = taskReason(t, task);
    return { id: task.id, title: task.title || task.operation_type || task.id, href: c.href({ task: task.id }), onOpen: (event: React.MouseEvent<HTMLAnchorElement>) => { if (event.button || event.ctrlKey || event.metaKey || event.altKey || event.shiftKey) return; event.preventDefault(); c.update({ task: task.id }, false, true); }, kind: t(taskKindKey(task)), creator: taskCreator(task), dateTime: task.created_at || "", dateLabel: fmt.date(task.created_at), reason: reason || undefined, danger: ["failed", "stale"].includes(status), selected: c.selected.has(task.id), onSelect: () => { if (!c.busy && !c.isComposing) c.toggle(task.id); }, status: <TaskStatus value={status} label={t(`gallery.taskStatus_${status}`)} />, statusLabel: t(`gallery.taskStatus_${status}`), progress: ["running", "recovering", "complete"].includes(status) ? { value: progress.percent, resources: t("gallery.progressCount", { done: progress.current, total: progress.total }) } : undefined, emptyProgress: status === "waiting" ? t("gallery.waitExecution") : status === "paused" ? t("gallery.keepProgress") : "—", actions: <div className="task-actions"><RActionButtons task={task} pendingAction={pending[task.id]} notice={notices[task.id]} error={errors[task.id]} onAction={action => handleAction(task, action)} onConfirm={action => requestAction(task, action)} /></div> };
  });

  return <div className="gallery-app">
    {!c.allowed ? <div className="state-view" role="alert"><h1>{t("gallery.noTaskPermission")}</h1></div> : <JobsView scopes={scopes} toolbar={toolbar} selection={selection} failures={batchOutcome?.length ? <section className="batch-errors" role="alert"><h2>{t("jobs.batch_partial")}</h2><ul>{batchOutcome.map(item => <li key={item.id}><span translate="no">{item.id.slice(0, 8)}</span>：{item.reason}</li>)}</ul></section> : undefined}>
      {c.query.isPending ? <div className="state-view" role="status">{t("common.loading")}</div> : c.query.isError ? <ErrorState error={c.query.error} onRetry={() => void c.query.refetch()} /> : c.rows.length ? <TaskTable selecting={selecting} selectionDisabled={c.busy || c.isComposing} rows={rows} /> : <div className="state-view"><h2>{t("gallery.emptyTasks")}</h2><button className="button" onClick={() => c.update({ q: null, kind: null, status: null, scope: null })}>{t("gallery.clearFilters")}</button></div>}
      {c.total > 12 && <nav className="pagination" aria-label={t("gallery.taskPagination")}><button className="button" disabled={c.page === 0 || c.busy} onClick={() => c.update({ p: String(c.page - 1) }, false, true)}>{t("works.prev")}</button><span>{t("gallery.pageCount", { page: c.page + 1, total: c.pages })}</span><button className="button" disabled={c.page + 1 >= c.pages || c.busy} onClick={() => c.update({ p: String(c.page + 1) }, false, true)}>{t("works.next")}</button></nav>}
    </JobsView>}
    {c.taskId && <RTaskDetail key={c.taskId} id={c.taskId} c={c} pendingAction={pending[c.taskId]} notice={notices[c.taskId]} error={errors[c.taskId]} onAction={handleAction} onConfirm={requestAction} />}
    {confirm?.kind === "task" && <Modal title={actionLabel(t, confirm.action)} onClose={closeConfirm}><div className="confirm-message"><strong>{confirm.task.title || confirm.task.id}</strong><br />{confirm.action === "repeat_sync" ? t("jobs.repeat_sync_confirm") : confirm.action === "delete" ? t("jobs.delete_task_confirm") : t("common.confirm")}</div>{pending[confirm.task.id] && <p className="confirm-pending" role="status">{t("jobs.action_submitted_close")}</p>}{errors[confirm.task.id] && <p className="confirm-error" role="alert">{errors[confirm.task.id]}</p>}{conflicts[confirm.task.id] && <Link className="text-link confirm-pending" href={`/admin/jobs?tab=downloads&job=${encodeURIComponent(conflicts[confirm.task.id])}`}>{t("jobs.open_existing_download")}</Link>}<div className="confirm-actions"><button type="button" className="button" onClick={closeConfirm}>{pending[confirm.task.id] ? t("common.close") : t("common.cancel")}</button><button type="button" className="button primary" aria-disabled={pending[confirm.task.id] ? "true" : undefined} aria-busy={Boolean(pending[confirm.task.id]) || undefined} onClick={async () => { if (pendingRef.current[confirm.task.id]) return; const token = confirmToken.current; const ok = await runAction(confirm.task, confirm.action, confirm.detailOrigin); if (ok && confirmToken.current === token) closeConfirm(); }}>{t("common.confirm")}</button></div></Modal>}
    {confirm?.kind === "batch" && <Modal title={t("jobs.batch_action")} onClose={closeConfirm}><div className="confirm-message">{t("jobs.batch_confirm", { eligible: confirm.tasks.length, ineligible: confirm.ineligible.length })}</div>{batchPending && <p className="confirm-pending" role="status">{t("jobs.action_submitted_close")}</p>}<div className="confirm-actions"><button type="button" className="button" onClick={closeConfirm}>{batchPending ? t("common.close") : t("common.cancel")}</button><button type="button" className="button primary" aria-disabled={batchPending ? "true" : undefined} aria-busy={batchPending || undefined} onClick={() => { if (batchPending) return; void runBatch(confirm, confirmToken.current); }}>{copy("confirmApply")}</button></div></Modal>}
  </div>;
}
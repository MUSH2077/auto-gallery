"use client";

import dynamic from "next/dynamic";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import {
  useDeferredValue,
  useEffect,
  useMemo,
  useState,
  useTransition,
} from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { ErrorState, PageHeader, PageShell, SourceBadge, StatusBadge } from "@/components";
import DomainDangerZone from "@/components/DomainDangerZone";
import { useToast } from "@/components/Toast";
import {
  api,
  queryKeys,
  type OperationAttentionItem,
  type OperationsView,
} from "@/lib/api";
import { adminRoutes } from "@/lib/adminRoutes";
import { useT } from "@/lib/i18n";
import { useI18nFormat } from "@/lib/i18n-format";
import { POLL_ACTIVE_MS, POLL_IDLE_MS } from "@/lib/polling";

const TaskDetailDrawer = dynamic(
  () => import("@/components/JobDrawers").then((module) => module.TaskDetailDrawer),
  { ssr: false },
);

const VIEWS: OperationsView[] = ["attention", "active", "resolved"];
type SortMode = "severity" | "newest" | "oldest";

function usePageVisibility() {
  const [visible, setVisible] = useState(true);
  useEffect(() => {
    const update = () => setVisible(document.visibilityState === "visible");
    update();
    document.addEventListener("visibilitychange", update);
    return () => document.removeEventListener("visibilitychange", update);
  }, []);
  return visible;
}

function reasonLabel(t: ReturnType<typeof useT>, code?: string | null) {
  if (!code) return t("operations.reason.unknown");
  const key = `operations.reason.${code}`;
  const translated = t(key);
  return translated === key ? code.replaceAll("_", " ") : translated;
}

function groupItems(items: OperationAttentionItem[]) {
  const groups = new Map<string, OperationAttentionItem[]>();
  for (const item of items) {
    const key = [item.repository_id || item.task?.subject_id || item.id, item.reason_code || item.title].join(":");
    groups.set(key, [...(groups.get(key) || []), item]);
  }
  return [...groups.values()];
}

function severityWeight(item: OperationAttentionItem) {
  return item.severity === "critical" ? 2 : item.severity === "warning" ? 1 : 0;
}

export default function OperationsAttentionCenter() {
  const t = useT();
  const fmt = useI18nFormat();
  const toast = useToast();
  const router = useRouter();
  const params = useSearchParams();
  const qc = useQueryClient();
  const visible = usePageVisibility();
  const rawView = params.get("view") as OperationsView | null;
  const view = rawView && VIEWS.includes(rawView) ? rawView : "attention";
  const rawSort = params.get("sort") as SortMode | null;
  const sort: SortMode = rawSort && ["severity", "newest", "oldest"].includes(rawSort) ? rawSort : "severity";
  const [search, setSearch] = useState(params.get("q") || "");
  const deferredSearch = useDeferredValue(search.trim().toLocaleLowerCase());
  const [, startTransition] = useTransition();
  const [selectedTaskId, setSelectedTaskId] = useState<string | null>(params.get("task"));
  const [batchMode, setBatchMode] = useState(false);
  const [selectedTaskIds, setSelectedTaskIds] = useState<Set<string>>(new Set());

  useEffect(() => {
    setSearch(params.get("q") || "");
  }, [params]);
  useEffect(() => {
    setSelectedTaskIds(new Set());
  }, [view]);

  const updateUrl = (next: { view?: OperationsView; q?: string; sort?: SortMode; task?: string | null }) => {
    const query = new URLSearchParams(params.toString());
    if (next.view) query.set("view", next.view);
    if (next.sort) query.set("sort", next.sort);
    if (next.q !== undefined) next.q ? query.set("q", next.q) : query.delete("q");
    if (next.task !== undefined) next.task ? query.set("task", next.task) : query.delete("task");
    startTransition(() => router.replace(`${adminRoutes.jobs}?${query.toString()}`, { scroll: false }));
  };

  const overview = useQuery({
    queryKey: queryKeys.tasks.operations(view),
    queryFn: ({ signal }) => api.operationsOverview(view, signal),
    placeholderData: (previous) => previous,
    staleTime: view === "active" ? 5_000 : 15_000,
    refetchInterval: (query) => {
      if (!visible) return false;
      return (query.state.data?.summary.active || 0) > 0 ? POLL_ACTIVE_MS : POLL_IDLE_MS;
    },
    refetchIntervalInBackground: false,
  });

  const invalidate = async () => {
    await qc.invalidateQueries({ queryKey: queryKeys.tasks.all });
  };
  const action = useMutation({
    mutationFn: async ({ item, name }: { item: OperationAttentionItem; name: "retry" | "pause" | "resume" | "acknowledge" }) => {
      if (!item.task_id) throw new Error(t("operations.no_task_action"));
      if (name === "retry") return api.retryTask(item.task_id);
      if (name === "pause") return api.pauseTask(item.task_id);
      if (name === "resume") return api.resumeTask(item.task_id);
      return api.acknowledgeTask(item.task_id);
    },
    onSuccess: async () => {
      toast.success(t("operations.action_complete"));
      await invalidate();
    },
    onError: (error: Error) => toast.error(error.message),
  });
  const batchAction = useMutation({
    mutationFn: async (name: "retry" | "acknowledge") => {
      const selected = (overview.data?.items || []).filter((item) => item.task_id && selectedTaskIds.has(item.task_id));
      const eligible = selected.filter((item) => (
        name === "retry"
          ? item.available_actions.includes("retry")
          : item.status === "open"
      ));
      for (const item of eligible) {
        if (!item.task_id) continue;
        if (name === "retry") await api.retryTask(item.task_id);
        else await api.acknowledgeTask(item.task_id);
      }
      return eligible.length;
    },
    onSuccess: async (count) => {
      setSelectedTaskIds(new Set());
      toast.success(t("operations.batch_complete", { count }));
      await invalidate();
    },
    onError: (error: Error) => toast.error(error.message),
  });

  const filteredGroups = useMemo(() => {
    let items = [...(overview.data?.items || [])];
    if (deferredSearch) {
      items = items.filter((item) => [
        item.title,
        item.summary,
        item.source,
        item.reason_code,
        item.repository_id,
      ].some((value) => value?.toLocaleLowerCase().includes(deferredSearch)));
    }
    items.sort((left, right) => {
      if (sort === "severity") {
        const severity = severityWeight(right) - severityWeight(left);
        if (severity) return severity;
      }
      const time = new Date(right.occurred_at).getTime() - new Date(left.occurred_at).getTime();
      return sort === "oldest" ? -time : time;
    });
    return groupItems(items);
  }, [deferredSearch, overview.data?.items, sort]);

  const openTask = (id: string) => {
    setSelectedTaskId(id);
    updateUrl({ task: id });
  };
  const closeTask = () => {
    setSelectedTaskId(null);
    updateUrl({ task: null });
  };
  const selectedItem = overview.data?.items.find((item) => item.task_id === selectedTaskId);
  const openSelectedRepository = () => {
    if (selectedItem?.repository_id) router.push(adminRoutes.repository(selectedItem.repository_id));
  };

  return (
    <PageShell>
      <PageHeader
        title={t("operations.title")}
        description={t("operations.desc")}
        primaryAction={(
          <div className="flex flex-wrap gap-2">
            <button
              type="button"
              className={batchMode ? "btn-primary min-h-11 px-4" : "btn-ghost min-h-11 px-4"}
              aria-pressed={batchMode}
              onClick={() => {
                setBatchMode((current) => !current);
                setSelectedTaskIds(new Set());
              }}
            >
              {batchMode ? t("operations.batch_done") : t("operations.batch_mode")}
            </button>
            <button type="button" className="btn-ghost min-h-11 px-4" onClick={() => overview.refetch()} disabled={overview.isFetching}>
              {overview.isFetching ? t("common.loading") : t("operations.refresh")}
            </button>
          </div>
        )}
      />

      {overview.data && (
        <div className="mb-4 flex flex-wrap items-center gap-x-5 gap-y-2 rounded-md border border-border bg-surface px-4 py-3 text-sm" aria-live="polite">
          <span><strong className="text-danger">{overview.data.summary.critical}</strong> {t("operations.summary.critical")}</span>
          <span><strong className="text-warning">{overview.data.summary.warning}</strong> {t("operations.summary.warning")}</span>
          <span><strong className="text-accent">{overview.data.summary.active}</strong> {t("operations.summary.active")}</span>
          <span><strong>{overview.data.summary.resource_limited}</strong> {t("operations.summary.resource")}</span>
        </div>
      )}

      <div className="sticky top-0 z-20 mb-4 rounded-md border border-border bg-surface/95 p-2 shadow-sm backdrop-blur supports-[backdrop-filter]:bg-surface/85">
        <div className="flex flex-col gap-2 md:flex-row md:items-center md:justify-between">
          <div className="flex overflow-x-auto" role="tablist" aria-label={t("operations.views") }>
            {VIEWS.map((candidate) => (
              <button
                key={candidate}
                type="button"
                role="tab"
                aria-selected={view === candidate}
                onClick={() => updateUrl({ view: candidate })}
                className={`min-h-11 whitespace-nowrap rounded-md px-4 text-sm font-medium ${view === candidate ? "bg-primary text-on-primary" : "text-muted hover:bg-subtle hover:text-fg"}`}
              >
                {t(`operations.view.${candidate}`)}
                {candidate === "attention" && overview.data ? ` ${overview.data.summary.attention}` : ""}
                {candidate === "active" && overview.data ? ` ${overview.data.summary.active}` : ""}
              </button>
            ))}
          </div>
          <div className="flex min-w-0 flex-1 flex-col gap-2 sm:flex-row md:max-w-xl">
            <label className="sr-only" htmlFor="operations-search">{t("operations.search")}</label>
            <input
              id="operations-search"
              value={search}
              onChange={(event) => {
                const value = event.target.value;
                setSearch(value);
                updateUrl({ q: value });
              }}
              placeholder={t("operations.search_placeholder")}
              className="min-h-11 min-w-0 flex-1 rounded-md border border-border bg-surface px-3 text-sm"
            />
            <label className="sr-only" htmlFor="operations-sort">{t("operations.sort")}</label>
            <select
              id="operations-sort"
              value={sort}
              onChange={(event) => updateUrl({ sort: event.target.value as SortMode })}
              className="min-h-11 rounded-md border border-border bg-surface px-3 text-sm"
            >
              <option value="severity">{t("operations.sort.severity")}</option>
              <option value="newest">{t("operations.sort.newest")}</option>
              <option value="oldest">{t("operations.sort.oldest")}</option>
            </select>
          </div>
        </div>
      </div>

      {batchMode && (
        <div className="mb-4 flex min-h-11 flex-wrap items-center gap-2 rounded-md border border-accent/30 bg-accent-subtle px-3 py-2" aria-live="polite">
          <span className="mr-auto text-sm font-medium">{t("operations.batch_selected", { count: selectedTaskIds.size })}</span>
          <button
            type="button"
            className="btn-ghost min-h-11 px-3"
            disabled={!selectedTaskIds.size || batchAction.isPending}
            onClick={() => batchAction.mutate("retry")}
          >
            {t("operations.batch_retry")}
          </button>
          <button
            type="button"
            className="btn-primary min-h-11 px-3"
            disabled={!selectedTaskIds.size || batchAction.isPending}
            onClick={() => batchAction.mutate("acknowledge")}
          >
            {t("operations.batch_acknowledge")}
          </button>
        </div>
      )}

      {overview.isLoading && (
        <div className="space-y-2" aria-hidden="true">
          {Array.from({ length: 4 }).map((_, index) => <div key={index} className="h-28 animate-pulse rounded-md bg-subtle" />)}
        </div>
      )}
      {overview.error && <ErrorState message={(overview.error as Error).message} onRetry={() => overview.refetch()} />}

      {!overview.isLoading && !overview.error && filteredGroups.length === 0 && (
        <section className="rounded-md border border-success/30 bg-success-subtle px-5 py-12 text-center" aria-live="polite">
          <div className="text-lg font-semibold text-success">{view === "attention" ? t("operations.healthy") : t("operations.empty")}</div>
          <p className="mx-auto mt-2 max-w-xl text-sm text-muted">{view === "attention" ? t("operations.healthy_desc") : t("operations.empty_desc")}</p>
        </section>
      )}

      <div className="space-y-2" aria-live="polite" aria-busy={overview.isFetching}>
        {filteredGroups.map((group) => {
          const item = group[0];
          const task = item.task;
          const retryable = item.available_actions.includes("retry");
          const pausable = !!task && ["enqueued", "running", "recovering"].includes(task.status);
          const groupTaskIds = group.flatMap((candidate) => candidate.task_id ? [candidate.task_id] : []);
          return (
            <article key={`${item.repository_id || item.id}:${item.reason_code || item.title}`} className={`rounded-md border bg-surface p-4 ${item.severity === "critical" ? "border-danger/40" : "border-border"}`}>
              <div className="flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
                {batchMode && groupTaskIds.length > 0 && (
                  <label className="flex min-h-11 min-w-11 shrink-0 items-center justify-center self-start rounded-md border border-border bg-surface" aria-label={t("operations.batch_select_group", { count: group.length })}>
                    <input
                      type="checkbox"
                      checked={groupTaskIds.every((taskId) => selectedTaskIds.has(taskId))}
                      onChange={(event) => {
                        const checked = event.target.checked;
                        setSelectedTaskIds((current) => {
                          const next = new Set(current);
                          for (const taskId of groupTaskIds) {
                            if (checked) next.add(taskId);
                            else next.delete(taskId);
                          }
                          return next;
                        });
                      }}
                      className="h-5 w-5 accent-accent"
                    />
                  </label>
                )}
                <button type="button" onClick={() => item.task_id && openTask(item.task_id)} disabled={!item.task_id} className="min-w-0 flex-1 text-left focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent disabled:cursor-default">
                  <div className="flex flex-wrap items-center gap-2">
                    {item.source && <SourceBadge source={item.source} />}
                    <StatusBadge status={view === "active" && task ? task.status : item.severity} label={view === "active" ? undefined : reasonLabel(t, item.reason_code)} />
                    {group.length > 1 && <span className="rounded-full bg-subtle px-2 py-0.5 text-xs font-medium">{t("operations.grouped", { count: group.length })}</span>}
                  </div>
                  <h2 className="mt-2 font-semibold text-fg">{item.title}</h2>
                  <p className="mt-1 line-clamp-2 text-sm text-muted">{item.summary || reasonLabel(t, item.reason_code)}</p>
                  <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted">
                    <span>{fmt.relative(item.occurred_at)}</span>
                    {item.repository_id && <span>{t("operations.repository_affected")}</span>}
                    {task?.resource_reason && <span>{t("operations.resource_limited")}</span>}
                  </div>
                </button>
                <div className="flex flex-wrap items-center gap-2 sm:max-w-[48%] sm:justify-end">
                  {retryable && <button type="button" className="btn-primary min-h-11 px-4" onClick={() => action.mutate({ item, name: "retry" })}>{t("jobs.retry")}</button>}
                  {pausable && task?.status !== "paused" && <button type="button" className="btn-ghost min-h-11 px-3" onClick={() => action.mutate({ item, name: "pause" })}>{t("jobs.pause")}</button>}
                  {task?.status === "paused" && <button type="button" className="btn-ghost min-h-11 px-3" onClick={() => action.mutate({ item, name: "resume" })}>{t("jobs.resume")}</button>}
                  {item.status === "open" && item.task_id && <button type="button" className="btn-ghost min-h-11 px-3" onClick={() => action.mutate({ item, name: "acknowledge" })}>{t("operations.acknowledge")}</button>}
                  {item.repository_id && <Link href={adminRoutes.repository(item.repository_id)} className="btn-ghost min-h-11 px-3">{t("operations.open_repository")}</Link>}
                  <button
                    type="button"
                    className="btn-ghost min-h-11 px-3"
                    onClick={async () => {
                      await navigator.clipboard.writeText(JSON.stringify(item, null, 2));
                      toast.success(t("operations.diagnostics_copied"));
                    }}
                  >
                    {t("operations.copy_diagnostics")}
                  </button>
                </div>
              </div>
            </article>
          );
        })}
      </div>

      <TaskDetailDrawer
        id={selectedTaskId}
        onClose={closeTask}
        onRetryTask={(id) => {
          const item = overview.data?.items.find((candidate) => candidate.task_id === id);
          if (item) action.mutate({ item, name: "retry" });
        }}
        onOpenDownload={openSelectedRepository}
        onOpenImport={openSelectedRepository}
      />
      <DomainDangerZone
        entity="jobs"
        title={t("datamgmt.danger_clear_jobs")}
        description={t("datamgmt.danger_clear_jobs_desc")}
      />
    </PageShell>
  );
}

"use client";
import { useEffect, useRef, useState } from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import { api, type DownloadJob, type ImportJob, type TaskRun, type TaskRunListResponse } from "@/lib/api";
import { usePermissions } from "@/lib/usePermissions";
import { useT } from "@/lib/i18n";
import { POLL_ACTIVE_MS } from "@/lib/polling";
import { terminalTaskStatuses } from "@/lib/taskPresentation";
import { parseTaskListRoute, taskListKey, TASK_PAGE_SIZE } from "./taskListQuery";
import { updatedTaskListHref } from "./taskListRoute";
import { hasDetailListOrigin, markDetailListEntry } from "./detailHistory";


function legacyTaskStatus(status: string) {
    if (["pending", "queued"].includes(status)) return "enqueued";
    if (["downloading", "downloaded", "importing"].includes(status)) return "running";
    return status;
}

type TaskListResponseWithPaging = TaskRunListResponse & { totalKnown?: boolean; hasNext?: boolean };

type LegacyTaskInput =
    | [job: DownloadJob, kind: "download"]
    | [job: ImportJob, kind: "import"];

function legacyTask(...args: LegacyTaskInput): TaskRun {
    const [job, kind] = args;
    const progress = job.progress_data ? { ...job.progress_data } : null;
    const attempts = kind === "download" ? job.retry_count : job.import_retry_count;
    return {
        ...job,
        id: job.id,
        kind,
        title: job.source_url || job.id,
        operation_type: `${kind}-job`,
        status: legacyTaskStatus(job.status),
        subject_type: `${kind}_job`,
        subject_id: job.id,
        attempts: attempts ?? 0,
        priority: 10,
        error_log: job.error_log,
        available_actions: job.available_actions,
        disabled_reasons: job.disabled_reasons,
        created_at: job.created_at,
        source: job.source,
        source_url: job.source_url,
        progress_data: progress,
        progress_current: progress?.current,
        progress_total: progress?.total,
        meta: { creator_name: job.creator_name, record_status: job.status },
    };
}

export function useTaskList() {
    const t = useT(), permissions = usePermissions(), sp = useSearchParams(), pathname = usePathname(), router = useRouter();
    const allowed = permissions.has('tasks') || permissions.has('system'), route = parseTaskListRoute(new URLSearchParams(sp.toString())), { q, kind, records, status, scope, page, source, sort, order } = route;
    const recordKind = records || undefined;
    const relatedRecord = Boolean((recordKind === "import" && route.downloadJobFilterId) || (recordKind === "download" && route.subscriptionSourceId));
    const recordScopeUnsupported = Boolean(recordKind && scope !== "all");
    const params = useRef(sp.toString()), detailEntry = useRef(false), assistGeneration = useRef(0);
    useEffect(() => { params.current = sp.toString(); if (detailEntry.current && sp.get("task")) markDetailListEntry("task"); if (!sp.get("task")) detailEntry.current=false; }, [sp]);
    const [input, setInput] = useState(q), [selected, setSelected] = useState<Set<string>>(new Set()), [isComposing, setIsComposing] = useState(false);
    function href(patch: Record<string, string | null>, resetPage = false) { return updatedTaskListHref(pathname, new URLSearchParams(params.current), patch, resetPage); }
    function update(patch: Record<string, string | null>, resetPage = true, push = false) { const normalized = { ...patch }; if (normalized.task) { normalized.job = null; normalized.import_job = null; } if (normalized.job || normalized.import_job) normalized.task = null; const url = href(normalized, resetPage); params.current = url.split('?')[1] || ''; if (push && normalized.task)
        detailEntry.current = true;
    if (push)
        router.push(url, { scroll: false });
    else
        router.replace(url, { scroll: false }); }
    function closeTask() {
        if (detailEntry.current || hasDetailListOrigin("task")) {
            router.back();
            return;
        }
        update({ task: null }, false);
    }
    function closeLegacyJob() { update({ job: null, import_job: null }, false); }
    useEffect(() => setInput(q), [q]);
    useEffect(() => {
        if (records || !q.trim()) return;
        const generation = ++assistGeneration.current;
        const sourceQuery = q;
        void api.assistSearch({ before_cursor: sourceQuery, scope: "tasks" }).then(result => {
            if (generation !== assistGeneration.current || sourceQuery !== q) return;
            const canonical = result.canonical_query || result.parsed?.canonical;
            if (canonical && canonical !== sourceQuery && new URLSearchParams(params.current).get("q") === sourceQuery) update({ q: canonical }, false);
        }).catch(() => { /* The list API remains authoritative when assist is unavailable. */ });
        return () => { assistGeneration.current += 1; };
    }, [q, records]);
    useEffect(() => { if (isComposing || input === q)
        return; const timer = setTimeout(() => update({ q: input }), 300); return () => clearTimeout(timer); }, [input, q, isComposing, pathname, router]);
    useEffect(() => setSelected(new Set()), [q, kind, records, status, source, sort, order, scope, page, route.downloadJobFilterId, route.subscriptionSourceId]);
    const queryKey = [...taskListKey(q, kind, status, scope, page), records, source, sort, order, route.downloadJobFilterId, route.subscriptionSourceId];

    async function fetchTaskPage(): Promise<TaskListResponseWithPaging> {
        const offset = page * TASK_PAGE_SIZE;
        const domainQ = route.search || undefined;

        if (recordKind === "import") {
            const result = await api.listImportJobs({
                download_job_id: route.downloadJobFilterId || undefined,
                status: status || undefined,
                q: domainQ,
                visibility: "all",
                offset,
                limit: TASK_PAGE_SIZE,
            });
            return {
                total: result.total,
                totalKnown: true,
                hasNext: offset + result.items.length < result.total,
                items: result.items.map(item => legacyTask(item, "import")),
            };
        }

        if (recordKind === "download") {
            const fetched = await api.listDownloadJobs({
                subscription_source_id: route.subscriptionSourceId || undefined,
                status: status || undefined,
                source: source || undefined,
                q: domainQ,
                visibility: "all",
                sort_by: sort === "created-desc" || sort === "created-asc"
                    ? "created_at"
                    : sort === "updated-desc" ? "updated_at" : undefined,
                sort_order: order || (sort === "created-asc" ? "asc" : "desc"),
                offset,
                limit: TASK_PAGE_SIZE + 1,
            });
            const hasNext = fetched.length > TASK_PAGE_SIZE;
            const items = hasNext ? fetched.slice(0, TASK_PAGE_SIZE) : fetched;
            return {
                total: items.length,
                totalKnown: false,
                hasNext,
                items: items.map(item => legacyTask(item, "download")),
            };
        }

        const result = await api.listTasks({
            q: q || undefined,
            kind: kind || undefined,
            source: source || undefined,
            status: status || undefined,
            visibility: "all",
            scope,
            include_summary: true,
            offset,
            limit: TASK_PAGE_SIZE,
        });
        return {
            ...result,
            totalKnown: true,
            hasNext: offset + result.items.length < result.total,
        };
    }

    const query = useQuery<TaskListResponseWithPaging>({
        queryKey,
        queryFn: fetchTaskPage,
        enabled: (recordKind ? permissions.has("tasks") : allowed) && !recordScopeUnsupported,
        placeholderData: (previous, previousQuery) => previousQuery?.queryKey.at(-6) === records ? previous : undefined,
        staleTime: 15000,
        refetchInterval: query => query.state.data?.items.some(task => !terminalTaskStatuses.has(task.status)) ? POLL_ACTIVE_MS : false,
        refetchIntervalInBackground: false,
    });
    const items = query.data?.items || [];
    const rows = items;
    const total = query.data?.total || 0, totalKnown = query.data?.totalKnown !== false, hasNext = Boolean(query.data?.hasNext), pages = totalKnown ? Math.max(1, Math.ceil(total / TASK_PAGE_SIZE)) : page + (hasNext ? 2 : 1);
    function toggle(id: string) {
        setSelected(previous => {
            const next = new Set(previous);
            if (next.has(id)) next.delete(id);
            else next.add(id);
            return next;
        });
    }

    const allSelected = rows.length > 0 && rows.every(task => selected.has(task.id));

    function togglePage() {
        setSelected(previous => {
            if (allSelected) return new Set([...previous].filter(id => !rows.some(task => task.id === id)));
            return new Set([...previous, ...rows.map(task => task.id)]);
        });
    }
    return { t, permissions, allowed, q, kind, records, status, source, sort, order, scope, page, recordKind, relatedRecord, recordScopeUnsupported, input, setInput, isComposing, setIsComposing, selected, setSelected, toggle, togglePage, allSelected, query, rows, total, totalKnown, hasNext, pages, href, update, closeTask, closeLegacyJob, taskId: route.taskId, downloadJobId: route.downloadJobId, importJobId: route.importJobId, downloadJobFilterId: route.downloadJobFilterId, subscriptionSourceId: route.subscriptionSourceId, busy: input !== q || query.isPlaceholderData || (query.isFetching && !query.data) };
}
export type TaskListController = ReturnType<typeof useTaskList>;

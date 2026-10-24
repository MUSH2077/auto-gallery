"use client";
import { useEffect, useRef, useState } from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import { api, type TaskRun } from "@/lib/api";
import { usePermissions } from "@/lib/usePermissions";
import { useT } from "@/lib/i18n";
import { POLL_ACTIVE_MS } from "@/lib/polling";
import { terminalTaskStatuses } from "@/lib/taskPresentation";
import { taskListKey, TASK_PAGE_SIZE } from "./taskListQuery";
import { hasDetailListOrigin, markDetailListEntry } from "./detailHistory";

export function useTaskList() {
    const t = useT(), permissions = usePermissions(), sp = useSearchParams(), pathname = usePathname(), router = useRouter();
    const allowed = permissions.has('tasks') || permissions.has('system'), q = sp.get('q') || '', kind = sp.get('kind') || '', status = sp.get('status') || '';
    const requestedScope = sp.get('scope') || 'all', scope = (['all', 'attention', 'running', 'waiting'].includes(requestedScope) ? requestedScope : 'all') as 'all' | 'attention' | 'running' | 'waiting';
    const rawPage = Number(sp.get('p') || 0), page = Number.isSafeInteger(rawPage) && rawPage >= 0 ? rawPage : 0;
    const params = useRef(sp.toString()), detailEntry = useRef(false);
    useEffect(() => { params.current = sp.toString(); if (detailEntry.current && sp.get("task")) markDetailListEntry("task"); if (!sp.get("task")) detailEntry.current=false; }, [sp]);
    const [input, setInput] = useState(q), [selected, setSelected] = useState<Set<string>>(new Set()), [isComposing, setIsComposing] = useState(false);
    function href(patch: Record<string, string | null>, resetPage = false) { const next = new URLSearchParams(params.current); for (const [key, value] of Object.entries(patch)) {
        if (value === null || value === '')
            next.delete(key);
        else
            next.set(key, value);
    } if (resetPage) {
        next.delete('p');
        if (!('task' in patch))
            next.delete('task');
    } return `${pathname}?${next}`; }
    function update(patch: Record<string, string | null>, resetPage = true, push = false) { const url = href(patch, resetPage); params.current = url.split('?')[1] || ''; if (push && patch.task)
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
    useEffect(() => setInput(q), [q]);
    useEffect(() => { if (isComposing || input === q)
        return; const timer = setTimeout(() => update({ q: input }), 300); return () => clearTimeout(timer); }, [input, q, isComposing, pathname, router]);
    useEffect(() => setSelected(new Set()), [q, kind, status, scope, page]);
    const query = useQuery({ queryKey: taskListKey(q, kind, status, scope, page), queryFn: () => api.listTasks({ q: q || undefined, kind: kind || undefined, status: status || undefined, visibility: 'all', scope, include_summary: true, offset: page * TASK_PAGE_SIZE, limit: TASK_PAGE_SIZE }), enabled: allowed, staleTime: 15000, refetchInterval: query => query.state.data?.items.some(task => !terminalTaskStatuses.has(task.status)) ? POLL_ACTIVE_MS : false, refetchIntervalInBackground: false });
    const rows = query.data?.items || [], total = query.data?.total || 0, pages = Math.max(1, Math.ceil(total / TASK_PAGE_SIZE));
    function toggle(id: string) { setSelected(previous => { const next = new Set(previous); if (next.has(id))
        next.delete(id);
    else
        next.add(id); return next; }); }
    const allSelected = rows.length > 0 && rows.every(task => selected.has(task.id));
    function togglePage() { setSelected(previous => allSelected ? new Set([...previous].filter(id => !rows.some(task => task.id === id))) : new Set([...previous, ...rows.map(task => task.id)])); }
    return { t, permissions, allowed, q, kind, status, scope, page, input, setInput, isComposing, setIsComposing, selected, setSelected, toggle, togglePage, allSelected, query, rows, total, pages, href, update, closeTask, taskId: sp.get('task'), busy: input !== q || (query.isFetching && !query.data) };
}
export type TaskListController = ReturnType<typeof useTaskList>;

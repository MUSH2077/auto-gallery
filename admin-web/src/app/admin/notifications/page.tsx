"use client";
import { useMemo, useRef, useState } from "react";
import { useInfiniteQuery } from "@tanstack/react-query";
import { useT } from "@/lib/i18n";
import { api, queryKeys } from "@/lib/api";
import { useStaggeredEntrance } from "@/lib/motion";
import { EmptyState, ErrorState, StatusBadge, SourceBadge } from "@/components";
import { BusinessPage, BusinessPermissionGuard as PermissionGuard } from "@/features/gallery-ui/BusinessPage";
import { useI18nFormat } from "@/lib/i18n-format";
import { taskRunDestination } from "@/lib/taskRoutes";
import Link from "next/link";

type Filter = "all" | "tasks" | "account";
const PAGE_SIZE = 50;

// "tasks" = the long-running pipeline kinds; "account" = audit events.
const TASK_KINDS = ["download", "import", "admin"];

function NotificationsContent() {
  const t = useT();
  const fmt = useI18nFormat();
  const [filter, setFilter] = useState<Filter>("all");
  const filterRefs = useRef<Array<HTMLButtonElement | null>>([]);

  // Account is a single-kind server filter; "tasks" has no single kind param,
  // so it fetches all and filters client-side. "all" fetches everything.
  const kindParam = filter === "account" ? "account" : undefined;

  const query = useInfiniteQuery({
    queryKey: [...queryKeys.tasks.all, "feed", filter],
    queryFn: ({ pageParam = 0 }) =>
      api.listTasks({
        kind: kindParam,
        include_account: filter !== "tasks",
        offset: pageParam as number,
        limit: PAGE_SIZE,
      }),
    initialPageParam: 0,
    getNextPageParam: (lastPage, pages) => {
      const loaded = pages.reduce((n, p) => n + p.items.length, 0);
      return loaded < lastPage.total ? loaded : undefined;
    },
  });

  const items = useMemo(() => {
    const all = query.data?.pages.flatMap((p) => p.items) ?? [];
    if (filter === "tasks") return all.filter((task) => TASK_KINDS.includes(task.kind));
    return all;
  }, [query.data, filter]);
  const itemEntrance = useStaggeredEntrance(items.map((task) => task.id));

  const filters: { key: Filter; label: string }[] = [
    { key: "all", label: t("notifications.filter_all") },
    { key: "tasks", label: t("notifications.filter_tasks") },
    { key: "account", label: t("notifications.filter_account") },
  ];
  const focusFilter = (index: number) => {
    const next = (index + filters.length) % filters.length;
    const key = filters[next]?.key;
    if (!key) return;
    setFilter(key);
    requestAnimationFrame(() => filterRefs.current[next]?.focus());
  };

  return (
    <BusinessPage title={t("notifications.title")} description={t("notifications.desc")} className="gallery-notifications-page">

      <div className="business-toolbar gallery-notification-filters" role="tablist" aria-label={t("notifications.title")}>
        {filters.map((f, index) => (
          <button
            key={f.key}
            id={`notifications-filter-${f.key}`}
            ref={(node) => { filterRefs.current[index] = node; }}
            type="button"
            role="tab"
            aria-selected={filter === f.key}
            aria-controls="notifications-panel"
            tabIndex={filter === f.key ? 0 : -1}
            onClick={() => setFilter(f.key)}
            onKeyDown={(event) => {
              if (event.key === "ArrowRight" || event.key === "ArrowDown") { event.preventDefault(); focusFilter(index + 1); }
              else if (event.key === "ArrowLeft" || event.key === "ArrowUp") { event.preventDefault(); focusFilter(index - 1); }
              else if (event.key === "Home") { event.preventDefault(); focusFilter(0); }
              else if (event.key === "End") { event.preventDefault(); focusFilter(filters.length - 1); }
            }}
            className={`btn-secondary min-h-11 ${filter === f.key ? "is-active" : ""}`}
          >
            {f.label}
          </button>
        ))}
      </div>

      <div id="notifications-panel" role="tabpanel" aria-labelledby={`notifications-filter-${filter}`} tabIndex={-1}>
      {query.isError ? (
        <ErrorState message={t("notifications.load_error")} onRetry={() => query.refetch()} />
      ) : query.isLoading ? (
        <div className="space-y-3">
          {[0, 1, 2].map((i) => (
            <div key={i} className="card h-20 animate-pulse" />
          ))}
        </div>
      ) : items.length === 0 ? (
        <EmptyState title={t("notification.empty")} />
      ) : (
        <div className="space-y-3">
          {items.map((task, index) => {
            const link = taskRunDestination(task);
            const entrance = itemEntrance(task.id, index);
            const pct =
              task.progress_total && task.progress_current !== undefined && task.progress_total > 0
                ? Math.min(100, Math.max(0, Math.round(((task.progress_current ?? 0) / task.progress_total) * 100)))
                : null;
            const content = (
                <div className="flex items-start gap-3">
                  <div className="notification-entry-body flex-1 min-w-0">
                    <div className="mb-1 flex items-center gap-2">
                      <StatusBadge status={task.status} />
                      {task.source && <SourceBadge source={task.source} />}
                      <h3 className="truncate text-sm font-semibold">
                        {task.title || task.operation_type || task.kind}
                      </h3>
                    </div>
                    {pct !== null && task.status !== "complete" && (
                      <div
                        className="mt-2 h-1.5 w-full overflow-hidden rounded-full bg-subtle dark:bg-border"
                        role="progressbar"
                        aria-label={task.title || task.operation_type || task.kind}
                        aria-valuemin={0}
                        aria-valuemax={100}
                        aria-valuenow={pct}
                        aria-valuetext={`${pct}%`}
                        aria-busy={task.status !== "complete"}
                      >
                        <div
                          className="h-full w-full rounded-full bg-accent transition-transform duration-slow"
                          style={{ transform: `scaleX(${pct / 100})`, transformOrigin: "left" }}
                        />
                      </div>
                    )}
                    <time className="mt-1 block text-[10px] text-muted tabular" dateTime={task.created_at || undefined}>{fmt.dateTime(task.created_at)}</time>
                  </div>
                </div>
            );
            return link ? (
              <Link
                key={task.id}
                href={link}
                className={`card block p-4 hover:border-accent/50 ${entrance.className}`}
                style={entrance.style}
              >
                {content}
              </Link>
            ) : (
              <div key={task.id} className={`card p-4 ${entrance.className}`} style={entrance.style}>
                {content}
              </div>
            );
          })}

          {query.hasNextPage && (
            <div className="flex justify-center pt-2">
              <button
                onClick={() => query.fetchNextPage()}
                disabled={query.isFetchingNextPage}
                className="btn-ghost px-4 py-1.5 text-xs"
              >
                {t("notifications.load_more")}
              </button>
            </div>
          )}
        </div>
      )}
      </div>
    </BusinessPage>
  );
}

export default function NotificationsPage() {
  return (
    <PermissionGuard module="tasks">
      <NotificationsContent />
    </PermissionGuard>
  );
}

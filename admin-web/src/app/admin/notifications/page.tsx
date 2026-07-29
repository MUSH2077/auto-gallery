"use client";
import { useMemo, useState } from "react";
import { useInfiniteQuery } from "@tanstack/react-query";
import { useT } from "@/lib/i18n";
import { api, queryKeys } from "@/lib/api";
import type { TaskRun } from "@/lib/api/types";
import { useStaggeredEntrance } from "@/lib/motion";
import { PageHeader, PageShell, EmptyState, ErrorState, StatusBadge, SourceBadge, PermissionGuard } from "@/components";
import { useI18nFormat } from "@/lib/i18n-format";
import Link from "next/link";

type Filter = "all" | "tasks" | "account";
const PAGE_SIZE = 50;

// "tasks" = the long-running pipeline kinds; "account" = audit events.
const TASK_KINDS = ["download", "import", "admin"];

function taskLink(task: TaskRun): string | null {
  if (task.kind === "download" || task.kind === "import") {
    return `/admin/jobs?tab=${task.kind}&task=${task.id}`;
  }
  return null;
}

export default function NotificationsPage() {
  const t = useT();
  const fmt = useI18nFormat();
  const [filter, setFilter] = useState<Filter>("all");

  // Account is a single-kind server filter; "tasks" has no single kind param,
  // so it fetches all and filters client-side. "all" fetches everything.
  const kindParam = filter === "account" ? "account" : undefined;

  const query = useInfiniteQuery({
    queryKey: [...queryKeys.tasks.all, "feed", filter],
    queryFn: ({ pageParam = 0 }) =>
      api.listTasks({ kind: kindParam, offset: pageParam as number, limit: PAGE_SIZE }),
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

  return (
    <PermissionGuard module="tasks">
    <PageShell>
      <PageHeader title={t("notifications.title")} description={t("notifications.desc")} />

      <div className="mb-4 flex gap-1">
        {filters.map((f) => (
          <button
            key={f.key}
            onClick={() => setFilter(f.key)}
            className={`rounded-md px-3 py-1.5 text-xs font-medium transition-colors ${
              filter === f.key
                ? "bg-accent-subtle text-accent"
                : "text-muted hover:bg-subtle hover:text-fg"
            }`}
          >
            {f.label}
          </button>
        ))}
      </div>

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
            const link = taskLink(task);
            const entrance = itemEntrance(task.id, index);
            const pct =
              task.progress_total && task.progress_current !== undefined && task.progress_total > 0
                ? Math.round(((task.progress_current ?? 0) / task.progress_total) * 100)
                : null;
            const content = (
                <div className="flex items-start gap-3">
                  <div className="flex-1 min-w-0">
                    <div className="mb-1 flex items-center gap-2">
                      <StatusBadge status={task.status} />
                      {task.source && <SourceBadge source={task.source} />}
                      <h3 className="truncate text-sm font-semibold">
                        {task.title || task.operation_type || task.kind}
                      </h3>
                    </div>
                    {pct !== null && task.status !== "complete" && (
                      <div className="mt-2 h-1.5 w-full overflow-hidden rounded-full bg-subtle dark:bg-border">
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
    </PageShell>
    </PermissionGuard>
  );
}

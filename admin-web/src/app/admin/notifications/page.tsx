"use client";
import { useMemo, useState } from "react";
import { useInfiniteQuery } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { useT } from "@/lib/i18n";
import { api, queryKeys } from "@/lib/api";
import type { TaskRun } from "@/lib/api/types";
import { PageHeader, EmptyState, ErrorState, StatusBadge, SourceBadge } from "@/components";

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

function timeStr(iso?: string | null): string {
  if (!iso) return "";
  return new Date(iso).toLocaleString();
}

export default function NotificationsPage() {
  const t = useT();
  const router = useRouter();
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

  const filters: { key: Filter; label: string }[] = [
    { key: "all", label: t("notifications.filter_all", "全部") },
    { key: "tasks", label: t("notifications.filter_tasks", "任务") },
    { key: "account", label: t("notifications.filter_account", "账户") },
  ];

  return (
    <main className="max-w-3xl mx-auto p-6 md:p-10 page-transition">
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
        <ErrorState message={t("notifications.load_error", "加载通知失败")} onRetry={() => query.refetch()} />
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
          {items.map((task) => {
            const link = taskLink(task);
            const pct =
              task.progress_total && task.progress_current !== undefined && task.progress_total > 0
                ? Math.round(((task.progress_current ?? 0) / task.progress_total) * 100)
                : null;
            return (
              <div
                key={task.id}
                className={`card p-4 ${link ? "cursor-pointer hover:border-accent/50" : ""}`}
                onClick={() => link && router.push(link)}
              >
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
                          className="h-full rounded-full bg-accent transition-all duration-500"
                          style={{ width: `${pct}%` }}
                        />
                      </div>
                    )}
                    <p className="mt-1 text-[10px] text-muted tabular">{timeStr(task.created_at)}</p>
                  </div>
                </div>
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
                {t("notifications.load_more", "加载更多")}
              </button>
            </div>
          )}
        </div>
      )}
    </main>
  );
}

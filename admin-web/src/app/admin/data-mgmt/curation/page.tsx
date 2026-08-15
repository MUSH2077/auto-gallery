"use client";

import Link from "next/link";
import { Suspense, useMemo, useState } from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, CurationCommit, queryKeys } from "@/lib/api";
import { EmptyState, ErrorState, PageHeader, PageShell, PermissionGuard, WorkMediaThumbnail } from "@/components";
import { useT } from "@/lib/i18n";
import { useStaggeredEntrance } from "@/lib/motion";
import { useI18nFormat } from "@/lib/i18n-format";
import { adminRoutes } from "@/lib/adminRoutes";
import { usePermissions } from "@/lib/usePermissions";

function formatBytes(bytes: number) {
  if (!bytes) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let value = bytes;
  let index = 0;
  while (value >= 1024 && index < units.length - 1) {
    value /= 1024;
    index += 1;
  }
  return `${value.toFixed(value >= 10 || index === 0 ? 0 : 1)} ${units[index]}`;
}

function shortId(id: string) {
  return id.slice(0, 8);
}

function statusClass(status: string) {
  if (status === "baseline") return "border-border bg-subtle text-muted dark:border-border dark:bg-subtle dark:text-muted";
  if (status === "reverted") return "border-border bg-subtle text-muted dark:border-border dark:bg-subtle dark:text-muted";
  if (status === "partial_reverted") return "border-warning/30 bg-warning-subtle text-warning dark:bg-warning-subtle dark:text-warning";
  return "border-success/25 bg-success-subtle text-success dark:bg-success-subtle dark:text-success";
}

function actionLabel(action: string) {
  return action.replaceAll("_", " ");
}

function CommitCard({ commit, onRevert, reverting }: { commit: CurationCommit; onRevert: (id: string) => void; reverting: boolean }) {
  const t = useT();
  const fmt = useI18nFormat();
  const [expanded, setExpanded] = useState(false);
  const workChanges = commit.changes.filter((c) => c.subject_type === "work");
  const assetChanges = commit.changes.filter((c) => c.subject_type === "asset");
  const canRevert = commit.revertible ?? (commit.status === "active" && !commit.reverts_commit_id && !commit.is_baseline);
  const shownChanges = expanded ? commit.changes : commit.changes.slice(0, 5);
  const thumbnails = commit.changes
    .map((change) => (change.after_state?.thumbnail_asset_id || change.before_state?.thumbnail_asset_id) as string | undefined)
    .filter(Boolean)
    .slice(0, 8) as string[];
  return (
    <article className="relative pl-7">
      <div className="absolute left-[7px] top-2 h-full w-px bg-border dark:bg-border" />
      <div className="absolute left-0 top-2 h-3.5 w-3.5 rounded-full border-2 border-accent bg-white dark:border-accent dark:bg-canvas" />
      <div className="rounded-md border border-border bg-white p-4 dark:border-border dark:bg-surface">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <Link href={`${adminRoutes.curation}?commit=${commit.id}`} className="truncate text-sm font-semibold text-accent hover:underline dark:text-accent">
                {commit.message}
              </Link>
              <span className={`rounded-full border px-2 py-0.5 text-[11px] font-medium ${statusClass(commit.status)}`}>{commit.status}</span>
              {commit.is_baseline && <span className="rounded-full border border-border bg-subtle px-2 py-0.5 text-[11px] text-muted dark:border-border dark:bg-subtle dark:text-muted">{t("curation.baseline")}</span>}
              <span className="rounded-full border border-border px-2 py-0.5 text-[11px] text-muted dark:border-border dark:text-muted">{commit.trigger}</span>
            </div>
            <div className="mt-1 text-xs text-muted">
              <span className="font-mono">{shortId(commit.id)}</span>
              <span className="mx-1.5">·</span>
              <span>{commit.actor_type}</span>
              <span className="mx-1.5">·</span>
              <time dateTime={commit.occurred_at}>{fmt.dateTime(commit.occurred_at)}</time>
            </div>
          </div>
          {canRevert && (
            <button
              onClick={() => onRevert(commit.id)}
              disabled={reverting}
              className="rounded-md border border-border px-3 py-1.5 text-xs font-medium hover:bg-subtle disabled:opacity-50 dark:border-border dark:hover:bg-subtle"
            >
              {t("curation.revert")}
            </button>
          )}
          <button onClick={() => setExpanded((value) => !value)} className="rounded-md border border-border px-3 py-1.5 text-xs font-medium hover:bg-subtle dark:border-border dark:hover:bg-subtle">
            {expanded ? t("curation.compact") : t("curation.details")}
          </button>
        </div>

        <div className="mt-3 flex flex-wrap gap-2 text-xs text-muted">
          <span>{t("curation.works_count", { count: workChanges.length })}</span>
          <span>{t("curation.assets_count", { count: assetChanges.length })}</span>
          {typeof commit.stats?.bytes_reclaimed === "number" && <span>{t("curation.reclaimed", { size: formatBytes(commit.stats.bytes_reclaimed) })}</span>}
          {commit.reverts_commit_id && <span>{t("curation.reverts", { id: shortId(commit.reverts_commit_id) })}</span>}
        </div>

        {commit.changes.length > 0 && (
          <div className="mt-3 space-y-1 border-t border-border pt-3 dark:border-border">
            {thumbnails.length > 0 && (
              <div className="mb-2 flex gap-2 overflow-x-auto">
                {thumbnails.map((assetId) => (
                  <div key={assetId} className="h-12 w-12 shrink-0 overflow-hidden rounded-md border border-border bg-subtle dark:border-border dark:bg-subtle">
                    <WorkMediaThumbnail assetId={assetId} alt="" className="h-full w-full object-cover" />
                  </div>
                ))}
              </div>
            )}
            {shownChanges.map((change) => (
              <div key={change.id} className="rounded-md border border-border p-2 text-xs dark:border-border">
                <div className="flex items-center justify-between gap-3">
                  <span className="min-w-0 truncate text-fg">
                    <span className="font-medium">{actionLabel(change.action)}</span>
                    <span className="mx-1 text-placeholder">{t("curation.on")}</span>
                    <span className="font-mono">{change.subject_type}:{shortId(change.subject_id)}</span>
                  </span>
                  <span className="flex shrink-0 gap-2">
                    {change.subject_type === "work" && <Link href={`/admin/works/${change.subject_id}`} className="text-accent hover:underline dark:text-accent">{t("common.open")}</Link>}
                    {change.subject_type === "repository" && <Link href={adminRoutes.repository(change.subject_id)} className="text-accent hover:underline dark:text-accent">{t("curation.open_repository")}</Link>}
                    {change.subject_type === "repository" && <Link href={`/admin/jobs?tab=downloads&q=${encodeURIComponent(`kind:download repo:${change.subject_id}`)}`} className="text-accent hover:underline dark:text-accent">{t("curation.open_jobs")}</Link>}
                  </span>
                </div>
                {expanded && change.diff && Object.keys(change.diff).length > 0 && (
                  <pre className="mt-2 max-h-40 overflow-auto rounded bg-subtle p-2 font-mono text-[11px] text-muted dark:bg-canvas dark:text-muted">{JSON.stringify(change.diff, null, 2)}</pre>
                )}
              </div>
            ))}
            {commit.changes.length > 5 && <div className="text-xs text-muted">{t("curation.more_changes", { count: commit.changes.length - 5 })}</div>}
          </div>
        )}
      </div>
    </article>
  );
}

function CurationContent() {
  const t = useT();
  const qc = useQueryClient();
  const sp = useSearchParams();
  const router = useRouter();
  const pathname = usePathname();
  const { isAdmin } = usePermissions();
  const trigger = sp.get("trigger") || "";
  const includeBaseline = sp.get("include_baseline") !== "false";
  const subjectType = sp.get("subject_type") || undefined;
  const subjectId = sp.get("subject_id") || undefined;
  const params = useMemo(() => ({ limit: 40, trigger: trigger || undefined, subject_type: subjectType, subject_id: subjectId, include_baseline: includeBaseline }), [trigger, subjectType, subjectId, includeBaseline]);
  const commits = useQuery({ queryKey: queryKeys.curation.commits(params), queryFn: () => api.listCurationCommits(params) });
  const trash = useQuery({
    queryKey: [...queryKeys.works.all, "trash-preview"],
    queryFn: async () => {
      const result = await api.search("is:trashed sort:updated-desc", 0, 12, "works");
      return result.groups.works || { total: 0, items: [] };
    },
  });
  const purgePreview = useQuery({
    queryKey: [...queryKeys.curation.all, "purge-preview"],
    queryFn: () => api.previewPurge(),
    refetchInterval: 30000,
    enabled: isAdmin,
  });
  const suggestions = useQuery({ queryKey: queryKeys.curation.suggestions, queryFn: api.curationRuleSuggestions });
  const backfillStatus = useQuery({ queryKey: queryKeys.curation.backfillStatus, queryFn: api.getCurationBackfillStatus, refetchInterval: 30000 });
  const commitItems = commits.data?.items ?? [];
  const suggestionItems = suggestions.data ?? [];
  const commitEntrance = useStaggeredEntrance(commitItems.map((commit) => commit.id));
  const suggestionEntrance = useStaggeredEntrance(suggestionItems.map((item) => item.id));

  const revert = useMutation({
    mutationFn: (id: string) => api.revertCurationCommit(id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: queryKeys.curation.all });
      qc.invalidateQueries({ queryKey: queryKeys.works.all });
      qc.invalidateQueries({ queryKey: ["repositories"] });
      qc.invalidateQueries({ queryKey: queryKeys.downloadJobs.all });
      qc.invalidateQueries({ queryKey: queryKeys.importJobs.all });
    },
  });

  const purge = useMutation({
    mutationFn: async () => {
      const ids = (purgePreview.data?.works ?? []).map((work) => work.id);
      for (let start = 0; start < ids.length; start += 25) {
        await api.purgeWorks(
          ids.slice(start, start + 25),
          "Purge currently eligible trashed works",
        );
      }
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: queryKeys.curation.all });
      qc.invalidateQueries({ queryKey: queryKeys.works.all });
    },
  });

  const backfill = useMutation({
    mutationFn: api.runCurationBackfill,
    onSuccess: () => {
      // Queued: the replay runs in the governed maintenance listener.
      qc.invalidateQueries({ queryKey: queryKeys.curation.all });
      qc.invalidateQueries({ queryKey: queryKeys.curation.backfillStatus });
      qc.invalidateQueries({ queryKey: ["repositories"] });
      qc.invalidateQueries({ queryKey: queryKeys.downloadJobs.all });
      qc.invalidateQueries({ queryKey: queryKeys.tasks.all });
    },
  });

  const filters = [
    ["", t("curation.filter_all")],
    ["work_trash", t("curation.filter_trash")],
    ["work_restore", t("curation.filter_restore")],
    ["work_purge", t("curation.filter_purge")],
    ["creator_archive", t("curation.filter_creator")],
    ["source_synced", t("curation.filter_import")],
    ["commit_revert", t("curation.filter_revert")],
  ];
  const updateParams = (updates: Record<string, string | null>) => {
    const next = new URLSearchParams(sp.toString());
    Object.entries(updates).forEach(([key, value]) => {
      if (!value) next.delete(key); else next.set(key, value);
    });
    router.replace(next.toString() ? `${pathname}?${next.toString()}` : pathname, { scroll: false });
  };

  return (
    <PageShell>
      <PageHeader title={t("curation.title")} description={t("curation.desc")} />
      {(subjectType || subjectId) && (
        <div className="mb-4 rounded-md border border-border bg-white px-3 py-2 text-sm dark:border-border dark:bg-surface">
          {t("curation.filtered_by")} <span className="font-mono">{subjectType}:{subjectId}</span>
          <Link href={adminRoutes.curation} className="ml-3 text-accent hover:underline dark:text-accent">{t("common.clear")}</Link>
        </div>
      )}

      <div className="mb-4 flex flex-wrap items-center gap-2">
        {filters.map(([key, label]) => (
          <button
            key={key}
            onClick={() => updateParams({ trigger: key || null })}
            className={`rounded-md border px-3 py-1.5 text-xs font-medium ${trigger === key ? "border-accent bg-accent-subtle text-accent dark:border-accent dark:bg-accent-subtle dark:text-accent" : "border-border bg-white hover:bg-subtle dark:border-border dark:bg-surface dark:hover:bg-subtle"}`}
          >
            {label}
          </button>
        ))}
        <button
          onClick={() => updateParams({ include_baseline: includeBaseline ? "false" : null })}
          className={`rounded-md border px-3 py-1.5 text-xs font-medium ${includeBaseline ? "border-border bg-white hover:bg-subtle dark:border-border dark:bg-surface dark:hover:bg-subtle" : "border-accent bg-accent-subtle text-accent dark:border-accent dark:bg-accent-subtle dark:text-accent"}`}
        >
          {includeBaseline ? t("curation.hide_baseline") : t("curation.show_baseline")}
        </button>
      </div>

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-[minmax(0,1fr)_320px]">
        <section className="space-y-4">
          {commits.isLoading && <div className="h-48 rounded-md border border-border bg-white dark:border-border dark:bg-surface" />}
          {commits.error && <ErrorState message={(commits.error as Error).message} onRetry={() => commits.refetch()} />}
          {commits.data && commits.data.items.length === 0 && <EmptyState title={t("curation.empty_title")} description={t("curation.empty_desc")} />}
          {commitItems.map((commit, index) => {
            const entrance = commitEntrance(commit.id, index);
            return (
              <div key={commit.id} className={entrance.className} style={entrance.style}>
                <CommitCard commit={commit} onRevert={(id) => revert.mutate(id)} reverting={revert.isPending} />
              </div>
            );
          })}
        </section>

        <aside className="space-y-4">
          <div className="rounded-md border border-border bg-white p-4 dark:border-border dark:bg-surface">
            <h2 className="text-sm font-semibold">{t("curation.baseline_title")}</h2>
            <p className="mt-1 text-xs text-muted">
              {t("curation.baseline_desc")}
            </p>
            <div className="mt-3 grid grid-cols-3 gap-2 text-xs">
              {(["creators", "repositories", "work_groups"] as const).map((key) => (
                <div key={key} className="rounded-md border border-border p-2 dark:border-border">
                  <div className="font-semibold">{backfillStatus.data?.missing?.[key] ?? "-"}</div>
                  <div className="mt-0.5 text-muted">{t(`curation.baseline_${key}`)}</div>
                  <div className="mt-1 text-[11px] text-placeholder dark:text-muted">
                    {t("curation.baseline_counts", {
                      existing: backfillStatus.data?.existing?.[key] ?? "-",
                      expected: backfillStatus.data?.expected?.[key] ?? "-",
                    })}
                  </div>
                </div>
              ))}
            </div>
            {backfillStatus.data?.is_complete && (
              <p className="mt-3 text-xs text-success dark:text-success">{t("curation.baseline_graph_hint")}</p>
            )}
            <button
              onClick={() => backfill.mutate()}
              disabled={backfill.isPending || backfillStatus.data?.is_complete}
              className="btn-primary mt-3 min-h-11 w-full text-xs"
            >
              {backfillStatus.data?.is_complete ? t("curation.baseline_complete") : backfill.isPending ? t("curation.backfilling") : t("curation.run_backfill")}
            </button>
          </div>

          <div className="rounded-md border border-border bg-white p-4 dark:border-border dark:bg-surface">
            <h2 className="text-sm font-semibold">{t("curation.trash")}</h2>
            <div className="mt-3 grid grid-cols-2 gap-2 text-sm">
              <div>
                <div className="text-2xl font-semibold">{trash.data?.total ?? "-"}</div>
                <div className="text-xs text-muted">{t("curation.trashed_works")}</div>
              </div>
              <div>
                <div className="text-2xl font-semibold">{formatBytes(purgePreview.data?.bytes_reclaimable || 0)}</div>
                <div className="text-xs text-muted">{t("curation.reclaimable")}</div>
              </div>
            </div>
            <div className="mt-3 flex gap-2">
              <Link href="/admin/works?curation=trashed" className="rounded-md border border-border px-3 py-1.5 text-xs font-medium hover:bg-subtle dark:border-border dark:hover:bg-subtle">{t("curation.open_trash")}</Link>
              {isAdmin && <button
                onClick={() => {
                  if (window.confirm(t("curation.purge_confirm"))) purge.mutate();
                }}
                disabled={!purgePreview.data?.work_count || purge.isPending}
                className="rounded-md bg-danger px-3 py-1.5 text-xs font-medium text-white hover:bg-danger disabled:opacity-50"
              >
                {t("curation.purge")}
              </button>}
            </div>
          </div>

          <div className="rounded-md border border-border bg-white p-4 dark:border-border dark:bg-surface">
            <h2 className="text-sm font-semibold">{t("curation.rule_suggestions")}</h2>
            <div className="mt-3 space-y-3">
              {suggestionItems.length ? suggestionItems.map((item, index) => {
                const entrance = suggestionEntrance(item.id, index);
                return (
                  <div key={item.id} className={`${entrance.className} rounded-md border border-border p-3 text-sm dark:border-border`} style={entrance.style}>
                    <div className="font-medium">{item.title}</div>
                    <p className="mt-1 text-xs text-muted">{item.description}</p>
                  </div>
                );
              }) : <p className="text-xs text-muted">{t("curation.no_suggestions")}</p>}
            </div>
          </div>
        </aside>
      </div>
    </PageShell>
  );
}

export default function CurationPage() {
  return (
    <PermissionGuard module="curation">
      <Suspense>
        <CurationContent />
      </Suspense>
    </PermissionGuard>
  );
}

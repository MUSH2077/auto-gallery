"use client";

import Link from "next/link";
import type { ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, queryKeys, WorkbenchSummary } from "@/lib/api";
import { EmptyState, ErrorState, SourceBadge, StatusBadge, PageHeader, PageShell, MotionNumber } from "@/components";
import { useT } from "@/lib/i18n";
import { useI18nFormat } from "@/lib/i18n-format";

function fmtBytes(bytes?: number | null): string {
  if (!bytes) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  const i = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
  return `${(bytes / Math.pow(1024, i)).toFixed(i > 0 ? 1 : 0)} ${units[i]}`;
}

function StatusDot({ tone }: { tone: "ok" | "warning" | "danger" | "muted" | "info" }) {
  const cls = {
    ok: "bg-success",
    warning: "bg-warning",
    danger: "bg-danger",
    muted: "bg-placeholder",
    info: "bg-accent",
  }[tone];
  return <span className={`h-2.5 w-2.5 rounded-full ${cls}`} />;
}

function MetricCard({ label, value, sub, tone = "muted" }: { label: string; value: string | number; sub?: string; tone?: "ok" | "warning" | "danger" | "muted" | "info" }) {
  return (
    <div className="card p-4">
      <div className="flex items-center justify-between gap-3">
        <div className="text-xs font-medium uppercase text-muted">{label}</div>
        <StatusDot tone={tone} />
      </div>
      <div className="mt-2 tabular text-2xl font-semibold tracking-tight text-fg">
        {typeof value === "number" ? <MotionNumber value={value} /> : value}
      </div>
      {sub && <div className="mt-1 truncate text-xs text-muted">{sub}</div>}
    </div>
  );
}

function AttentionCard({ title, value, description, href, tone }: { title: string; value: string | number; description: string; href: string; tone: "ok" | "warning" | "danger" | "muted" }) {
  const active = tone !== "ok" && tone !== "muted";
  return (
    <Link
      href={href}
      className={`rounded-md border p-4 transition-colors ${
        active
          ? "border-danger/30 bg-danger-subtle hover:border-danger/50 dark:border-danger/30 dark:bg-danger-subtle"
          : "border-border bg-white hover:border-accent/40 dark:border-border dark:bg-surface"
      }`}
    >
      <div className="flex items-center justify-between gap-3">
        <h3 className="text-sm font-semibold text-fg">{title}</h3>
        <StatusDot tone={tone === "ok" ? "ok" : tone === "muted" ? "muted" : tone} />
      </div>
      <div className="mt-2 tabular text-xl font-semibold text-fg">{value}</div>
      <p className="mt-1 text-xs leading-5 text-muted">{description}</p>
    </Link>
  );
}

function RecentRow({ children, href }: { children: ReactNode; href: string }) {
  return (
    <Link href={href} className="flex min-w-0 items-center gap-3 rounded-md px-2 py-2 text-sm hover:bg-subtle dark:hover:bg-subtle">
      {children}
    </Link>
  );
}

function MiniBar({ value, max, tone }: { value: number; max: number; tone: "info" | "danger" | "warning" | "ok" }) {
  const pct = max <= 0 ? 0 : Math.min(100, Math.max(4, (value / max) * 100));
  const color = tone === "danger" ? "bg-danger" : tone === "warning" ? "bg-warning" : tone === "ok" ? "bg-success" : "bg-accent";
  // scaleX (not width): transform-only per motion red-lines. animate-bar-grow
  // plays once on mount; the transition smooths later poll-driven changes.
  return (
    <div className="mt-3 h-1.5 overflow-hidden rounded-full bg-subtle dark:bg-border">
      <div
        className={`h-full w-full ${color} animate-bar-grow transition-transform duration-slow ease-expo`}
        style={{ transform: `scaleX(${pct / 100})`, transformOrigin: "left" }}
      />
    </div>
  );
}

function RecentActivity({ data }: { data: WorkbenchSummary }) {
  const t = useT();
  const fmt = useI18nFormat();
  return (
    <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
      <section className="card p-4">
        <h2 className="mb-3 text-base font-semibold">{t("dashboard.recent_download_jobs")}</h2>
        {data.recent.download_jobs.length ? (
          <div className="space-y-1">
            {data.recent.download_jobs.map((job) => (
              <RecentRow key={job.id} href="/admin/jobs">
                <SourceBadge source={job.source} />
                <span className="min-w-0 flex-1 truncate font-mono text-xs text-muted">{job.source_url}</span>
                <StatusBadge status={job.status} />
              </RecentRow>
            ))}
          </div>
        ) : <EmptyState title={t("dashboard.no_download_jobs")} description={t("dashboard.no_download_jobs_desc")} />}
      </section>

      <section className="card p-4">
        <h2 className="mb-3 text-base font-semibold">{t("dashboard.recent_imports_works")}</h2>
        <div className="space-y-1">
          {data.recent.import_jobs.slice(0, 3).map((job) => (
            <RecentRow key={job.id} href="/admin/import-jobs">
              <span className="w-20 font-mono text-xs text-muted">{job.id.slice(0, 8)}</span>
              <span className="min-w-0 flex-1 truncate text-xs text-muted">{t("dashboard.import_for", { id: job.download_job_id.slice(0, 8) })}</span>
              <StatusBadge status={job.status} />
            </RecentRow>
          ))}
          {data.recent.works.slice(0, 5).map((work) => (
            <RecentRow key={work.id} href={`/admin/works/${work.id}`}>
              <div className="h-8 w-8 shrink-0 overflow-hidden rounded-md border border-border bg-subtle dark:border-border dark:bg-subtle">
                {work.thumbnail_asset_id && <img src={api.mediaUrl(work.thumbnail_asset_id, "thumb")} alt="" className="h-full w-full object-cover" loading="lazy" decoding="async" />}
              </div>
              <span className="min-w-0 flex-1 truncate text-sm text-fg">{work.title || t("dashboard.untitled_work")}</span>
              <span className="text-xs text-muted">{fmt.dateTime(work.created_at)}</span>
            </RecentRow>
          ))}
          {!data.recent.import_jobs.length && !data.recent.works.length && (
            <p className="px-2 py-4 text-sm text-muted">{t("dashboard.no_recent_imports_works")}</p>
          )}
        </div>
      </section>
      <section className="card p-4 xl:col-span-2">
        <h2 className="mb-3 text-base font-semibold">{t("dashboard.successful_syncs")}</h2>
        {data.recent.successful_syncs.length ? (
          <div className="grid gap-2 md:grid-cols-2">
            {data.recent.successful_syncs.map((sync) => (
              <RecentRow key={sync.source_id} href={`/admin/creators/${sync.creator_id}`}>
                <SourceBadge source={sync.source} />
                <span className="min-w-0 flex-1 truncate text-sm text-fg">{sync.creator_name}</span>
                <span className="text-xs text-muted">{fmt.relative(sync.last_synced_at)}</span>
              </RecentRow>
            ))}
          </div>
        ) : <p className="px-2 py-4 text-sm text-muted">{t("dashboard.no_successful_syncs")}</p>}
      </section>
    </div>
  );
}

export default function Dashboard() {
  const t = useT();
  const fmt = useI18nFormat();
  const workbench = useQuery({
    queryKey: queryKeys.workbench,
    queryFn: api.workbench,
    refetchInterval: (query) => {
      const data = query.state.data;
      const active = (data?.queue.active_download_count || 0) + (data?.queue.active_import_count || 0);
      return active > 0 ? 5000 : 15000;
    },
  });

  const data = workbench.data;
  const activeJobs = (data?.queue.active_download_count || 0) + (data?.queue.active_import_count || 0);

  if (workbench.error) {
    return <PageShell><ErrorState message={(workbench.error as Error).message} onRetry={() => workbench.refetch()} /></PageShell>;
  }

  return (
    <PageShell className="page-transition">
      <PageHeader title={t("dashboard.title")} description={t("dashboard.workbench_desc")}>
          <div className="flex items-center gap-2 text-xs text-muted">
            <span className={`h-2 w-2 rounded-full ${activeJobs > 0 ? "animate-pulse bg-accent" : "bg-success"}`} />
            <span>{activeJobs > 0 ? t("dashboard.live") : t("dashboard.idle")}</span>
            <span>·</span>
            <span>{t("dashboard.updated", { time: fmt.dateTime(data?.updated_at) })}</span>
          </div>
      </PageHeader>

      {!data && workbench.isLoading && (
        <div className="grid grid-cols-2 gap-4 md:grid-cols-4">{Array.from({ length: 8 }).map((_, i) => <div key={`skel-${i}`} className="card h-28 animate-pulse" />)}</div>
      )}

      {data && (
        <div className="space-y-6">
          <section className="grid grid-cols-2 gap-3 lg:grid-cols-6">
            <MetricCard
              label={t("dashboard.auto_sync")}
              value={data.scheduler.enabled ? t("common.on") : t("common.off")}
              sub={data.scheduler.next_scan_at ? t("dashboard.next_scan", { time: fmt.dateTime(data.scheduler.next_scan_at) }) : t("dashboard.no_scan")}
              tone={data.scheduler.enabled ? "ok" : "danger"}
            />
            <MetricCard
              label={t("dashboard.active_jobs")}
              value={data.queue.active_download_count + data.queue.active_import_count}
              sub={t("dashboard.active_jobs_sub", { downloads: data.queue.active_download_count, imports: data.queue.active_import_count })}
              tone={data.queue.active_download_count + data.queue.active_import_count > 0 ? "info" : "muted"}
            />
            <MetricCard
              label={t("dashboard.failed")}
              value={data.queue.failed_download_count + data.queue.failed_import_count}
              sub={t("dashboard.failed_jobs_sub", { downloads: data.queue.failed_download_count, imports: data.queue.failed_import_count })}
              tone={data.queue.failed_download_count + data.queue.failed_import_count > 0 ? "danger" : "ok"}
            />
            <MetricCard
              label={t("dashboard.stale")}
              value={data.queue.stale_count}
              sub={t("dashboard.stale_sub")}
              tone={data.queue.stale_count > 0 ? "warning" : "ok"}
            />
            <MetricCard
              label={t("dashboard.disk")}
              value={data.storage.disk_free_percent == null ? "—" : t("dashboard.disk_free_percent", { percent: data.storage.disk_free_percent })}
              sub={t("dashboard.disk_free", { size: fmtBytes(data.storage.disk_free_bytes) })}
              tone={data.storage.risk_level === "critical" ? "danger" : data.storage.risk_level === "warning" ? "warning" : "ok"}
            />
            {(workbench.data as any)?.queue?.rebuild_active > 0 && (
              <MetricCard label={t("dashboard.rebuild_active")} value={(workbench.data as any).queue.rebuild_active} sub={t("dashboard.rebuild_active_sub")} tone="info" />
            )}
          </section>
          <div className="grid gap-3 md:grid-cols-3">
            <div className="card p-3 text-xs text-muted">
              <div className="flex justify-between"><span>{t("dashboard.active_jobs")}</span><span className="font-mono">{activeJobs}</span></div>
              <MiniBar value={activeJobs} max={Math.max(activeJobs, 5)} tone="info" />
            </div>
            <div className="card p-3 text-xs text-muted">
              <div className="flex justify-between"><span>{t("dashboard.failed")}</span><span className="font-mono">{data.queue.failed_download_count + data.queue.failed_import_count}</span></div>
              <MiniBar value={data.queue.failed_download_count + data.queue.failed_import_count} max={Math.max(data.queue.failed_download_count + data.queue.failed_import_count, 5)} tone="danger" />
            </div>
            <div className="card p-3 text-xs text-muted">
              <div className="flex justify-between"><span>{t("dashboard.disk_free")}</span><span className="font-mono">{data.storage.disk_free_percent ?? "—"}%</span></div>
              <MiniBar value={data.storage.disk_used_percent || 0} max={100} tone={data.storage.risk_level === "warning" ? "warning" : "ok"} />
            </div>
          </div>

          <section>
            <div className="mb-3 flex items-center justify-between gap-3">
              <h2 className="section-title">{t("dashboard.attention")}</h2>
              <Link href="/admin/scheduler" className="text-sm text-accent hover:underline dark:text-accent">{t("dashboard.open_scheduler")}</Link>
            </div>
            <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-5">
              <AttentionCard title={t("dashboard.auth_issues")} value={data.attention.auth_unhealthy_count} description={t("dashboard.auth_issues_desc")} href="/admin/settings/auth-status" tone={data.attention.auth_unhealthy_count ? "danger" : "ok"} />
              <AttentionCard title={t("dashboard.failed_downloads")} value={data.attention.failed_download_count} description={t("dashboard.failed_downloads_desc")} href="/admin/jobs" tone={data.attention.failed_download_count ? "danger" : "ok"} />
              <AttentionCard title={t("dashboard.failed_imports")} value={data.attention.failed_import_count} description={t("dashboard.failed_imports_desc")} href="/admin/import-jobs" tone={data.attention.failed_import_count ? "danger" : "ok"} />
              <AttentionCard title={t("dashboard.stale_jobs")} value={data.attention.stale_job_count} description={t("dashboard.stale_jobs_desc")} href="/admin/jobs" tone={data.attention.stale_job_count ? "warning" : "ok"} />
              <AttentionCard title={t("dashboard.storage_risk")} value={data.storage.risk_level} description={t("dashboard.storage_risk_desc", { size: fmtBytes(data.storage.disk_free_bytes) })} href="/admin/settings/data-mgmt" tone={data.attention.low_disk_warning ? "warning" : "ok"} />
            </div>
          </section>

          <section className="grid grid-cols-1 gap-4 xl:grid-cols-[1fr_360px]">
            <RecentActivity data={data} />
            <aside className="space-y-4">
              <section className="card p-4">
                <h2 className="mb-3 text-base font-semibold">{t("dashboard.services")}</h2>
                <div className="grid grid-cols-2 gap-2">
                  {Object.entries(data.health).map(([name, status]) => (
                    <div key={name} className="flex items-center justify-between gap-2 rounded-md border border-border px-3 py-2 dark:border-border">
                      <span className="truncate text-xs capitalize text-muted">{name}</span>
                      <StatusBadge status={status} />
                    </div>
                  ))}
                </div>
              </section>
              <section className="card p-4">
                <h2 className="mb-3 text-base font-semibold">{t("dashboard.quick_links")}</h2>
                <div className="grid grid-cols-2 gap-2">
                  {[
                    ["/admin/creators", t("dashboard.quick_creators")],
                    ["/admin/subscriptions", t("dashboard.quick_subscriptions")],
                    ["/admin/scheduler", t("dashboard.quick_scheduler")],
                    ["/admin/jobs", t("dashboard.quick_download_jobs")],
                    ["/admin/works", t("dashboard.quick_works")],
                    ["/admin/settings", t("dashboard.quick_settings")],
                  ].map(([href, label]) => (
                    <Link key={href} href={href} className="rounded-md border border-border px-3 py-2 text-sm font-medium hover:bg-subtle dark:border-border dark:hover:bg-subtle">{label}</Link>
                  ))}
                </div>
              </section>
            </aside>
          </section>
        </div>
      )}
    </PageShell>
  );
}

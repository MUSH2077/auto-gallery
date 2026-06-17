"use client";

import Link from "next/link";
import type { ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, queryKeys, WorkbenchSummary } from "@/lib/api";
import { EmptyState, ErrorState, SourceBadge, StatusBadge } from "@/components";
import { useT } from "@/lib/i18n";
import { statusLabel, useI18nFormat } from "@/lib/i18n-format";

function fmtBytes(bytes?: number | null): string {
  if (!bytes) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  const i = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
  return `${(bytes / Math.pow(1024, i)).toFixed(i > 0 ? 1 : 0)} ${units[i]}`;
}

function StatusDot({ tone }: { tone: "ok" | "warning" | "danger" | "muted" | "info" }) {
  const cls = {
    ok: "bg-[#1a7f37]",
    warning: "bg-[#bf8700]",
    danger: "bg-[#cf222e]",
    muted: "bg-[#8c959f]",
    info: "bg-[#0969da]",
  }[tone];
  return <span className={`h-2.5 w-2.5 rounded-full ${cls}`} />;
}

function MetricCard({ label, value, sub, tone = "muted" }: { label: string; value: string | number; sub?: string; tone?: "ok" | "warning" | "danger" | "muted" | "info" }) {
  return (
    <div className="card p-4">
      <div className="flex items-center justify-between gap-3">
        <div className="text-xs font-medium uppercase text-[#57606a] dark:text-[#8b949e]">{label}</div>
        <StatusDot tone={tone} />
      </div>
      <div className="mt-2 tabular text-2xl font-semibold tracking-tight text-[#24292f] dark:text-[#e6edf3]">{value}</div>
      {sub && <div className="mt-1 truncate text-xs text-[#57606a] dark:text-[#8b949e]">{sub}</div>}
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
          ? "border-[#cf222e]/30 bg-[#fff8f7] hover:border-[#cf222e]/50 dark:border-[#f85149]/30 dark:bg-[#f8514914]"
          : "border-[#d8dee4] bg-white hover:border-[#0969da]/40 dark:border-[#30363d] dark:bg-[#161b22]"
      }`}
    >
      <div className="flex items-center justify-between gap-3">
        <h3 className="text-sm font-semibold text-[#24292f] dark:text-[#e6edf3]">{title}</h3>
        <StatusDot tone={tone === "ok" ? "ok" : tone === "muted" ? "muted" : tone} />
      </div>
      <div className="mt-2 tabular text-xl font-semibold text-[#24292f] dark:text-[#e6edf3]">{value}</div>
      <p className="mt-1 text-xs leading-5 text-[#57606a] dark:text-[#8b949e]">{description}</p>
    </Link>
  );
}

function RecentRow({ children, href }: { children: ReactNode; href: string }) {
  return (
    <Link href={href} className="flex min-w-0 items-center gap-3 rounded-md px-2 py-2 text-sm hover:bg-[#f6f8fa] dark:hover:bg-[#21262d]">
      {children}
    </Link>
  );
}

function MiniBar({ value, max, tone }: { value: number; max: number; tone: "info" | "danger" | "warning" | "ok" }) {
  const width = max <= 0 ? 0 : Math.min(100, Math.max(4, (value / max) * 100));
  const color = tone === "danger" ? "bg-[#cf222e]" : tone === "warning" ? "bg-[#bf8700]" : tone === "ok" ? "bg-[#1a7f37]" : "bg-[#0969da]";
  return <div className="mt-3 h-1.5 overflow-hidden rounded-full bg-[#eaeef2] dark:bg-[#30363d]"><div className={`h-full rounded-full ${color}`} style={{ width: `${width}%` }} /></div>;
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
                <span className="min-w-0 flex-1 truncate font-mono text-xs text-[#57606a] dark:text-[#8b949e]">{job.source_url}</span>
                <span className="badge">{statusLabel(t, job.status)}</span>
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
              <span className="w-20 font-mono text-xs text-[#57606a] dark:text-[#8b949e]">{job.id.slice(0, 8)}</span>
              <span className="min-w-0 flex-1 truncate text-xs text-[#57606a] dark:text-[#8b949e]">{t("dashboard.import_for", { id: job.download_job_id.slice(0, 8) })}</span>
              <span className="badge">{statusLabel(t, job.status)}</span>
            </RecentRow>
          ))}
          {data.recent.works.slice(0, 5).map((work) => (
            <RecentRow key={work.id} href={`/admin/works/${work.id}`}>
              <div className="h-8 w-8 shrink-0 overflow-hidden rounded-md border border-[#d8dee4] bg-[#f6f8fa] dark:border-[#30363d] dark:bg-[#21262d]">
                {work.thumbnail_asset_id && <img src={api.mediaUrl(work.thumbnail_asset_id, "thumb")} alt="" className="h-full w-full object-cover" loading="lazy" />}
              </div>
              <span className="min-w-0 flex-1 truncate text-sm text-[#24292f] dark:text-[#e6edf3]">{work.title || t("dashboard.untitled_work")}</span>
              <span className="text-xs text-[#57606a] dark:text-[#8b949e]">{fmt.dateTime(work.created_at)}</span>
            </RecentRow>
          ))}
          {!data.recent.import_jobs.length && !data.recent.works.length && (
            <p className="px-2 py-4 text-sm text-[#57606a] dark:text-[#8b949e]">{t("dashboard.no_recent_imports_works")}</p>
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
                <span className="min-w-0 flex-1 truncate text-sm text-[#24292f] dark:text-[#e6edf3]">{sync.creator_name}</span>
                <span className="text-xs text-[#57606a] dark:text-[#8b949e]">{fmt.relative(sync.last_synced_at)}</span>
              </RecentRow>
            ))}
          </div>
        ) : <p className="px-2 py-4 text-sm text-[#57606a] dark:text-[#8b949e]">{t("dashboard.no_successful_syncs")}</p>}
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
    return <main className="mx-auto max-w-7xl p-6"><ErrorState message={(workbench.error as Error).message} onRetry={() => workbench.refetch()} /></main>;
  }

  return (
    <main className="mx-auto max-w-7xl p-6 page-transition">
      <header className="mb-5 border-b border-[#d8dee4] pb-4 dark:border-[#30363d]">
        <div className="flex flex-col gap-3 md:flex-row md:items-end md:justify-between">
          <div>
            <h1 className="text-2xl font-semibold tracking-normal text-[#24292f] dark:text-[#e6edf3]">{t("dashboard.title")}</h1>
            <p className="mt-1.5 max-w-3xl text-sm leading-6 text-[#57606a] dark:text-[#8b949e]">
              {t("dashboard.workbench_desc")}
            </p>
          </div>
          <div className="flex items-center gap-2 text-xs text-[#57606a] dark:text-[#8b949e]">
            <span className={`h-2 w-2 rounded-full ${activeJobs > 0 ? "animate-pulse bg-[#0969da]" : "bg-[#1a7f37]"}`} />
            <span>{activeJobs > 0 ? t("dashboard.live") : t("dashboard.idle")}</span>
            <span>·</span>
            <span>{t("dashboard.updated", { time: fmt.dateTime(data?.updated_at) })}</span>
          </div>
        </div>
      </header>

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
              sub={t("dashboard.original_media_size", { size: fmtBytes(data.storage.original_media_size_bytes) })}
              tone={data.storage.risk_level === "critical" ? "danger" : data.storage.risk_level === "warning" ? "warning" : "ok"}
            />
            <MetricCard
              label={t("dashboard.library")}
              value={fmtBytes(data.storage.library_size_bytes)}
              sub={t("dashboard.index_files", { count: fmt.number(data.storage.library_file_count) })}
              tone="muted"
            />
          </section>
          <div className="grid gap-3 md:grid-cols-3">
            <div className="card p-3 text-xs text-[#57606a] dark:text-[#8b949e]">
              <div className="flex justify-between"><span>{t("dashboard.active_jobs")}</span><span className="font-mono">{activeJobs}</span></div>
              <MiniBar value={activeJobs} max={Math.max(activeJobs, 5)} tone="info" />
            </div>
            <div className="card p-3 text-xs text-[#57606a] dark:text-[#8b949e]">
              <div className="flex justify-between"><span>{t("dashboard.failed")}</span><span className="font-mono">{data.queue.failed_download_count + data.queue.failed_import_count}</span></div>
              <MiniBar value={data.queue.failed_download_count + data.queue.failed_import_count} max={Math.max(data.queue.failed_download_count + data.queue.failed_import_count, 5)} tone="danger" />
            </div>
            <div className="card p-3 text-xs text-[#57606a] dark:text-[#8b949e]">
              <div className="flex justify-between"><span>{t("dashboard.disk_free")}</span><span className="font-mono">{data.storage.disk_free_percent ?? "—"}%</span></div>
              <MiniBar value={data.storage.disk_used_percent || 0} max={100} tone={data.storage.risk_level === "warning" ? "warning" : "ok"} />
            </div>
          </div>

          <section>
            <div className="mb-3 flex items-center justify-between gap-3">
              <h2 className="section-title">{t("dashboard.attention")}</h2>
              <Link href="/admin/scheduler" className="text-sm text-[#0969da] hover:underline dark:text-[#58a6ff]">{t("dashboard.open_scheduler")}</Link>
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
                    <div key={name} className="flex items-center justify-between gap-2 rounded-md border border-[#d8dee4] px-3 py-2 dark:border-[#30363d]">
                      <span className="truncate text-xs capitalize text-[#57606a] dark:text-[#8b949e]">{name}</span>
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
                    <Link key={href} href={href} className="rounded-md border border-[#d8dee4] px-3 py-2 text-sm font-medium hover:bg-[#f6f8fa] dark:border-[#30363d] dark:hover:bg-[#21262d]">{label}</Link>
                  ))}
                </div>
              </section>
            </aside>
          </section>
        </div>
      )}
    </main>
  );
}

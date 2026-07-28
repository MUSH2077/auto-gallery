"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import {
  Bell,
  CalendarClock,
  CircleGauge,
  Clock3,
  Code2,
  Copy,
  Database,
  GitBranch,
  GitMerge,
  Globe2,
  Home,
  Image,
  Inbox,
  Settings,
  Tag,
  Upload,
  UserRound,
  UsersRound,
  X,
  type LucideIcon,
} from "lucide-react";

import { api, queryKeys } from "@/lib/api";
import {
  ADMIN_LINK_MODULE,
  ADMIN_NAV_GROUPS,
  pathnameMatches,
  type AdminIconName,
} from "@/lib/adminNavigation";
import { useT } from "@/lib/i18n";
import { usePermissions } from "@/lib/usePermissions";
import SourceCodeLink from "@/components/SourceCodeLink";

const ICONS: Record<AdminIconName, LucideIcon> = {
  home: Home,
  image: Image,
  tag: Tag,
  upload: Upload,
  branch: GitBranch,
  copy: Copy,
  merge: GitMerge,
  globe: Globe2,
  person: UserRound,
  inbox: Inbox,
  code: Code2,
  clock: Clock3,
  calendar: CalendarClock,
  bell: Bell,
  database: Database,
  pulse: CircleGauge,
  gear: Settings,
  people: UsersRound,
};

function NavIcon({ name }: { name: AdminIconName }) {
  const Icon = ICONS[name];
  return <Icon className="h-[18px] w-[18px] shrink-0" strokeWidth={1.8} aria-hidden />;
}

function SidebarStatus({ enabled, compact }: { enabled: boolean; compact: boolean }) {
  const t = useT();
  const workbench = useQuery({
    queryKey: queryKeys.workbench,
    queryFn: api.workbench,
    enabled,
    refetchInterval: (query) => {
      const data = query.state.data;
      const active = (data?.queue.active_download_count || 0) + (data?.queue.active_import_count || 0);
      return active > 0 ? 10000 : 30000;
    },
  });
  if (!enabled || !workbench.data) return null;

  const queue = workbench.data.queue;
  const active = (queue.active_download_count || 0) + (queue.active_import_count || 0);
  const usedPercent = workbench.data.storage.disk_used_percent ?? null;
  const risk = workbench.data.storage.risk_level;
  const barColor = risk === "critical" ? "bg-danger" : risk === "warning" ? "bg-warning" : "bg-success";
  const statusLabel = active > 0
    ? t("sidebar.running", { downloads: queue.active_download_count, imports: queue.active_import_count })
    : t("sidebar.idle");

  if (compact) {
    return (
      <div className="flex justify-center border-t border-border py-3" title={statusLabel}>
        <span className={`h-2.5 w-2.5 rounded-full ${active > 0 ? "animate-pulse bg-accent" : "bg-success"}`} />
        <span className="sr-only">{statusLabel}</span>
      </div>
    );
  }

  return (
    <div className="grid gap-2 border-t border-border px-4 py-3 text-xs text-muted">
      <div className="flex min-w-0 items-center gap-2">
        <span className={`h-2 w-2 shrink-0 rounded-full ${active > 0 ? "animate-pulse bg-accent" : "bg-success"}`} />
        <span className="truncate">{statusLabel}</span>
      </div>
      {usedPercent !== null && (
        <div className="flex items-center gap-2">
          <span className="tabular shrink-0">{t("sidebar.disk", { percent: usedPercent })}</span>
          <span className="h-1 flex-1 overflow-hidden rounded-full bg-border">
            <i
              className={`block h-full w-full rounded-full ${barColor} transition-transform duration-slow`}
              style={{ transform: `scaleX(${Math.min(100, usedPercent) / 100})`, transformOrigin: "left" }}
            />
          </span>
        </div>
      )}
    </div>
  );
}

export default function AppSidebar({
  compact = false,
  onNavigate,
  onDismiss,
}: {
  compact?: boolean;
  onNavigate?: () => void;
  onDismiss?: () => void;
}) {
  const t = useT();
  const pathname = usePathname();
  const { has } = usePermissions();
  const canSeeStatus = has("system");
  const workbenchBadge = useQuery({
    queryKey: queryKeys.workbench,
    queryFn: api.workbench,
    enabled: canSeeStatus,
    staleTime: 10000,
  });
  const activeJobs = canSeeStatus
    ? (workbenchBadge.data?.queue.active_download_count || 0)
      + (workbenchBadge.data?.queue.active_import_count || 0)
    : 0;

  const groups = ADMIN_NAV_GROUPS
    .map((group) => ({
      ...group,
      links: group.links.filter(({ href }) => {
        const module = ADMIN_LINK_MODULE[href];
        return !module || has(module);
      }),
    }))
    .filter((group) => group.links.length > 0);

  return (
    <div className={`flex h-full flex-col ${compact ? "w-16" : "w-[248px]"}`}>
      <div className={`flex h-14 shrink-0 items-center border-b border-border ${compact ? "justify-center px-2" : "gap-2 px-4"}`}>
        <span className="flex h-7 w-7 shrink-0 items-center justify-center rounded-lg bg-accent text-[11px] font-bold tracking-tight text-white">
          ag
        </span>
        {!compact && <span className="min-w-0 truncate text-sm font-semibold tracking-tight text-fg">auto-gallery</span>}
        {onDismiss && !compact && (
          <button
            type="button"
            onClick={onDismiss}
            className="btn-icon ml-auto"
            aria-label={t("nav.close_sidebar")}
            title={t("nav.close_sidebar")}
          >
            <X className="h-4 w-4" strokeWidth={1.8} aria-hidden />
          </button>
        )}
      </div>

      <nav aria-label={t("nav.primary")} className={`min-h-0 flex-1 overflow-y-auto pb-2 ${compact ? "px-2 pt-2" : "px-2"}`}>
        {groups.map((group, groupIndex) => {
          const labelId = `sidebar-group-${groupIndex}`;
          return (
            <section
              key={group.labelKey}
              aria-labelledby={compact ? undefined : labelId}
              className={compact
                ? "border-t border-border/70 py-1 first:border-t-0"
                : "border-t border-border/70 pb-1 pt-3 first:border-t-0 first:pt-3"}
            >
              {!compact && (
                <h2 id={labelId} className="mx-2 mb-1 text-[11px] font-semibold uppercase tracking-[0.08em] text-muted">
                  {t(group.labelKey)}
                </h2>
              )}
              {group.links.map(({ href, labelKey, icon }) => {
                const active = pathnameMatches(pathname, href);
                const label = t(labelKey);
                return (
                  <Link
                    key={href}
                    href={href}
                    onClick={onNavigate}
                    aria-current={active ? "page" : undefined}
                    aria-label={compact ? label : undefined}
                    title={compact ? label : undefined}
                    className={`side-item ${compact ? "justify-center px-0" : ""} ${active ? "side-item-active" : ""}`}
                  >
                    <NavIcon name={icon} />
                    {!compact && <span className="truncate">{label}</span>}
                    {href === "/admin/jobs" && activeJobs > 0 && (
                      <span className={compact
                        ? "absolute right-0 top-0 h-2 w-2 rounded-full bg-accent ring-2 ring-surface"
                        : "tabular ml-auto rounded-full bg-accent px-1.5 text-[11px] font-medium leading-[18px] text-white"}
                      >
                        {compact ? <span className="sr-only">{activeJobs}</span> : activeJobs}
                      </span>
                    )}
                  </Link>
                );
              })}
            </section>
          );
        })}
      </nav>
      <SidebarStatus enabled={canSeeStatus} compact={compact} />
      <SourceCodeLink compact={compact} className="mx-2 mb-2" />
    </div>
  );
}

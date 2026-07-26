"use client";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import { api, queryKeys } from "@/lib/api";
import {
  ADMIN_LINK_MODULE,
  ADMIN_NAV_GROUPS,
  ADMIN_USERS_LINK,
  type AdminIconName,
} from "@/lib/adminNavigation";
import { useT } from "@/lib/i18n";
import { usePermissions } from "@/lib/usePermissions";

// 16px octicon-style glyphs (fill: currentColor).
function Icon({ name }: { name: AdminIconName }) {
  const paths: Record<AdminIconName, string> = {
    home: "M6.906.664a1.749 1.749 0 0 1 2.187 0l5.25 4.2c.415.332.657.835.657 1.367v7.019A1.75 1.75 0 0 1 13.25 15h-3.5a.75.75 0 0 1-.75-.75V9H7v5.25a.75.75 0 0 1-.75.75h-3.5A1.75 1.75 0 0 1 1 13.25V6.23c0-.531.242-1.034.657-1.366l5.25-4.2Z",
    image: "M0 2.75C0 1.784.784 1 1.75 1h12.5c.966 0 1.75.784 1.75 1.75v10.5A1.75 1.75 0 0 1 14.25 15H1.75A1.75 1.75 0 0 1 0 13.25Zm1.75-.25a.25.25 0 0 0-.25.25v10.5c0 .138.112.25.25.25h.94l6.06-6.06a.75.75 0 0 1 1.06 0l3.69 3.69V2.75a.25.25 0 0 0-.25-.25ZM5.5 4.5a1.5 1.5 0 1 1 0 3 1.5 1.5 0 0 1 0-3Z",
    tag: "M1 7.775V2.75C1 1.784 1.784 1 2.75 1h5.025c.464 0 .91.184 1.238.513l6.25 6.25a1.75 1.75 0 0 1 0 2.474l-5.026 5.026a1.75 1.75 0 0 1-2.474 0l-6.25-6.25A1.752 1.752 0 0 1 1 7.775Zm1.5 0c0 .066.026.13.073.177l6.25 6.25a.25.25 0 0 0 .354 0l5.025-5.025a.25.25 0 0 0 0-.354l-6.25-6.25a.25.25 0 0 0-.177-.073H2.75a.25.25 0 0 0-.25.25ZM6 5a1 1 0 1 1-2 0 1 1 0 0 1 2 0Z",
    upload: "M2.75 14A1.75 1.75 0 0 1 1 12.25v-2.5a.75.75 0 0 1 1.5 0v2.5c0 .138.112.25.25.25h10.5a.25.25 0 0 0 .25-.25v-2.5a.75.75 0 0 1 1.5 0v2.5A1.75 1.75 0 0 1 13.25 14ZM11.78 4.72a.749.749 0 1 1-1.06 1.06L8.75 3.811V9.5a.75.75 0 0 1-1.5 0V3.811L5.28 5.78a.749.749 0 1 1-1.06-1.06l3.25-3.25a.749.749 0 0 1 1.06 0l3.25 3.25Z",
    branch: "M9.5 3.25a2.25 2.25 0 1 1 3 2.122V6A2.5 2.5 0 0 1 10 8.5H6a1 1 0 0 0-1 1v1.128a2.251 2.251 0 1 1-1.5 0V5.372a2.25 2.25 0 1 1 1.5 0v1.836A2.493 2.493 0 0 1 6 7h4a1 1 0 0 0 1-1v-.628A2.25 2.25 0 0 1 9.5 3.25Z",
    copy: "M0 6.75C0 5.784.784 5 1.75 5h1.5a.75.75 0 0 1 0 1.5h-1.5a.25.25 0 0 0-.25.25v7.5c0 .138.112.25.25.25h7.5a.25.25 0 0 0 .25-.25v-1.5a.75.75 0 0 1 1.5 0v1.5A1.75 1.75 0 0 1 9.25 16h-7.5A1.75 1.75 0 0 1 0 14.25ZM5 1.75C5 .784 5.784 0 6.75 0h7.5C15.216 0 16 .784 16 1.75v7.5A1.75 1.75 0 0 1 14.25 11h-7.5A1.75 1.75 0 0 1 5 9.25Z",
    merge: "M5.45 5.154A4.25 4.25 0 0 0 9.25 7.5h1.378a2.251 2.251 0 1 1 0 1.5H9.25A5.734 5.734 0 0 1 5 7.123v3.505a2.25 2.25 0 1 1-1.5 0V5.372a2.25 2.25 0 1 1 1.95-.218Z",
    globe: "M8 0a8 8 0 1 1 0 16A8 8 0 0 1 8 0ZM5.78 8.75a9.64 9.64 0 0 0 1.363 4.177q.383.64.857 1.215.475-.576.858-1.215A9.64 9.64 0 0 0 10.22 8.75Zm4.44-1.5a9.64 9.64 0 0 0-1.363-4.177q-.383-.64-.857-1.215a9.9 9.9 0 0 0-.858 1.215A9.64 9.64 0 0 0 5.78 7.25Zm5.25 1.5h-2.302q-.128 2.42-1.106 4.31a6.51 6.51 0 0 0 3.408-4.31Zm0-1.5a6.51 6.51 0 0 0-3.408-4.31q.978 1.89 1.106 4.31ZM2.53 8.75a6.51 6.51 0 0 0 3.408 4.31q-.978-1.89-1.106-4.31Zm0-1.5h2.302q.128-2.42 1.106-4.31A6.51 6.51 0 0 0 2.53 7.25Z",
    person: "M10.561 8.073a6.005 6.005 0 0 1 3.432 5.142.75.75 0 1 1-1.498.07 4.5 4.5 0 0 0-8.99 0 .75.75 0 0 1-1.498-.07 6.004 6.004 0 0 1 3.431-5.142 3.999 3.999 0 1 1 5.123 0ZM10.5 5a2.5 2.5 0 1 0-5 0 2.5 2.5 0 0 0 5 0Z",
    inbox: "M2.8 2.06A1.75 1.75 0 0 1 4.41 1h7.18c.7 0 1.333.417 1.61 1.06l2.74 6.395c.04.093.06.194.06.295v4.5A1.75 1.75 0 0 1 14.25 15H1.75A1.75 1.75 0 0 1 0 13.25v-4.5c0-.101.02-.202.06-.295Zm1.61.44a.25.25 0 0 0-.23.152L1.887 8H4.75a.75.75 0 0 1 .6.3L6.625 10h2.75l1.275-1.7a.75.75 0 0 1 .6-.3h2.863L11.82 2.652a.25.25 0 0 0-.23-.152Z",
    code: "M4.72 3.22a.75.75 0 0 1 1.06 1.06L2.06 8l3.72 3.72a.749.749 0 1 1-1.06 1.06L.47 8.53a.75.75 0 0 1 0-1.06Zm6.56 0a.75.75 0 1 0-1.06 1.06L13.94 8l-3.72 3.72a.749.749 0 1 0 1.06 1.06l4.25-4.25a.75.75 0 0 0 0-1.06Z",
    clock: "M8 0a8 8 0 1 1 0 16A8 8 0 0 1 8 0ZM1.5 8a6.5 6.5 0 1 0 13 0 6.5 6.5 0 0 0-13 0Zm7-3.25v2.992l2.028 1.19a.75.75 0 0 1-.758 1.293l-2.5-1.467A.75.75 0 0 1 7 8.25v-3.5a.75.75 0 0 1 1.5 0Z",
    calendar: "M4.75 0a.75.75 0 0 1 .75.75V2h5V.75a.75.75 0 0 1 1.5 0V2h1.25c.966 0 1.75.784 1.75 1.75v10.5A1.75 1.75 0 0 1 13.25 16H2.75A1.75 1.75 0 0 1 1 14.25V3.75C1 2.784 1.784 2 2.75 2H4V.75A.75.75 0 0 1 4.75 0ZM2.5 7.5v6.75c0 .138.112.25.25.25h10.5a.25.25 0 0 0 .25-.25V7.5Zm10.75-4H2.75a.25.25 0 0 0-.25.25V6h11V3.75a.25.25 0 0 0-.25-.25Z",
    bell: "M8 16a2 2 0 0 0 1.985-1.75c.017-.137-.097-.25-.235-.25h-3.5c-.138 0-.252.113-.235.25A2 2 0 0 0 8 16ZM3 5a5 5 0 0 1 10 0v2.947c0 .05.015.098.042.139l1.703 2.555A1.519 1.519 0 0 1 13.482 13H2.518a1.516 1.516 0 0 1-1.263-2.36l1.703-2.554A.255.255 0 0 0 3 7.947Z",
    database: "M1 3.5c0-.626.292-1.165.7-1.59.406-.422.956-.767 1.579-1.041C4.525.32 6.195 0 8 0s3.475.32 4.722.869c.622.274 1.172.62 1.578 1.04.408.426.7.965.7 1.591v9c0 .626-.292 1.165-.7 1.59-.406.422-.956.767-1.579 1.041C11.476 15.68 9.806 16 8 16s-3.475-.32-4.721-.869c-.623-.274-1.173-.62-1.579-1.04-.408-.426-.7-.965-.7-1.591Zm1.5 0c0 .133.058.318.282.551.227.237.591.483 1.101.707C4.898 5.205 6.353 5.5 8 5.5s3.102-.295 4.117-.742c.51-.224.874-.47 1.101-.707.224-.233.282-.418.282-.551s-.058-.318-.282-.551c-.227-.237-.591-.483-1.101-.707C11.102 1.795 9.647 1.5 8 1.5s-3.102.295-4.117.742c-.51.224-.874.47-1.101.707-.224.233-.282.418-.282.551Z",
    pulse: "M6 2a.75.75 0 0 1 .696.471L10 10.731l1.304-3.26A.75.75 0 0 1 12 7h3.25a.75.75 0 0 1 0 1.5h-2.742l-1.812 4.528a.75.75 0 0 1-1.392 0L6 4.77 4.696 8.03A.75.75 0 0 1 4 8.5H.75a.75.75 0 0 1 0-1.5h2.742l1.812-4.529A.75.75 0 0 1 6 2Z",
    gear: "M8 0c1.036 0 1.875.84 1.875 1.875v.437c.372.117.727.264 1.062.44l.31-.31a1.875 1.875 0 0 1 2.651 2.652l-.309.309c.175.335.322.69.439 1.062h.437a1.875 1.875 0 0 1 0 3.75h-.437a5.9 5.9 0 0 1-.44 1.062l.31.31a1.875 1.875 0 1 1-2.652 2.651l-.309-.309a5.9 5.9 0 0 1-1.062.439v.437a1.875 1.875 0 0 1-3.75 0v-.437a5.9 5.9 0 0 1-1.062-.44l-.31.31a1.875 1.875 0 1 1-2.651-2.652l.309-.309a5.9 5.9 0 0 1-.439-1.062h-.437a1.875 1.875 0 0 1 0-3.75h.437c.117-.372.264-.727.44-1.062l-.31-.31a1.875 1.875 0 1 1 2.652-2.651l.309.309c.335-.175.69-.322 1.062-.439v-.437C6.125.839 6.965 0 8 0Zm0 5.25a2.75 2.75 0 1 0 0 5.5 2.75 2.75 0 0 0 0-5.5Z",
    people: "M2 5.5a3.5 3.5 0 1 1 5.898 2.549 5.508 5.508 0 0 1 3.034 4.084.75.75 0 1 1-1.482.235 4 4 0 0 0-7.9 0 .75.75 0 0 1-1.482-.236A5.507 5.507 0 0 1 3.102 8.05 3.493 3.493 0 0 1 2 5.5ZM11 4a3.001 3.001 0 0 1 2.22 5.018 5.01 5.01 0 0 1 2.56 3.012.749.749 0 0 1-.885.954.752.752 0 0 1-.549-.514 3.507 3.507 0 0 0-2.522-2.372.75.75 0 0 1-.574-.73v-.352a.75.75 0 0 1 .416-.672A1.5 1.5 0 0 0 11 5.5.75.75 0 0 1 11 4Z",
  };
  return (
    <svg viewBox="0 0 16 16" className="h-4 w-4 shrink-0" fill="currentColor" aria-hidden>
      <path d={paths[name]} />
    </svg>
  );
}

/** Machine-status footer: what the NAS is doing right now, always visible.
 *  workbench lives in the `system` module, so the block (and the jobs badge
 *  it feeds) only renders for users holding that permission. */
function SidebarStatus({ enabled }: { enabled: boolean }) {
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
  const q = workbench.data.queue;
  const active = (q.active_download_count || 0) + (q.active_import_count || 0);
  const usedPercent = workbench.data.storage.disk_used_percent ?? null;
  const risk = workbench.data.storage.risk_level;
  const barColor = risk === "critical" ? "bg-danger" : risk === "warning" ? "bg-warning" : "bg-success";
  return (
    <div className="grid gap-2 border-t border-border px-4 py-3 text-xs text-muted">
      <div className="flex items-center gap-2">
        <span className={`h-2 w-2 shrink-0 rounded-full ${active > 0 ? "animate-pulse bg-accent" : "bg-success"}`} />
        <span className="truncate">
          {active > 0
            ? t("sidebar.running", { downloads: q.active_download_count, imports: q.active_import_count })
            : t("sidebar.idle")}
        </span>
      </div>
      {usedPercent !== null && (
        <div className="flex items-center gap-2">
          <span className="tabular shrink-0">{t("sidebar.disk", { percent: usedPercent })}</span>
          <span className="h-1 flex-1 overflow-hidden rounded-full bg-border">
            <i className={`block h-full w-full rounded-full ${barColor} transition-transform duration-slow`}
              style={{ transform: `scaleX(${Math.min(100, usedPercent) / 100})`, transformOrigin: "left" }} />
          </span>
        </div>
      )}
    </div>
  );
}

export default function AppSidebar({
  onNavigate,
  onDismiss,
}: {
  onNavigate?: () => void;
  onDismiss?: () => void;
}) {
  const t = useT();
  const pathname = usePathname();
  const { isAdmin, has } = usePermissions();
  const canSeeStatus = has("system");
  const workbenchBadge = useQuery({
    queryKey: queryKeys.workbench,
    queryFn: api.workbench,
    enabled: canSeeStatus,
    staleTime: 10000,
  });
  const activeJobs = canSeeStatus
    ? (workbenchBadge.data?.queue.active_download_count || 0) + (workbenchBadge.data?.queue.active_import_count || 0)
    : 0;

  const groups = ADMIN_NAV_GROUPS
    .map((group) => ({
      ...group,
      links: [
        ...group.links.filter(({ href }) => {
          const module = ADMIN_LINK_MODULE[href];
          return !module || has(module);
        }),
        ...(group.labelKey === "nav.admin" && isAdmin ? [ADMIN_USERS_LINK] : []),
      ],
    }))
    .filter((group) => group.links.length > 0);

  const isActive = (href: string) => (href === "/admin" ? pathname === "/admin" : pathname.startsWith(href));

  return (
    <div className="flex h-full w-[264px] flex-col">
      <div className="flex items-center gap-2 px-4 pb-2 pt-4 text-sm font-semibold">
        <span className="flex h-[22px] w-[22px] items-center justify-center rounded-md bg-fg text-xs text-canvas">ag</span>
        auto-gallery
        {onDismiss && (
          <button
            type="button"
            onClick={onDismiss}
            className="btn-icon ml-auto"
            aria-label={t("nav.close_sidebar")}
            title={t("nav.close_sidebar")}
          >
            <svg viewBox="0 0 16 16" className="h-4 w-4" fill="currentColor" aria-hidden>
              <path d="M3.22 3.22a.75.75 0 0 1 1.06 0L8 6.94l3.72-3.72a.75.75 0 1 1 1.06 1.06L9.06 8l3.72 3.72a.75.75 0 1 1-1.06 1.06L8 9.06l-3.72 3.72a.75.75 0 0 1-1.06-1.06L6.94 8 3.22 4.28a.75.75 0 0 1 0-1.06Z" />
            </svg>
          </button>
        )}
      </div>
      <nav aria-label={t("nav.primary")} className="min-h-0 flex-1 overflow-y-auto px-2 pb-2">
        {groups.map((group, groupIndex) => {
          const labelId = `sidebar-group-${groupIndex}`;
          return (
          <section key={group.labelKey} aria-labelledby={labelId} className="border-t border-border/70 pb-1 pt-3 first:border-t-0 first:pt-1">
            <h2 id={labelId} className="mx-2 mb-1 text-[11px] font-semibold uppercase tracking-wider text-muted">
              {t(group.labelKey)}
            </h2>
            {group.links.map(({ href, labelKey, icon }) => (
              <Link key={href} href={href} onClick={onNavigate}
                aria-current={isActive(href) ? "page" : undefined}
                className={`side-item ${isActive(href) ? "side-item-active" : ""}`}>
                <Icon name={icon} />
                <span className="truncate">{t(labelKey)}</span>
                {href === "/admin/jobs" && activeJobs > 0 && (
                  <span className="tabular ml-auto rounded-full bg-accent px-1.5 text-[11px] font-medium leading-[18px] text-white">
                    {activeJobs}
                  </span>
                )}
              </Link>
            ))}
          </section>
          );
        })}
      </nav>
      <SidebarStatus enabled={canSeeStatus} />
    </div>
  );
}

export type AdminIconName =
  | "home" | "image" | "tag" | "upload" | "branch" | "copy" | "merge"
  | "globe" | "person" | "inbox" | "code" | "clock" | "calendar" | "bell"
  | "database" | "pulse" | "gear" | "people";

export interface AdminNavLink {
  href: string;
  labelKey: string;
  icon: AdminIconName;
}

export interface AdminNavGroup {
  labelKey: string;
  links: AdminNavLink[];
}

// Nav href -> permission module. Links with no entry (dashboard; users is
// separately gated by is_admin) are always shown once logged in.
export const ADMIN_LINK_MODULE: Record<string, string> = {
  "/admin/works": "library",
  "/admin/tags": "library",
  "/admin/creators": "library",
  "/admin/search": "library",
  "/admin/upload": "upload",
  "/admin/curation": "curation",
  "/admin/dedup": "curation",
  "/admin/merge-candidates": "curation",
  "/admin/sources": "subscriptions",
  "/admin/subscriptions": "subscriptions",
  "/admin/reference/danbooru": "subscriptions",
  "/admin/jobs": "tasks",
  "/admin/import-jobs": "tasks",
  "/admin/scheduler": "tasks",
  "/admin/notifications": "tasks",
  "/admin/data-mgmt": "system",
  "/admin/system": "system",
  "/admin/settings": "system",
};

export const ADMIN_NAV_GROUPS: AdminNavGroup[] = [
  {
    labelKey: "nav.overview",
    links: [{ href: "/admin", labelKey: "nav.dashboard", icon: "home" }],
  },
  {
    labelKey: "nav.library",
    links: [
      { href: "/admin/works", labelKey: "nav.works", icon: "image" },
      { href: "/admin/tags", labelKey: "nav.tags", icon: "tag" },
      { href: "/admin/upload", labelKey: "nav.upload", icon: "upload" },
      { href: "/admin/curation", labelKey: "nav.curation", icon: "branch" },
      { href: "/admin/dedup", labelKey: "nav.dedup", icon: "copy" },
      { href: "/admin/merge-candidates", labelKey: "nav.merge", icon: "merge" },
    ],
  },
  {
    labelKey: "nav.sources",
    links: [
      { href: "/admin/sources", labelKey: "nav.sources", icon: "globe" },
      { href: "/admin/creators", labelKey: "nav.creators", icon: "person" },
      { href: "/admin/subscriptions", labelKey: "nav.subscriptions", icon: "inbox" },
      { href: "/admin/reference/danbooru", labelKey: "nav.danbooru", icon: "code" },
    ],
  },
  {
    labelKey: "nav.operations",
    links: [
      { href: "/admin/jobs", labelKey: "nav.jobs", icon: "clock" },
      { href: "/admin/scheduler", labelKey: "nav.scheduler", icon: "calendar" },
      { href: "/admin/notifications", labelKey: "notifications.title", icon: "bell" },
    ],
  },
  {
    labelKey: "nav.admin",
    links: [
      { href: "/admin/data-mgmt", labelKey: "nav.datamgmt", icon: "database" },
      { href: "/admin/system", labelKey: "nav.system", icon: "pulse" },
      { href: "/admin/settings", labelKey: "nav.settings", icon: "gear" },
    ],
  },
];

export const ADMIN_USERS_LINK: AdminNavLink = {
  href: "/admin/users",
  labelKey: "nav.users",
  icon: "people",
};

function pathnameMatches(pathname: string, href: string): boolean {
  return href === "/admin" ? pathname === "/admin" : pathname.startsWith(href);
}

/** Longest-prefix navigation match used by the top bar and page hierarchy. */
export function findAdminNavEntry(pathname: string): {
  groupKey: string;
  labelKey: string;
} | null {
  let best: { groupKey: string; labelKey: string; len: number } | null = null;
  for (const group of ADMIN_NAV_GROUPS) {
    const links = group.labelKey === "nav.admin"
      ? [...group.links, ADMIN_USERS_LINK]
      : group.links;
    for (const link of links) {
      if (pathnameMatches(pathname, link.href) && (!best || link.href.length > best.len)) {
        best = {
          groupKey: group.labelKey,
          labelKey: link.labelKey,
          len: link.href.length,
        };
      }
    }
  }
  return best && { groupKey: best.groupKey, labelKey: best.labelKey };
}


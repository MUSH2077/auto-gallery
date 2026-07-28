export type AdminIconName =
  | "home"
  | "image"
  | "tag"
  | "upload"
  | "branch"
  | "copy"
  | "merge"
  | "globe"
  | "person"
  | "inbox"
  | "code"
  | "clock"
  | "calendar"
  | "bell"
  | "database"
  | "pulse"
  | "gear"
  | "people";

export type AdminNavContext =
  | "overview"
  | "library"
  | "source-management"
  | "operations"
  | "governance"
  | "settings";

export interface AdminNavLink {
  href: string;
  labelKey: string;
  icon: AdminIconName;
  context: AdminNavContext;
  keywords?: string[];
  primary?: boolean;
  adminOnly?: boolean;
}

export interface AdminNavGroup {
  labelKey: string;
  links: AdminNavLink[];
}

// Route -> permission module. Missing entries are available to every signed-in
// user; adminOnly links receive an additional is_admin check in consumers.
export const ADMIN_LINK_MODULE: Record<string, string> = {
  "/admin/works": "library",
  "/admin/tags": "library",
  "/admin/search": "library",
  "/admin/upload": "upload",
  "/admin/curation": "curation",
  "/admin/dedup": "curation",
  "/admin/merge-candidates": "curation",
  "/admin/sources": "subscriptions",
  "/admin/creators": "library",
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

const link = (
  href: string,
  labelKey: string,
  icon: AdminIconName,
  context: AdminNavContext,
  options: Pick<AdminNavLink, "keywords" | "primary" | "adminOnly"> = {},
): AdminNavLink => ({ href, labelKey, icon, context, ...options });

export const ADMIN_NAV_LINKS: AdminNavLink[] = [
  link("/admin", "nav.dashboard", "home", "overview", { primary: true, keywords: ["overview", "home", "概览"] }),
  link("/admin/works", "nav.works", "image", "library", { primary: true, keywords: ["gallery", "images", "图库"] }),
  link("/admin/tags", "nav.tags", "tag", "library", { primary: true, keywords: ["labels", "标签"] }),
  link("/admin/creators", "nav.creators", "person", "source-management", { primary: true, keywords: ["artists", "作者"] }),
  link("/admin/subscriptions", "nav.subscriptions", "inbox", "source-management", { primary: true, keywords: ["repositories", "repos", "订阅", "仓库"] }),
  link("/admin/jobs", "nav.jobs", "clock", "operations", { primary: true, keywords: ["tasks", "queue", "任务", "队列"] }),
  link("/admin/scheduler", "nav.scheduler", "calendar", "operations", { primary: true, keywords: ["schedule", "sync", "调度", "同步"] }),
  link("/admin/data-mgmt", "nav.datamgmt", "database", "governance", { primary: true, keywords: ["storage", "governance", "数据", "存储"] }),
  link("/admin/system", "nav.system", "pulse", "governance", { primary: true, keywords: ["health", "status", "健康"] }),
  link("/admin/settings", "nav.settings", "gear", "settings", { primary: true, keywords: ["config", "preferences", "配置"] }),

  link("/admin/upload", "nav.upload", "upload", "library", { keywords: ["import files", "上传"] }),
  link("/admin/search", "nav.search", "image", "library", { keywords: ["find", "搜索"] }),
  link("/admin/sources", "nav.sources", "globe", "source-management", { keywords: ["providers", "source", "来源"] }),
  link("/admin/reference/danbooru", "nav.danbooru", "code", "source-management", { keywords: ["reference", "mapping"] }),
  link("/admin/import-jobs", "nav.import", "branch", "operations", { keywords: ["imports", "导入任务"] }),
  link("/admin/notifications", "notifications.title", "bell", "operations", { keywords: ["alerts", "通知"] }),
  link("/admin/curation", "nav.curation", "branch", "governance", { keywords: ["history", "策展"] }),
  link("/admin/dedup", "nav.dedup", "copy", "governance", { keywords: ["duplicates", "查重"] }),
  link("/admin/merge-candidates", "nav.merge", "merge", "governance", { keywords: ["merge", "合并"] }),
  link("/admin/users", "nav.users", "people", "settings", { adminOnly: true, keywords: ["accounts", "权限", "用户"] }),
];

const byHref = (href: string) => ADMIN_NAV_LINKS.find((item) => item.href === href)!;

export const ADMIN_NAV_GROUPS: AdminNavGroup[] = [
  { labelKey: "nav.overview", links: [byHref("/admin")] },
  { labelKey: "nav.library", links: [byHref("/admin/works"), byHref("/admin/tags")] },
  { labelKey: "nav.sources", links: [byHref("/admin/creators"), byHref("/admin/subscriptions")] },
  { labelKey: "nav.operations", links: [byHref("/admin/jobs"), byHref("/admin/scheduler")] },
  { labelKey: "nav.admin", links: [byHref("/admin/data-mgmt"), byHref("/admin/system"), byHref("/admin/settings")] },
];

export const ADMIN_CONTEXT_LINKS: Record<AdminNavContext, AdminNavLink[]> = {
  overview: [byHref("/admin")],
  library: [
    byHref("/admin/works"),
    byHref("/admin/tags"),
    byHref("/admin/upload"),
  ],
  "source-management": [
    byHref("/admin/creators"),
    byHref("/admin/subscriptions"),
    byHref("/admin/sources"),
    byHref("/admin/reference/danbooru"),
  ],
  operations: [
    byHref("/admin/jobs"),
    byHref("/admin/scheduler"),
    byHref("/admin/notifications"),
  ],
  governance: [
    byHref("/admin/data-mgmt"),
    byHref("/admin/curation"),
    byHref("/admin/dedup"),
    byHref("/admin/merge-candidates"),
    byHref("/admin/system"),
  ],
  settings: [
    byHref("/admin/settings"),
    byHref("/admin/users"),
  ],
};

export const ADMIN_USERS_LINK = byHref("/admin/users");

export function pathnameMatches(pathname: string, href: string): boolean {
  return href === "/admin" ? pathname === "/admin" : pathname.startsWith(href);
}

export function findAdminNavLink(pathname: string): AdminNavLink | null {
  let best: AdminNavLink | null = null;
  for (const item of ADMIN_NAV_LINKS) {
    if (pathnameMatches(pathname, item.href) && (!best || item.href.length > best.href.length)) {
      best = item;
    }
  }
  // Settings detail routes intentionally inherit the settings context.
  if (!best && pathname.startsWith("/admin/settings/")) return byHref("/admin/settings");
  return best;
}

/** Longest-prefix navigation match used by the top bar and page hierarchy. */
export function findAdminNavEntry(pathname: string): {
  groupKey: string;
  labelKey: string;
} | null {
  const current = findAdminNavLink(pathname);
  if (!current) return null;
  const group = ADMIN_NAV_GROUPS.find((item) =>
    item.links.some((candidate) => candidate.href === current.href)
    || item.links.some((candidate) => candidate.context === current.context),
  );
  return {
    groupKey: group?.labelKey || "nav.admin",
    labelKey: current.labelKey,
  };
}

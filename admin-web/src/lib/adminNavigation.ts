import { adminRoutes } from "@/lib/adminRoutes";

export type AdminIconName =
  | "home"
  | "image"
  | "tag"
  | "upload"
  | "branch"
  | "copy"
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
  | "ingestion"
  | "source-management"
  | "operations"
  | "notifications"
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
  topbarOnly?: boolean;
}

export interface AdminNavGroup {
  labelKey: string;
  links: AdminNavLink[];
}

export type AdminPermissionRequirement = string | readonly string[];

// Route -> permission module. Missing entries are available to every signed-in
// user; adminOnly links receive an additional is_admin check in consumers.
export const ADMIN_LINK_MODULE: Record<string, AdminPermissionRequirement> = {
  [adminRoutes.works]: "library",
  [adminRoutes.tags]: "library",
  [adminRoutes.search]: "library",
  [adminRoutes.upload]: "upload",
  [adminRoutes.curation]: "curation",
  [adminRoutes.dedup]: "curation",
  [adminRoutes.creators]: "library",
  [adminRoutes.subscriptions]: "subscriptions",
  [adminRoutes.danbooru]: "subscriptions",
  [adminRoutes.jobs]: "tasks",
  [adminRoutes.scheduler]: "tasks",
  [adminRoutes.notifications]: "tasks",
  [adminRoutes.dataManagement]: "system",
  [adminRoutes.system]: ["system", "subscriptions"],
  [adminRoutes.settings]: "system",
};

export function hasAdminPermission(
  requirement: AdminPermissionRequirement | undefined,
  has: (module: string) => boolean,
): boolean {
  if (!requirement) return true;
  if (typeof requirement === "string") return has(requirement);
  return requirement.some((module) => has(module));
}

const link = (
  href: string,
  labelKey: string,
  icon: AdminIconName,
  context: AdminNavContext,
  options: Pick<AdminNavLink, "keywords" | "primary" | "adminOnly" | "topbarOnly"> = {},
): AdminNavLink => ({ href, labelKey, icon, context, ...options });

export const ADMIN_NAV_LINKS: AdminNavLink[] = [
  link(adminRoutes.dashboard, "nav.dashboard", "home", "overview", { primary: true, keywords: ["overview", "home", "概览"] }),
  link(adminRoutes.works, "nav.works", "image", "library", { primary: true, keywords: ["gallery", "images", "图库"] }),
  link(adminRoutes.tags, "nav.tags", "tag", "library", { primary: true, keywords: ["labels", "标签"] }),
  link(adminRoutes.upload, "nav.upload", "upload", "ingestion", { primary: true, keywords: ["import files", "上传"] }),
  link(adminRoutes.danbooru, "nav.danbooru", "code", "ingestion", { primary: true, keywords: ["reference", "mapping"] }),
  link(adminRoutes.creators, "nav.creators", "person", "source-management", { primary: true, keywords: ["artists", "作者"] }),
  link(adminRoutes.subscriptions, "nav.subscriptions", "inbox", "source-management", { primary: true, keywords: ["repositories", "repos", "订阅", "仓库"] }),
  link(adminRoutes.jobs, "nav.jobs", "clock", "operations", {
    primary: true,
    keywords: ["tasks", "queue", "imports", "任务", "队列", "导入任务"],
  }),
  link(adminRoutes.scheduler, "nav.scheduler", "calendar", "operations", { primary: true, keywords: ["schedule", "sync", "调度", "同步"] }),
  link(adminRoutes.dataManagement, "nav.datamgmt", "database", "governance", { primary: true, keywords: ["storage", "governance", "数据", "存储"] }),
  link(adminRoutes.system, "nav.system", "pulse", "governance", {
    primary: true,
    keywords: ["health", "status", "source", "provider", "健康", "数据源"],
  }),
  link(adminRoutes.settings, "nav.settings", "gear", "settings", { primary: true, keywords: ["config", "preferences", "配置"] }),

  link(adminRoutes.search, "nav.search", "image", "library", { keywords: ["find", "搜索"] }),
  link(adminRoutes.notifications, "notifications.title", "bell", "notifications", {
    keywords: ["alerts", "通知"],
    topbarOnly: true,
  }),
  link(adminRoutes.curation, "nav.curation", "branch", "governance", { keywords: ["history", "策展"] }),
  link(adminRoutes.dedup, "nav.dedup", "copy", "governance", {
    keywords: ["duplicates", "merge", "candidate", "查重", "合并候选"],
  }),
  link(adminRoutes.users, "nav.users", "people", "settings", { adminOnly: true, keywords: ["accounts", "权限", "用户"] }),
];

const byHref = (href: string) => ADMIN_NAV_LINKS.find((item) => item.href === href)!;

export const ADMIN_NAV_GROUPS: AdminNavGroup[] = [
  { labelKey: "nav.library", links: [byHref(adminRoutes.works), byHref(adminRoutes.tags)] },
  { labelKey: "nav.ingestion", links: [byHref(adminRoutes.upload), byHref(adminRoutes.danbooru)] },
  { labelKey: "nav.sources", links: [byHref(adminRoutes.creators), byHref(adminRoutes.subscriptions)] },
  { labelKey: "nav.operations", links: [byHref(adminRoutes.jobs), byHref(adminRoutes.scheduler)] },
  { labelKey: "nav.admin", links: [byHref(adminRoutes.dataManagement), byHref(adminRoutes.system), byHref(adminRoutes.settings)] },
];

export const ADMIN_CONTEXT_LINKS: Record<AdminNavContext, AdminNavLink[]> = {
  overview: [byHref(adminRoutes.dashboard)],
  library: [
    byHref(adminRoutes.works),
    byHref(adminRoutes.tags),
  ],
  ingestion: [
    byHref(adminRoutes.upload),
    byHref(adminRoutes.danbooru),
  ],
  "source-management": [
    byHref(adminRoutes.creators),
    byHref(adminRoutes.subscriptions),
  ],
  operations: [
    byHref(adminRoutes.jobs),
    byHref(adminRoutes.scheduler),
  ],
  notifications: [byHref(adminRoutes.notifications)],
  governance: [
    byHref(adminRoutes.dataManagement),
    byHref(adminRoutes.curation),
    byHref(adminRoutes.dedup),
    byHref(adminRoutes.system),
  ],
  settings: [
    byHref(adminRoutes.settings),
    byHref(adminRoutes.users),
  ],
};

export const ADMIN_USERS_LINK = byHref(adminRoutes.users);

export function pathnameMatches(pathname: string, href: string): boolean {
  return href === adminRoutes.dashboard ? pathname === adminRoutes.dashboard : pathname.startsWith(href);
}

export function findAdminNavLink(pathname: string): AdminNavLink | null {
  let best: AdminNavLink | null = null;
  for (const item of ADMIN_NAV_LINKS) {
    if (pathnameMatches(pathname, item.href) && (!best || item.href.length > best.href.length)) {
      best = item;
    }
  }
  // Settings detail routes intentionally inherit the settings context.
  if (!best && pathname.startsWith(`${adminRoutes.settings}/`)) return byHref(adminRoutes.settings);
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
    groupKey: group?.labelKey || current.labelKey,
    labelKey: current.labelKey,
  };
}

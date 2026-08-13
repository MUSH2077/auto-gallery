/**
 * Canonical admin route interface.
 *
 * Navigation, breadcrumbs, notifications, and entity links should use this
 * module instead of assembling paths locally. Legacy routes live separately
 * so compatibility adapters cannot accidentally become new link targets.
 */
export const adminRoutes = {
  dashboard: "/admin",
  works: "/admin/works",
  work: (id: string) => `/admin/works/${encodeURIComponent(id)}`,
  tags: "/admin/tags",
  tag: (id: string | number) => `/admin/tags/${encodeURIComponent(String(id))}`,
  search: "/admin/search",

  upload: "/admin/upload",
  danbooru: "/admin/upload/danbooru",

  creators: "/admin/creators",
  creator: (id: string) => `/admin/creators/${encodeURIComponent(id)}`,
  creatorMapping: (id: string) => `/admin/creators/${encodeURIComponent(id)}/mapping`,
  creatorDuplicates: "/admin/creators/duplicates",
  subscriptions: "/admin/subscriptions",
  subscription: (id: string) => `/admin/subscriptions/${encodeURIComponent(id)}`,
  repository: (id: string) => `/admin/subscriptions/repositories/${encodeURIComponent(id)}`,

  jobs: "/admin/jobs",
  scheduler: "/admin/scheduler",
  schedulerAuth: "/admin/scheduler#auth-status",
  notifications: "/admin/notifications",

  dataManagement: "/admin/data-mgmt",
  curation: "/admin/data-mgmt/curation",
  dedup: "/admin/data-mgmt/dedup",
  system: "/admin/system",

  profile: "/admin/profile",
  settings: "/admin/settings",
  settingsSection: (section: string) => `/admin/settings/${encodeURIComponent(section)}`,
  users: "/admin/settings/users",
  user: (id: string | number) => `/admin/settings/users/${encodeURIComponent(String(id))}`,
} as const;

export const legacyAdminRoutes = {
  danbooru: "/admin/reference/danbooru",
  repository: (id: string) => `/admin/repositories/${encodeURIComponent(id)}`,
  curation: "/admin/curation",
  dedup: "/admin/dedup",
  users: "/admin/users",
  user: (id: string | number) => `/admin/users/${encodeURIComponent(String(id))}`,
  mergeCandidates: "/admin/merge-candidates",
  sources: "/admin/sources",
  importJobs: "/admin/import-jobs",
  settingsDataManagement: "/admin/settings/data-mgmt",
  profile: "/admin/settings/profile",
  authStatus: "/admin/settings/auth-status",
} as const;

export interface AdminBreadcrumbParent {
  labelKey: string;
  href: string;
}

/**
 * Parent hierarchy for pages whose location cannot be expressed by the
 * sidebar's flat navigation group alone.
 */
export function adminBreadcrumbParents(pathname: string): AdminBreadcrumbParent[] {
  if (pathname === adminRoutes.profile) {
    return [{ labelKey: "nav.dashboard", href: adminRoutes.dashboard }];
  }
  if (pathname === adminRoutes.users) {
    return [{ labelKey: "settings.title", href: adminRoutes.settings }];
  }
  if (pathname.startsWith(`${adminRoutes.users}/`)) {
    return [
      { labelKey: "settings.title", href: adminRoutes.settings },
      { labelKey: "users.title", href: adminRoutes.users },
    ];
  }
  if (pathname.startsWith(`${adminRoutes.settings}/`)) {
    return [{ labelKey: "settings.title", href: adminRoutes.settings }];
  }
  if (
    pathname !== adminRoutes.subscriptions
    && pathname.startsWith(`${adminRoutes.subscriptions}/`)
    && !pathname.startsWith(`${adminRoutes.subscriptions}/repositories/`)
  ) {
    return [{ labelKey: "subscriptions.title", href: adminRoutes.subscriptions }];
  }
  return [];
}

const SETTINGS_PAGE_TITLE_KEYS: Readonly<Record<string, string>> = {
  [adminRoutes.settings]: "settings.title",
  [adminRoutes.settingsSection("appearance")]: "appearance.title",
  [adminRoutes.settingsSection("backup")]: "backup.title",
  [adminRoutes.settingsSection("dedup")]: "dedup.title",
  [adminRoutes.settingsSection("download-defaults")]: "dldefaults.title",
  [adminRoutes.settingsSection("gallerydl")]: "gallerydl.title",
  [adminRoutes.settingsSection("gitllery")]: "gitllery_settings.title",
  [adminRoutes.settingsSection("logs")]: "logs.title",
  [adminRoutes.settingsSection("proxy")]: "proxy.title",
  [adminRoutes.settingsSection("scheduler-defaults")]: "scheduler.defaults_title",
  [adminRoutes.settingsSection("slideshow")]: "slideshow_settings.title",
  [adminRoutes.settingsSection("subscription-defaults")]: "subdefaults.title",
  [adminRoutes.users]: "users.title",
  [adminRoutes.profile]: "auth.profile",
};

export function adminPageTitleKey(pathname: string): string | null {
  if (pathname.startsWith(`${adminRoutes.users}/`)) return "user_detail.title";
  return SETTINGS_PAGE_TITLE_KEYS[pathname] || null;
}

export interface LegacyAdminRedirect {
  pathname: string;
  query?: Readonly<Record<string, string>>;
  hash?: string;
}

/**
 * Resolve compatibility URLs before the React tree begins streaming.
 *
 * Server-component redirects can become a 200 response plus a client redirect
 * once a parent layout has started streaming. The proxy consumes this matcher
 * so legacy HTTP requests receive a real 308 while the compatibility pages
 * remain a defensive fallback.
 */
export function resolveLegacyAdminRoute(pathname: string): LegacyAdminRedirect | null {
  const exact: Record<string, LegacyAdminRedirect> = {
    [legacyAdminRoutes.danbooru]: { pathname: adminRoutes.danbooru },
    [legacyAdminRoutes.curation]: { pathname: adminRoutes.curation },
    [legacyAdminRoutes.dedup]: { pathname: adminRoutes.dedup },
    [legacyAdminRoutes.users]: { pathname: adminRoutes.users },
    [legacyAdminRoutes.mergeCandidates]: {
      pathname: adminRoutes.dedup,
      query: { status: "pending" },
    },
    [legacyAdminRoutes.sources]: {
      pathname: adminRoutes.system,
      query: { tab: "sources" },
    },
    [legacyAdminRoutes.importJobs]: {
      pathname: adminRoutes.jobs,
      query: { tab: "imports" },
    },
    [legacyAdminRoutes.settingsDataManagement]: {
      pathname: adminRoutes.dataManagement,
    },
    [legacyAdminRoutes.profile]: {
      pathname: adminRoutes.profile,
    },
    [legacyAdminRoutes.authStatus]: {
      pathname: adminRoutes.scheduler,
      hash: "auth-status",
    },
  };
  if (exact[pathname]) return exact[pathname];

  const repository = pathname.match(/^\/admin\/repositories\/([^/]+)$/);
  if (repository) {
    return { pathname: `/admin/subscriptions/repositories/${repository[1]}` };
  }
  const user = pathname.match(/^\/admin\/users\/([^/]+)$/);
  if (user) {
    return { pathname: `/admin/settings/users/${user[1]}` };
  }
  return null;
}

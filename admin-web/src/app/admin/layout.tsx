"use client";
import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { useRouter, usePathname } from "next/navigation";
import { useT } from "@/lib/i18n";
import { ThemeToggle, LangToggle, PaletteToggle } from "@/lib/theme";
import { useAuth } from "@/lib/auth";
import ErrorBoundary from "@/components/ErrorBoundary";
import { NotificationBell } from "@/components/NotificationCenter";

export const dynamic = 'force-dynamic';

function UserMenu() {
  const t = useT();
  const { user, logout } = useAuth();
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    function handleClick(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    }
    document.addEventListener("mousedown", handleClick);
    return () => document.removeEventListener("mousedown", handleClick);
  }, []);

  return (
    <div ref={ref} className="relative">
      <button
        onClick={() => setOpen(!open)}
        className="flex items-center gap-1.5 px-2 py-1 rounded-md hover:bg-[#21262d] transition-colors text-sm"
        title={user?.display_name || user?.username}
      >
        <svg className="w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <circle cx="12" cy="8" r="4" />
          <path d="M4 20c0-4 3.6-7 8-7s8 3 8 7" />
        </svg>
        <span className="hidden sm:inline max-w-[100px] truncate">{user?.display_name || user?.username}</span>
      </button>
      {open && (
        <div className="popover absolute right-0 mt-1 w-44 bg-white dark:bg-[#161b22] border border-[#d8dee4] dark:border-[#30363d] rounded-md shadow-overlay dark:shadow-overlay-dark z-50 text-[#24292f] dark:text-[#e6edf3] text-sm overflow-hidden">
          <Link
            href="/admin/settings/profile"
            onClick={() => setOpen(false)}
            className="flex items-center gap-2 px-3 py-2 hover:bg-[#f6f8fa] dark:hover:bg-[#21262d] transition-colors"
          >
            <svg className="w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M12 20h9M16.5 3.5a2.121 2.121 0 013 3L7 19l-4 1 1-4L16.5 3.5z" />
            </svg>
            {t("auth.change_password")}
          </Link>
          <button
            onClick={() => { setOpen(false); logout(); }}
            className="w-full flex items-center gap-2 px-3 py-2 hover:bg-[#f6f8fa] dark:hover:bg-[#21262d] transition-colors text-left text-[#cf222e] dark:text-[#f85149]"
          >
            <svg className="w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M9 21H5a2 2 0 01-2-2V5a2 2 0 012-2h4M16 17l5-5-5-5M21 12H9" />
            </svg>
            {t("auth.logout")}
          </button>
        </div>
      )}
    </div>
  );
}

function AdminNav() {
  const t = useT();
  const pathname = usePathname();
  const [open, setOpen] = useState(false);
  const groups = [
    { label: t("nav.dashboard"), links: [["/admin", t("nav.dashboard")]] },
    { label: t("nav.library", "图库"), links: [["/admin/works", t("nav.works")], ["/admin/tags", t("nav.tags")], ["/admin/curation", t("nav.curation", "Curation")], ["/admin/dedup", t("nav.dedup", "作品查重")], ["/admin/merge-candidates", t("nav.merge", "合并候选")]] },
    { label: t("nav.sources"), links: [["/admin/sources", t("nav.sources")], ["/admin/creators", t("nav.creators")], ["/admin/subscriptions", t("nav.subscriptions")], ["/admin/reference/danbooru", t("nav.danbooru")]] },
    { label: t("nav.operations", "任务"), links: [["/admin/jobs", t("nav.jobs")], ["/admin/scheduler", t("nav.scheduler")], ["/admin/notifications", t("notifications.title")]] },
    { label: t("nav.admin", "管理"), links: [["/admin/data-mgmt", t("nav.datamgmt")], ["/admin/system", t("nav.system", "系统状态")], ["/admin/settings", t("nav.settings")]] },
  ];
  const links = groups.flatMap((group) => group.links);
  const isActive = (href: string) => pathname === href || (href !== "/admin" && pathname.startsWith(href));

  return (
    <nav className="bg-nav text-nav-fg px-4 sm:px-6 py-3 border-b border-nav-fg/15">
      <div className="flex items-center gap-4 text-sm">
        <Link href="/admin" className="font-semibold text-base shrink-0 tracking-tight">auto-gallery</Link>
        {/* Desktop links */}
        <div className="hidden md:flex items-center gap-2 flex-1 flex-wrap">
          {groups.map((group) => (
            <div key={group.label} className="flex items-center gap-1 rounded-lg bg-nav-fg/[0.04] px-1 py-1">
              {group.links.map(([href, label]) => (
                <Link key={href} href={href}
                    className={`rounded-md px-2 py-1 transition-colors ${isActive(href) ? "bg-nav-fg/10 text-nav-fg font-medium shadow-sm" : "text-nav-fg/70 hover:bg-nav-fg/10 hover:text-nav-fg"}`}>{label}</Link>
              ))}
            </div>
          ))}
        </div>
        <div className="flex items-center gap-1 shrink-0 ml-auto">
          <Link href="/admin/search" className="p-1.5 rounded-md hover:bg-nav-fg/10 transition-colors" title={t("nav.search")} aria-label={t("nav.search")}>
            <svg className="w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <circle cx="11" cy="11" r="8" />
              <path d="M21 21l-4.3-4.3" />
            </svg>
          </Link>
          <NotificationBell />
          <LangToggle />
          <PaletteToggle />
          <ThemeToggle />
          <UserMenu />
          {/* Hamburger — mobile only */}
          <button onClick={() => setOpen(!open)} className="md:hidden p-1.5 rounded-md hover:bg-nav-fg/10" aria-label="Toggle navigation menu">
            <svg className="w-5 h-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              {open ? <path d="M18 6L6 18M6 6l12 12" /> : <path d="M4 6h16M4 12h16M4 18h16" />}
            </svg>
          </button>
        </div>
      </div>
      {/* Mobile menu */}
      {open && (
        <div className="md:hidden mt-3 pt-3 border-t border-nav-fg/20 flex flex-col gap-1 pb-1">
          {groups.map((group) => (
            <div key={group.label} className="py-1">
              <div className="px-2 pb-1 text-[10px] font-semibold uppercase tracking-wide text-nav-fg/50">{group.label}</div>
              {group.links.map(([href, label]) => (
                <Link key={href} href={href} onClick={() => setOpen(false)}
                  className={`block hover:bg-nav-fg/10 hover:text-nav-fg rounded-md transition-colors px-2 py-1 text-sm ${isActive(href) ? "bg-nav-fg/10 text-nav-fg" : ""}`}>{label}</Link>
              ))}
            </div>
          ))}
          <Link href="/admin/search" onClick={() => setOpen(false)}
            className="hover:bg-nav-fg/10 hover:text-nav-fg rounded-md transition-colors px-2 py-1 text-sm">{t("nav.search")}</Link>
        </div>
      )}
    </nav>
  );
}

function AuthGuard({ children }: { children: React.ReactNode }) {
  const { isAuthenticated, isLoading, user } = useAuth();
  const router = useRouter();
  const pathname = usePathname();

  useEffect(() => {
    if (!isLoading && !isAuthenticated && pathname !== "/admin/login") {
      router.replace("/admin/login");
      return;
    }
    if (
      !isLoading
      && isAuthenticated
      && user?.must_change_password
      && pathname !== "/admin/settings/profile"
      && pathname !== "/admin/login"
    ) {
      router.replace("/admin/settings/profile");
    }
  }, [isAuthenticated, isLoading, pathname, router, user?.must_change_password]);

  if (isLoading) {
    return (
      <div className="min-h-screen flex items-center justify-center bg-slate-50 dark:bg-slate-900">
        <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-slate-600" />
      </div>
    );
  }

  if (!isAuthenticated && pathname !== "/admin/login") return null;

  return <>{children}</>;
}

export default function AdminLayout({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const isLoginPage = pathname === "/admin/login";

  if (isLoginPage) {
    return <ErrorBoundary>{children}</ErrorBoundary>;
  }

  return (
    <ErrorBoundary>
      <AuthGuard>
        <div>
          <a href="#main-content" className="sr-only focus:not-sr-only focus:absolute focus:top-2 focus:left-2 focus:z-50 focus:px-4 focus:py-2 focus:bg-slate-900 focus:text-nav-fg focus:rounded">Skip to content</a>
          <AdminNav />
          <main id="main-content" className="min-h-[calc(100vh-52px)]">{children}</main>
        </div>
      </AuthGuard>
    </ErrorBoundary>
  );
}

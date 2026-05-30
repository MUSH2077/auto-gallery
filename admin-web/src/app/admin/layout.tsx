"use client";
import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { useRouter, usePathname } from "next/navigation";
import { useT } from "@/lib/i18n";
import { ThemeToggle, LangToggle } from "@/lib/theme";
import { useAuth } from "@/lib/auth";
import ErrorBoundary from "@/components/ErrorBoundary";

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
        className="flex items-center gap-1.5 px-2 py-1 rounded hover:bg-white/10 transition-colors text-sm"
        title={user?.display_name || user?.username}
      >
        <svg className="w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <circle cx="12" cy="8" r="4" />
          <path d="M4 20c0-4 3.6-7 8-7s8 3 8 7" />
        </svg>
        <span className="hidden sm:inline max-w-[100px] truncate">{user?.display_name || user?.username}</span>
      </button>
      {open && (
        <div className="absolute right-0 mt-1 w-44 bg-white dark:bg-slate-800 border border-slate-200 dark:border-slate-700 rounded shadow-lg z-50 text-slate-800 dark:text-slate-100 text-sm overflow-hidden">
          <Link
            href="/admin/settings/profile"
            onClick={() => setOpen(false)}
            className="flex items-center gap-2 px-3 py-2 hover:bg-slate-100 dark:hover:bg-slate-700 transition-colors"
          >
            <svg className="w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M12 20h9M16.5 3.5a2.121 2.121 0 013 3L7 19l-4 1 1-4L16.5 3.5z" />
            </svg>
            {t("auth.change_password")}
          </Link>
          <button
            onClick={() => { setOpen(false); logout(); }}
            className="w-full flex items-center gap-2 px-3 py-2 hover:bg-slate-100 dark:hover:bg-slate-700 transition-colors text-left text-red-500 dark:text-red-400"
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
  const [open, setOpen] = useState(false);
  const links = [
    ["/admin", t("nav.dashboard")],
    ["/admin/sources", t("nav.sources")],
    ["/admin/creators", t("nav.creators")],
    ["/admin/subscriptions", t("nav.subscriptions")],
    ["/admin/jobs", t("nav.jobs")],
    ["/admin/works", t("nav.works")],
    ["/admin/tags", t("nav.tags")],
    ["/admin/scheduler", t("nav.scheduler")],
    ["/admin/reference/danbooru", t("nav.danbooru")],
    ["/admin/settings", t("nav.settings")],
  ];

  return (
    <nav className="bg-slate-900 text-white px-4 sm:px-6 py-3">
      <div className="flex items-center gap-4 text-sm">
        <Link href="/admin" className="font-bold text-base shrink-0">auto-gallery</Link>
        {/* Desktop links */}
        <div className="hidden md:flex items-center gap-3 flex-1 flex-wrap">
          {links.map(([href, label]) => (
            <Link key={href} href={href} className="hover:text-gray-300 transition-colors">{label}</Link>
          ))}
        </div>
        <div className="flex items-center gap-1 shrink-0 ml-auto">
          <Link href="/admin/search" className="p-1.5 rounded hover:bg-white/10 transition-colors" title={t("nav.search")}>
            <svg className="w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <circle cx="11" cy="11" r="8" />
              <path d="M21 21l-4.3-4.3" />
            </svg>
          </Link>
          <LangToggle />
          <ThemeToggle />
          <UserMenu />
          {/* Hamburger — mobile only */}
          <button onClick={() => setOpen(!open)} className="md:hidden p-1.5 rounded hover:bg-white/10">
            <svg className="w-5 h-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              {open ? <path d="M18 6L6 18M6 6l12 12" /> : <path d="M4 6h16M4 12h16M4 18h16" />}
            </svg>
          </button>
        </div>
      </div>
      {/* Mobile menu */}
      {open && (
        <div className="md:hidden mt-3 pt-3 border-t border-slate-700 flex flex-col gap-2 pb-1">
          {links.map(([href, label]) => (
            <Link key={href} href={href} onClick={() => setOpen(false)}
              className="hover:text-gray-300 transition-colors px-1 py-1 text-sm">{label}</Link>
          ))}
          <Link href="/admin/search" onClick={() => setOpen(false)}
            className="hover:text-gray-300 transition-colors px-1 py-1 text-sm">{t("nav.search")}</Link>
        </div>
      )}
    </nav>
  );
}

function AuthGuard({ children }: { children: React.ReactNode }) {
  const { isAuthenticated, isLoading } = useAuth();
  const router = useRouter();
  const pathname = usePathname();

  useEffect(() => {
    if (!isLoading && !isAuthenticated && pathname !== "/admin/login") {
      router.replace("/admin/login");
    }
  }, [isAuthenticated, isLoading, pathname, router]);

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
          <AdminNav />
          <div className="min-h-[calc(100vh-52px)]">{children}</div>
        </div>
      </AuthGuard>
    </ErrorBoundary>
  );
}

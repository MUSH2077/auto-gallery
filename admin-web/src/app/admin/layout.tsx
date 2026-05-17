"use client";
import { useState } from "react";
import Link from "next/link";
import { useT } from "@/lib/i18n";
import { ThemeToggle, LangToggle } from "@/lib/theme";

export const dynamic = 'force-dynamic';

function AdminNav() {
  const t = useT();
  const [open, setOpen] = useState(false);
  const links = [
    ["/admin", t("nav.dashboard")],
    ["/admin/sources", t("nav.sources")],
    ["/admin/creators", t("nav.creators")],
    ["/admin/subscriptions", t("nav.subscriptions")],
    ["/admin/downloads", t("nav.downloads")],
    ["/admin/works", t("nav.works")],
    ["/admin/tags", t("nav.tags")],
    ["/admin/scheduler", t("nav.scheduler")],
    ["/admin/import-jobs", t("nav.import")],
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
          <LangToggle />
          <ThemeToggle />
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
        </div>
      )}
    </nav>
  );
}

export default function AdminLayout({ children }: { children: React.ReactNode }) {
  return (
    <div>
      <AdminNav />
      <div className="min-h-[calc(100vh-52px)]">{children}</div>
    </div>
  );
}

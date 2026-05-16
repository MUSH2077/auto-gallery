"use client";
import Link from "next/link";
import { useT } from "@/lib/i18n";
import { ThemeToggle, LangToggle } from "@/lib/theme";

export const dynamic = 'force-dynamic';

function AdminNav() {
  const t = useT();
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
    <nav className="bg-slate-900 text-white px-6 py-3 flex items-center gap-4 text-sm flex-wrap">
      <Link href="/admin" className="font-bold text-base shrink-0">auto-gallery</Link>
      <div className="flex items-center gap-3 flex-1 flex-wrap">
        {links.map(([href, label]) => (
          <Link key={href} href={href} className="hover:text-gray-300 transition-colors">{label}</Link>
        ))}
      </div>
      <div className="flex items-center gap-1 shrink-0 ml-auto">
        <LangToggle />
        <ThemeToggle />
      </div>
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

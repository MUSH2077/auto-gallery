"use client";
import { useEffect, useRef, useState, type RefObject } from "react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useT } from "@/lib/i18n";
import { ThemeToggle, LangToggle, PaletteToggle } from "@/lib/theme";
import { useAuth } from "@/lib/auth";
import { usePermissions } from "@/lib/usePermissions";
import { usePresence } from "@/lib/motion";
import { findAdminNavEntry } from "@/lib/adminNavigation";
import { NotificationBell } from "@/components/NotificationCenter";

function UserMenu() {
  const t = useT();
  const { user, logout } = useAuth();
  const [open, setOpen] = useState(false);
  const { mounted, closing } = usePresence(open);
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
        className="flex items-center gap-1.5 rounded-md px-2 py-1 text-sm text-muted transition-colors hover:bg-subtle hover:text-fg"
        title={user?.display_name || user?.username}
      >
        <span className="flex h-[26px] w-[26px] items-center justify-center rounded-full border border-border bg-accent text-[11px] font-semibold text-white">
          {(user?.display_name || user?.username || "?").trim().slice(0, 2).toUpperCase()}
        </span>
      </button>
      {mounted && (
        <div className={`popover ${closing ? "popover-exit" : ""} absolute right-0 z-50 mt-1 w-44 overflow-hidden rounded-md border border-border bg-surface text-sm text-fg shadow-overlay dark:shadow-overlay-dark`}>
          <div className="border-b border-border px-3 py-2 text-xs text-muted">{user?.display_name || user?.username}</div>
          <Link
            href="/admin/settings/profile"
            onClick={() => setOpen(false)}
            className="flex items-center gap-2 px-3 py-2 transition-colors hover:bg-subtle"
          >
            {t("auth.change_password")}
          </Link>
          <button
            onClick={() => { setOpen(false); logout(); }}
            className="flex w-full items-center gap-2 px-3 py-2 text-left text-danger transition-colors hover:bg-subtle"
          >
            {t("auth.logout")}
          </button>
        </div>
      )}
    </div>
  );
}

export default function AppTopBar({
  onToggleSidebar,
  sidebarExpanded,
  sidebarId,
  sidebarTriggerRef,
}: {
  onToggleSidebar: () => void;
  sidebarExpanded: boolean;
  sidebarId: string;
  sidebarTriggerRef?: RefObject<HTMLButtonElement>;
}) {
  const t = useT();
  const pathname = usePathname();
  const router = useRouter();
  const { has } = usePermissions();
  const crumb = findAdminNavEntry(pathname);
  const showSearch = has("library");
  const showUpload = has("upload");

  // ⌘K / Ctrl+K jumps to global search (GitHub-style shortcut).
  useEffect(() => {
    if (!showSearch) return;
    const onKey = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        router.push("/admin/search");
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [showSearch, router]);

  return (
    <header className="sticky top-0 z-40 flex h-14 items-center gap-2 border-b border-border bg-canvas px-4">
      <button
        ref={sidebarTriggerRef}
        type="button"
        onClick={onToggleSidebar}
        className="btn-icon no-press"
        aria-label={t("nav.sidebar_toggle")}
        aria-controls={sidebarId}
        aria-expanded={sidebarExpanded}
      >
        <svg width="16" height="16" viewBox="0 0 16 16" fill="currentColor" aria-hidden>
          <path d="M1 2.75A.75.75 0 0 1 1.75 2h12.5a.75.75 0 0 1 0 1.5H1.75A.75.75 0 0 1 1 2.75Zm0 5A.75.75 0 0 1 1.75 7h12.5a.75.75 0 0 1 0 1.5H1.75A.75.75 0 0 1 1 7.75ZM1.75 12h12.5a.75.75 0 0 1 0 1.5H1.75a.75.75 0 0 1 0-1.5Z" />
        </svg>
      </button>
      <div className="flex min-w-0 items-center gap-1.5 text-sm">
        {crumb && crumb.groupKey !== crumb.labelKey && (
          <>
            <span className="hidden text-muted sm:inline">{t(crumb.groupKey)}</span>
            <span className="hidden text-muted sm:inline">/</span>
          </>
        )}
        <span className="truncate font-semibold">{crumb ? t(crumb.labelKey) : "auto-gallery"}</span>
      </div>

      <div className="ml-auto flex items-center gap-1.5">
        {showSearch && (
          <Link
            href="/admin/search"
            className="hidden h-8 w-64 items-center gap-2 rounded-md border border-border px-2.5 text-[13px] text-placeholder transition-colors hover:border-muted md:flex"
          >
            <svg width="14" height="14" viewBox="0 0 16 16" fill="currentColor" aria-hidden>
              <path d="M10.68 11.74a6 6 0 0 1-7.922-8.982 6 6 0 0 1 8.982 7.922l3.04 3.04a.749.749 0 0 1-1.06 1.06ZM11.5 7a4.499 4.499 0 1 0-8.997 0A4.499 4.499 0 0 0 11.5 7Z" />
            </svg>
            <span className="truncate">{t("search.placeholder")}</span>
            <kbd className="ml-auto shrink-0 rounded border border-border bg-subtle px-1 font-mono text-[10px] text-muted">⌘K</kbd>
          </Link>
        )}
        {showUpload && (
          <Link href="/admin/upload" className="btn-ghost hidden px-3 py-1.5 text-xs sm:inline-flex">
            ↑ {t("nav.upload")}
          </Link>
        )}
        <NotificationBell />
        <LangToggle />
        <PaletteToggle />
        <ThemeToggle />
        <UserMenu />
      </div>
    </header>
  );
}

"use client";
import { useCallback, useEffect, useId, useRef, useState, type RefObject } from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { Activity, CircleCheck, Menu, Search, Upload } from "lucide-react";
import { useQuery } from "@tanstack/react-query";
import { useT } from "@/lib/i18n";
import { ThemeToggle, LangToggle } from "@/lib/theme";
import { useAuth } from "@/lib/auth";
import { usePermissions } from "@/lib/usePermissions";
import { usePresence } from "@/lib/motion";
import { findAdminNavEntry } from "@/lib/adminNavigation";
import { adminRoutes } from "@/lib/adminRoutes";
import { NotificationBell } from "@/components/NotificationCenter";
import CommandPalette from "@/components/CommandPalette";
import { api, queryKeys } from "@/lib/api";

function UserMenu() {
  const t = useT();
  const { user, logout } = useAuth();
  const [open, setOpen] = useState(false);
  const { mounted, closing } = usePresence(open);
  const ref = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const menuId = useId();

  useEffect(() => {
    function handleClick(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    }
    function handleKeyDown(e: KeyboardEvent) {
      if (!open) return;
      if (e.key === "Escape") {
        e.preventDefault();
        setOpen(false);
        triggerRef.current?.focus();
        return;
      }
      if (e.key !== "ArrowDown" && e.key !== "ArrowUp" && e.key !== "Home" && e.key !== "End") return;
      const items = Array.from(menuRef.current?.querySelectorAll<HTMLElement>('[role="menuitem"]') || []);
      if (!items.length) return;
      e.preventDefault();
      const currentIndex = items.indexOf(document.activeElement as HTMLElement);
      if (e.key === "Home") items[0].focus();
      else if (e.key === "End") items[items.length - 1].focus();
      else if (e.key === "ArrowDown") items[(currentIndex + 1 + items.length) % items.length].focus();
      else items[(currentIndex - 1 + items.length) % items.length].focus();
    }
    document.addEventListener("mousedown", handleClick);
    document.addEventListener("keydown", handleKeyDown);
    return () => {
      document.removeEventListener("mousedown", handleClick);
      document.removeEventListener("keydown", handleKeyDown);
    };
  }, [open]);

  return (
    <div ref={ref} className="relative">
      <button
        ref={triggerRef}
        type="button"
        onClick={() => setOpen(!open)}
        onKeyDown={(event) => {
          if (event.key === "ArrowDown") {
            event.preventDefault();
            setOpen(true);
            window.requestAnimationFrame(() => menuRef.current?.querySelector<HTMLElement>('[role="menuitem"]')?.focus());
          }
        }}
        className="flex h-11 min-w-11 items-center justify-center rounded-md px-2 text-sm text-muted transition-colors hover:bg-subtle hover:text-fg"
        title={user?.display_name || user?.username}
        aria-label={t("nav.user_menu")}
        aria-haspopup="menu"
        aria-controls={menuId}
        aria-expanded={open}
      >
        <span className="flex h-[26px] w-[26px] items-center justify-center rounded-full border border-border bg-accent text-[11px] font-semibold text-white">
          {(user?.display_name || user?.username || "?").trim().slice(0, 2).toUpperCase()}
        </span>
      </button>
      {mounted && (
        <div
          ref={menuRef}
          id={menuId}
          role="menu"
          aria-label={t("nav.user_menu")}
          className={`popover ${closing ? "popover-exit" : ""} absolute right-0 z-50 mt-1 w-52 overflow-hidden rounded-md border border-border bg-surface text-sm text-fg shadow-overlay dark:shadow-overlay-dark`}
        >
          <div className="border-b border-border px-3 py-2 text-xs text-muted">{user?.display_name || user?.username}</div>
          <Link
            href={adminRoutes.profile}
            onClick={() => setOpen(false)}
            role="menuitem"
            className="flex min-h-11 items-center gap-2 px-3 py-2 transition-colors hover:bg-subtle focus-visible:bg-subtle"
          >
            {t("auth.change_password")}
          </Link>
          <button
            type="button"
            onClick={() => { setOpen(false); logout(); }}
            role="menuitem"
            className="flex min-h-11 w-full items-center gap-2 px-3 py-2 text-left text-danger transition-colors hover:bg-subtle focus-visible:bg-subtle"
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
  const { has } = usePermissions();
  const crumb = findAdminNavEntry(pathname);
  const showSearch = has("library");
  const showUpload = has("upload");
  const showOperations = has("tasks");
  const operations = useQuery({
    queryKey: queryKeys.tasks.operations("attention"),
    queryFn: ({ signal }) => api.operationsOverview("attention", signal),
    enabled: showOperations,
    staleTime: 15_000,
    refetchInterval: 30_000,
    refetchIntervalInBackground: false,
  });
  const [commandOpen, setCommandOpen] = useState(false);
  const closeCommand = useCallback(() => setCommandOpen(false), []);

  // ⌘K / Ctrl+K opens the shared route + content command palette.
  useEffect(() => {
    if (!showSearch) return;
    const onKey = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        setCommandOpen((current) => !current);
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [showSearch]);

  return (
    <>
      <header className="sticky top-0 z-40 flex h-14 min-w-0 items-center gap-1 border-b border-border bg-surface/95 px-2 backdrop-blur sm:gap-2 sm:px-4">
        <button
          ref={sidebarTriggerRef}
          type="button"
          onClick={onToggleSidebar}
          className="btn-icon no-press shrink-0"
          aria-label={t("nav.sidebar_toggle")}
          aria-controls={sidebarId}
          aria-expanded={sidebarExpanded}
        >
          <Menu className="h-[18px] w-[18px]" strokeWidth={1.8} aria-hidden />
        </button>
        <div className="flex min-w-0 flex-1 items-center gap-1.5 text-sm">
          {crumb && crumb.groupKey !== crumb.labelKey && (
            <>
              <span className="hidden text-muted lg:inline">{t(crumb.groupKey)}</span>
              <span className="hidden text-muted lg:inline">/</span>
            </>
          )}
          <span className="truncate font-semibold">{crumb ? t(crumb.labelKey) : "auto-gallery"}</span>
        </div>

        <div className="flex shrink-0 items-center gap-0.5 sm:gap-1">
          {showOperations && (
            <Link
              href={adminRoutes.jobs}
              className={`relative flex h-11 min-w-11 items-center justify-center rounded-md px-2 transition-colors hover:bg-subtle ${operations.data?.summary.attention ? "text-danger" : "text-success"}`}
              aria-label={operations.data?.summary.attention
                ? t("operations.topbar_attention", { count: operations.data.summary.attention })
                : t("operations.topbar_healthy")}
              title={operations.data?.summary.attention
                ? t("operations.topbar_attention", { count: operations.data.summary.attention })
                : t("operations.topbar_healthy")}
            >
              {operations.data?.summary.attention ? <Activity className="h-[18px] w-[18px]" aria-hidden /> : <CircleCheck className="h-[18px] w-[18px]" aria-hidden />}
              {!!operations.data?.summary.attention && (
                <span className="absolute right-0.5 top-0.5 min-w-4 rounded-full bg-danger px-1 text-center text-[10px] font-semibold leading-4 text-white">
                  {operations.data.summary.attention > 99 ? "99+" : operations.data.summary.attention}
                </span>
              )}
            </Link>
          )}
          {showSearch && (
            <>
              <button
                type="button"
                onClick={() => setCommandOpen(true)}
                className="hidden h-8 w-48 items-center gap-2 rounded-lg border border-border bg-canvas px-2.5 text-[13px] text-placeholder transition-colors hover:border-muted xl:flex"
              >
                <Search className="h-4 w-4 shrink-0" strokeWidth={1.8} aria-hidden />
                <span className="truncate">{t("search.placeholder")}</span>
                <kbd className="ml-auto shrink-0 rounded border border-border bg-subtle px-1 font-mono text-[10px] text-muted">⌘K</kbd>
              </button>
              <button
                type="button"
                onClick={() => setCommandOpen(true)}
                className="btn-icon xl:hidden"
                aria-label={t("search.title")}
                title={t("search.title")}
              >
                <Search className="h-[18px] w-[18px]" strokeWidth={1.8} aria-hidden />
              </button>
            </>
          )}
          {showUpload && (
            <Link href="/admin/upload" className="btn-icon" aria-label={t("nav.upload")} title={t("nav.upload")}>
              <Upload className="h-[18px] w-[18px]" strokeWidth={1.8} aria-hidden />
            </Link>
          )}
          <NotificationBell />
          <LangToggle />
          <ThemeToggle />
          <UserMenu />
        </div>
      </header>
      <CommandPalette open={commandOpen} onClose={closeCommand} />
    </>
  );
}

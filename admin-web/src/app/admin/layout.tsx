"use client";
import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { useRouter, usePathname } from "next/navigation";
import { useAuth } from "@/lib/auth";
import { adminRoutes } from "@/lib/adminRoutes";
import { usePresence } from "@/lib/motion";
import ErrorBoundary from "@/components/ErrorBoundary";
import AppSidebar from "@/components/AppSidebar";
import AppTopBar from "@/components/AppTopBar";
import { useT } from "@/lib/i18n";
import {
  ADMIN_SIDEBAR_COMPACT_WIDTH,
  ADMIN_SIDEBAR_EXPANDED_WIDTH,
  LEGACY_SIDEBAR_KEY,
  SIDEBAR_MID_KEY,
  SIDEBAR_WIDE_KEY,
} from "@/lib/adminSidebar";

export const dynamic = 'force-dynamic';

type DesktopSidebarMode = "expanded" | "compact";
type ViewportTier = "mobile" | "mid" | "wide";

function AuthGuard({ children }: { children: React.ReactNode }) {
  const t = useT();
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
      && pathname !== adminRoutes.profile
      && pathname !== "/admin/login"
    ) {
      router.replace(adminRoutes.profile);
    }
  }, [isAuthenticated, isLoading, pathname, router, user?.must_change_password]);

  if (isLoading) {
    return (
      <div className="min-h-screen flex items-center justify-center bg-subtle">
        <div role="status" className="flex items-center gap-3 text-sm text-muted">
          <span aria-hidden="true" className="animate-spin rounded-full h-8 w-8 border-b-2 border-border" />
          <span className="sr-only">{t("common.loading")}</span>
        </div>
      </div>
    );
  }

  if (!isAuthenticated && pathname !== "/admin/login") return null;

  return <>{children}</>;
}

function AppShell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const [wideMode, setWideMode] = useState<DesktopSidebarMode>("expanded");
  const [midMode, setMidMode] = useState<DesktopSidebarMode>("compact");
  const [mobileOpen, setMobileOpen] = useState(false);
  const [viewportTier, setViewportTier] = useState<ViewportTier>("wide");
  const [sidebarReady, setSidebarReady] = useState(false);
  const { mounted: drawerMounted, closing: drawerClosing } = usePresence(mobileOpen);
  const sidebarTriggerRef = useRef<HTMLButtonElement>(null);
  const mainRef = useRef<HTMLElement>(null);
  const previousPathRef = useRef(pathname);
  const restoreDrawerFocusRef = useRef(true);

  useLayoutEffect(() => {
    try {
      const legacy = localStorage.getItem(LEGACY_SIDEBAR_KEY);
      const storedWide = localStorage.getItem(SIDEBAR_WIDE_KEY);
      const storedMid = localStorage.getItem(SIDEBAR_MID_KEY);
      if (storedWide === "expanded" || storedWide === "compact") {
        setWideMode(storedWide);
      } else if (legacy === "collapsed") {
        setWideMode("compact");
      }
      if (storedMid === "expanded" || storedMid === "compact") {
        setMidMode(storedMid);
      }
    } catch {}
  }, []);

  useLayoutEffect(() => {
    const mobile = window.matchMedia("(max-width: 767px)");
    const mid = window.matchMedia("(min-width: 768px) and (max-width: 1279px)");
    const syncViewport = () => {
      const tier: ViewportTier = mobile.matches ? "mobile" : mid.matches ? "mid" : "wide";
      setViewportTier(tier);
      if (tier !== "mobile") setMobileOpen(false);
    };
    syncViewport();
    setSidebarReady(true);
    mobile.addEventListener("change", syncViewport);
    mid.addEventListener("change", syncViewport);
    return () => {
      mobile.removeEventListener("change", syncViewport);
      mid.removeEventListener("change", syncViewport);
    };
  }, []);

  useEffect(() => {
    if (!mobileOpen || !drawerMounted) return;
    const previousFocus = document.activeElement as HTMLElement | null;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    window.requestAnimationFrame(() => {
      document.querySelector<HTMLElement>("#admin-mobile-sidebar button, #admin-mobile-sidebar a")?.focus();
    });
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        restoreDrawerFocusRef.current = true;
        setMobileOpen(false);
        return;
      }
      if (event.key !== "Tab") return;
      const drawer = document.querySelector<HTMLElement>("#admin-mobile-sidebar");
      const focusable = Array.from(drawer?.querySelectorAll<HTMLElement>(
        'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
      ) || []).filter((element) => !element.hasAttribute("hidden"));
      if (!focusable.length) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    document.addEventListener("keydown", handleKeyDown);
    return () => {
      document.removeEventListener("keydown", handleKeyDown);
      document.body.style.overflow = previousOverflow;
      if (restoreDrawerFocusRef.current) {
        (sidebarTriggerRef.current || previousFocus)?.focus();
      }
    };
  }, [drawerMounted, mobileOpen]);

  useEffect(() => {
    if (previousPathRef.current === pathname) return;
    previousPathRef.current = pathname;
    window.scrollTo({ top: 0, left: 0, behavior: "auto" });
    window.requestAnimationFrame(() => {
      window.scrollTo({ top: 0, left: 0, behavior: "auto" });
      mainRef.current?.focus({ preventScroll: true });
    });
  }, [pathname]);

  const desktopMode = viewportTier === "mid" ? midMode : wideMode;
  const desktopCompact = desktopMode === "compact";
  const sidebarWidth = desktopCompact
    ? ADMIN_SIDEBAR_COMPACT_WIDTH
    : ADMIN_SIDEBAR_EXPANDED_WIDTH;

  useLayoutEffect(() => {
    if (!sidebarReady) return;
    document.documentElement.style.setProperty("--admin-sidebar-width", `${sidebarWidth}px`);
  }, [sidebarReady, sidebarWidth]);

  const toggleSidebar = () => {
    if (viewportTier === "mobile") {
      setMobileOpen((open) => {
        restoreDrawerFocusRef.current = true;
        return !open;
      });
      return;
    }
    const setter = viewportTier === "mid" ? setMidMode : setWideMode;
    const storageKey = viewportTier === "mid" ? SIDEBAR_MID_KEY : SIDEBAR_WIDE_KEY;
    setter((current) => {
      const next = current === "compact" ? "expanded" : "compact";
      try { localStorage.setItem(storageKey, next); } catch {}
      return next;
    });
  };

  return (
    <div
      className="grid min-h-screen grid-cols-1 md:grid-cols-[var(--admin-sidebar-width)_minmax(0,1fr)]"
    >
      <aside
        id="admin-sidebar"
        className="sticky top-0 hidden h-screen min-w-0 overflow-hidden border-r border-border bg-surface md:block"
      >
        <AppSidebar compact={desktopCompact} />
      </aside>
      {/* Mobile drawer */}
      {drawerMounted && (
        <>
          <div
            className={`fixed inset-0 z-50 bg-black/30 md:hidden ${drawerClosing ? "overlay-backdrop-exit" : "overlay-backdrop"}`}
            onClick={() => {
              restoreDrawerFocusRef.current = true;
              setMobileOpen(false);
            }}
            aria-hidden
          />
          <aside
            id="admin-mobile-sidebar"
            role="dialog"
            aria-modal="true"
            aria-label="auto-gallery"
            className={`fixed inset-y-0 left-0 z-[60] border-r border-border bg-subtle md:hidden ${drawerClosing ? "drawer-left-exit" : "drawer-left"}`}
          >
            <AppSidebar
              onNavigate={() => {
                restoreDrawerFocusRef.current = false;
                setMobileOpen(false);
              }}
              onDismiss={() => {
                restoreDrawerFocusRef.current = true;
                setMobileOpen(false);
              }}
            />
          </aside>
        </>
      )}
      <div className="flex min-w-0 flex-1 flex-col">
        <AppTopBar
          onToggleSidebar={toggleSidebar}
          sidebarExpanded={viewportTier === "mobile" ? mobileOpen : desktopMode === "expanded"}
          sidebarId={viewportTier === "mobile" ? "admin-mobile-sidebar" : "admin-sidebar"}
          sidebarTriggerRef={sidebarTriggerRef}
        />
        <main
          ref={mainRef}
          id="main-content"
          tabIndex={-1}
          className="min-h-[calc(100vh-56px)] min-w-0 outline-none"
        >
          {children}
        </main>
      </div>
    </div>
  );
}

export default function AdminLayout({ children }: { children: React.ReactNode }) {
  const t = useT();
  const pathname = usePathname();
  const isLoginPage = pathname === "/admin/login";

  if (isLoginPage) {
    return <ErrorBoundary>{children}</ErrorBoundary>;
  }

  return (
    <ErrorBoundary>
      <AuthGuard>
        <a href="#main-content" className="sr-only focus:not-sr-only focus:absolute focus:top-2 focus:left-2 focus:z-50 focus:px-4 focus:py-2 focus:bg-subtle focus:text-fg focus:rounded">{t("common.skip_to_content")}</a>
        <AppShell>{children}</AppShell>
      </AuthGuard>
    </ErrorBoundary>
  );
}

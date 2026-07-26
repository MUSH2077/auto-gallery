"use client";
import { useEffect, useRef, useState } from "react";
import { useRouter, usePathname } from "next/navigation";
import { useAuth } from "@/lib/auth";
import { usePresence } from "@/lib/motion";
import ErrorBoundary from "@/components/ErrorBoundary";
import AppSidebar from "@/components/AppSidebar";
import AppTopBar from "@/components/AppTopBar";

export const dynamic = 'force-dynamic';

const SIDEBAR_KEY = "auto-gallery-sidebar";

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
      <div className="min-h-screen flex items-center justify-center bg-subtle">
        <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-border" />
      </div>
    );
  }

  if (!isAuthenticated && pathname !== "/admin/login") return null;

  return <>{children}</>;
}

function AppShell({ children }: { children: React.ReactNode }) {
  const [collapsed, setCollapsed] = useState(false);
  const [mobileOpen, setMobileOpen] = useState(false);
  const [mobileViewport, setMobileViewport] = useState(false);
  const { mounted: drawerMounted, closing: drawerClosing } = usePresence(mobileOpen);
  const sidebarTriggerRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    try {
      if (localStorage.getItem(SIDEBAR_KEY) === "collapsed") setCollapsed(true);
    } catch {}
  }, []);

  useEffect(() => {
    const media = window.matchMedia("(max-width: 767px)");
    const syncViewport = () => {
      setMobileViewport(media.matches);
      if (!media.matches) setMobileOpen(false);
    };
    syncViewport();
    media.addEventListener("change", syncViewport);
    return () => media.removeEventListener("change", syncViewport);
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
      (sidebarTriggerRef.current || previousFocus)?.focus();
    };
  }, [drawerMounted, mobileOpen]);

  const toggleSidebar = () => {
    if (mobileViewport) {
      setMobileOpen((open) => !open);
      return;
    }
    setCollapsed((c) => {
      try { localStorage.setItem(SIDEBAR_KEY, c ? "open" : "collapsed"); } catch {}
      return !c;
    });
  };

  return (
    <div className="flex min-h-screen">
      {/* Desktop sidebar — sticky full-height, collapse is instant by design */}
      {!collapsed && (
        <aside id="admin-sidebar" className="sticky top-0 hidden h-screen shrink-0 overflow-hidden border-r border-border bg-subtle md:block">
          <AppSidebar />
        </aside>
      )}
      {/* Mobile drawer */}
      {drawerMounted && (
        <>
          <div
            className={`fixed inset-0 z-50 bg-black/30 md:hidden ${drawerClosing ? "overlay-backdrop-exit" : "overlay-backdrop"}`}
            onClick={() => setMobileOpen(false)}
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
              onNavigate={() => setMobileOpen(false)}
              onDismiss={() => setMobileOpen(false)}
            />
          </aside>
        </>
      )}
      <div className="flex min-w-0 flex-1 flex-col">
        <AppTopBar
          onToggleSidebar={toggleSidebar}
          sidebarExpanded={mobileViewport ? mobileOpen : !collapsed}
          sidebarId={mobileViewport ? "admin-mobile-sidebar" : "admin-sidebar"}
          sidebarTriggerRef={sidebarTriggerRef}
        />
        <main id="main-content" className="min-h-[calc(100vh-56px)]">{children}</main>
      </div>
    </div>
  );
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
        <a href="#main-content" className="sr-only focus:not-sr-only focus:absolute focus:top-2 focus:left-2 focus:z-50 focus:px-4 focus:py-2 focus:bg-subtle focus:text-fg focus:rounded">Skip to content</a>
        <AppShell>{children}</AppShell>
      </AuthGuard>
    </ErrorBoundary>
  );
}

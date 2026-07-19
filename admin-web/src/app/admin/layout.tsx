"use client";
import { useEffect, useState } from "react";
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
  const { mounted: drawerMounted, closing: drawerClosing } = usePresence(mobileOpen);

  useEffect(() => {
    try {
      if (localStorage.getItem(SIDEBAR_KEY) === "collapsed") setCollapsed(true);
    } catch {}
  }, []);

  const toggleSidebar = () => {
    if (typeof window !== "undefined" && window.matchMedia("(max-width: 767px)").matches) {
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
        <aside className="sticky top-0 hidden h-screen shrink-0 overflow-hidden border-r border-border bg-subtle md:block">
          <AppSidebar />
        </aside>
      )}
      {/* Mobile drawer */}
      {drawerMounted && (
        <>
          <div
            className={`fixed inset-0 z-40 bg-black/30 md:hidden ${drawerClosing ? "overlay-backdrop-exit" : "overlay-backdrop"}`}
            onClick={() => setMobileOpen(false)}
            aria-hidden
          />
          <aside className={`fixed inset-y-0 left-0 z-50 border-r border-border bg-subtle md:hidden ${drawerClosing ? "drawer-left-exit" : "drawer-left"}`}>
            <AppSidebar onNavigate={() => setMobileOpen(false)} />
          </aside>
        </>
      )}
      <div className="flex min-w-0 flex-1 flex-col">
        <AppTopBar onToggleSidebar={toggleSidebar} />
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

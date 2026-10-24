"use client";
import { useEffect, type ReactNode } from "react";
import { useRouter, usePathname } from "next/navigation";
import { useAuth } from "@/lib/auth";
import { adminRoutes } from "@/lib/adminRoutes";
import { useT } from "@/lib/i18n";

export default function AuthGuard({ children, loginPath="/admin/login", loadingFallback, unavailableFallback }: { children: ReactNode;loginPath?:string;loadingFallback?:ReactNode;unavailableFallback?:ReactNode }) {
  const t = useT();
  const { isAuthenticated, isLoading, authUnavailable, user } = useAuth();
  const router = useRouter();
  const pathname = usePathname();

  useEffect(() => {
    if (!isLoading && !isAuthenticated && !authUnavailable && pathname !== "/admin/login") {
      router.replace(loginPath+"?redirect="+encodeURIComponent(window.location.pathname+window.location.search));
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
  }, [isAuthenticated, isLoading, authUnavailable, pathname, router, user?.must_change_password, loginPath]);

  if (isLoading) {
    if (loadingFallback) return <>{loadingFallback}</>;
    return (
      <div className="min-h-screen flex items-center justify-center bg-subtle">
        <div role="status" className="flex items-center gap-3 text-sm text-muted">
          <span aria-hidden="true" className="animate-spin rounded-full h-8 w-8 border-b-2 border-border" />
          <span className="sr-only">{t("common.loading")}</span>
        </div>
      </div>
    );
  }

  if (authUnavailable) {
    if (unavailableFallback) return <>{unavailableFallback}</>;
    return <div role="alert" className="m-8 rounded border p-6">
      {t("auth.session_unavailable")}
      <button type="button" className="btn-primary ml-4" onClick={() => window.location.reload()}>{t("auth.retry")}</button>
    </div>;
  }

  if (!isAuthenticated && pathname !== "/admin/login") return null;

  return <>{children}</>;
}

"use client";
import { useEffect, type ReactNode } from "react";
import { useRouter, usePathname } from "next/navigation";
import { useAuth } from "@/lib/auth";
import { adminRoutes } from "@/lib/adminRoutes";

export default function AuthGuard({ children, loadingFallback, unavailableFallback }: { children: ReactNode;loadingFallback:ReactNode;unavailableFallback:ReactNode }) {
  const { isAuthenticated, isLoading, authUnavailable, user } = useAuth();
  const router = useRouter();
  const pathname = usePathname();

  useEffect(() => {
    if (!isLoading && !isAuthenticated && !authUnavailable && pathname !== "/admin/login") {
      router.replace("/admin/login?redirect="+encodeURIComponent(window.location.pathname+window.location.search+window.location.hash));
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
  }, [isAuthenticated, isLoading, authUnavailable, pathname, router, user?.must_change_password]);

  if (isLoading) return <>{loadingFallback}</>;
  if (authUnavailable) return <>{unavailableFallback}</>;

  if (!isAuthenticated && pathname !== "/admin/login") return null;
  if (user?.must_change_password && pathname !== adminRoutes.profile && pathname !== "/admin/login") return null;

  return <>{children}</>;
}

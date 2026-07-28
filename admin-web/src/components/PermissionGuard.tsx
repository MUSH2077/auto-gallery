"use client";
import { ReactNode } from "react";
import EmptyState from "@/components/EmptyState";
import { usePermissions } from "@/lib/usePermissions";
import { useT } from "@/lib/i18n";

/**
 * Client-side gate for a module-scoped page. Mirrors the backend's
 * RequirePermission(module) — this is a UX convenience (hide/blank the page),
 * NOT the security boundary; the API rejects unauthorized calls regardless.
 *
 * While the `me` query is loading, renders nothing to avoid a 403 flash for
 * users who do have access.
 */
export default function PermissionGuard({ module, children }: { module: string; children: ReactNode }) {
  const t = useT();
  const { has, isLoading } = usePermissions();

  if (isLoading) return null;

  if (!has(module)) {
    return (
      <div className="p-6">
        <EmptyState icon="🔒" title={t("common.forbidden")} />
      </div>
    );
  }

  return <>{children}</>;
}

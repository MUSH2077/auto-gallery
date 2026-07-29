"use client";
import { ReactNode } from "react";
import { LockKeyhole } from "lucide-react";
import EmptyState from "@/components/EmptyState";
import PageShell from "@/components/PageShell";
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
type PermissionGuardProps = {
  children: ReactNode;
} & (
  | { module: string; anyOf?: never }
  | { module?: never; anyOf: readonly string[] }
);

export default function PermissionGuard({ module, anyOf, children }: PermissionGuardProps) {
  const t = useT();
  const { has, isLoading } = usePermissions();
  const requirements = anyOf || (module ? [module] : []);

  if (isLoading) return null;

  if (!requirements.some((requirement) => has(requirement))) {
    return (
      <PageShell>
        <EmptyState
          icon={<LockKeyhole aria-hidden="true" className="h-8 w-8" />}
          title={t("common.forbidden")}
          description={t("common.forbidden_desc")}
        />
      </PageShell>
    );
  }

  return <>{children}</>;
}

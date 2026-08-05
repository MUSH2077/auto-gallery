"use client";
import { ReactNode } from "react";
import { usePathname } from "next/navigation";
import { LockKeyhole } from "lucide-react";
import EmptyState from "@/components/EmptyState";
import PageHeader from "@/components/PageHeader";
import PageShell from "@/components/PageShell";
import { adminPageTitleKey } from "@/lib/adminRoutes";
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
  const pathname = usePathname();
  const { has, isLoading } = usePermissions();
  const requirements = anyOf || (module ? [module] : []);
  const titleKey = adminPageTitleKey(pathname);

  if (isLoading) {
    if (!titleKey) return null;
    return (
      <PageShell>
        <PageHeader title={t(titleKey)} />
        <div aria-hidden="true" className="h-32 animate-pulse rounded-md bg-subtle" />
      </PageShell>
    );
  }

  if (!requirements.some((requirement) => has(requirement))) {
    return (
      <PageShell>
        {titleKey && <PageHeader title={t(titleKey)} />}
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

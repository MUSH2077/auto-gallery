"use client";

import { Suspense } from "react";

import { TableSkeleton } from "@/components";
import { BusinessPage, BusinessPermissionGuard as PermissionGuard } from "@/features/gallery-ui/BusinessPage";
import { useT } from "@/lib/i18n";
import RemoteDiscoveryPage from "./RemoteDiscoveryPage";

function DiscoveryFallback() {
  const t = useT();
  return (
    <BusinessPage title={t("discovery.title")} description={t("discovery.desc")} className="gallery-discovery">
      <TableSkeleton rows={6} />
    </BusinessPage>
  );
}

export default function DiscoveryRoute() {
  return (
    <PermissionGuard module="subscriptions">
      <Suspense fallback={<DiscoveryFallback />}>
        <RemoteDiscoveryPage />
      </Suspense>
    </PermissionGuard>
  );
}

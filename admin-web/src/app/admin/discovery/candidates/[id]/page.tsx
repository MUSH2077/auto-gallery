"use client";

import { Suspense } from "react";

import { BusinessPage, BusinessPermissionGuard as PermissionGuard } from "@/features/gallery-ui/BusinessPage";
import { useT } from "@/lib/i18n";
import RemoteCreatorDetailPage from "./RemoteCreatorDetailPage";

function DetailFallback() {
  const t = useT();
  return (
    <BusinessPage title={t("discovery.creator_details")} className="gallery-discovery">
      <div className="h-72 animate-pulse rounded-2xl bg-subtle" aria-label={t("common.loading")} />
    </BusinessPage>
  );
}

export default function DiscoveryCandidateDetailRoute() {
  return (
    <PermissionGuard module="subscriptions">
      <Suspense fallback={<DetailFallback />}>
        <RemoteCreatorDetailPage />
      </Suspense>
    </PermissionGuard>
  );
}

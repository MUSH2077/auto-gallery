import { permanentRedirect } from "next/navigation";

import { adminRoutes } from "@/lib/adminRoutes";
import { canonicalRedirectUrl, type LegacySearchParams } from "@/lib/legacyRedirect";

export default async function LegacyRepositoryPage({
  params,
  searchParams,
}: {
  params: Promise<{ id: string }>;
  searchParams: Promise<LegacySearchParams>;
}) {
  const { id } = await params;
  permanentRedirect(canonicalRedirectUrl(adminRoutes.repository(id), await searchParams));
}

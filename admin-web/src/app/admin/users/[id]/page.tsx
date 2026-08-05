import { permanentRedirect } from "next/navigation";

import { adminRoutes } from "@/lib/adminRoutes";
import { canonicalRedirectUrl, type LegacySearchParams } from "@/lib/legacyRedirect";

export default async function LegacyUserPage({
  params,
  searchParams,
}: {
  params: Promise<{ id: string }>;
  searchParams: Promise<LegacySearchParams>;
}) {
  const { id } = await params;
  permanentRedirect(canonicalRedirectUrl(adminRoutes.user(id), await searchParams));
}

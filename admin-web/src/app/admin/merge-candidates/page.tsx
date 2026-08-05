import { permanentRedirect } from "next/navigation";

import { adminRoutes } from "@/lib/adminRoutes";
import { canonicalRedirectUrl, type LegacySearchParams } from "@/lib/legacyRedirect";

export default async function LegacyMergeCandidatesPage({
  searchParams,
}: {
  searchParams: Promise<LegacySearchParams>;
}) {
  permanentRedirect(canonicalRedirectUrl(adminRoutes.dedup, await searchParams, { status: "pending" }));
}

import { permanentRedirect } from "next/navigation";

import { adminRoutes } from "@/lib/adminRoutes";
import { canonicalRedirectUrl, type LegacySearchParams } from "@/lib/legacyRedirect";

export default async function LegacyDanbooruPage({
  searchParams,
}: {
  searchParams: Promise<LegacySearchParams>;
}) {
  permanentRedirect(canonicalRedirectUrl(adminRoutes.danbooru, await searchParams));
}

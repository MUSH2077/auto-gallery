import { redirect } from "next/navigation";

import { adminRoutes } from "@/lib/adminRoutes";

/** Defensive fallback; the proxy emits the canonical HTTP 308 first. */
export default function AuthStatusRedirect() {
  redirect(adminRoutes.schedulerAuth);
}

import { redirect } from "next/navigation";

export default function LegacyMergeCandidatesPage() {
  redirect("/admin/dedup?status=pending");
}

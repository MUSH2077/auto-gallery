import { redirect } from "next/navigation";
import { adminRoutes } from "@/lib/adminRoutes";

type DetailRouteProps = {
  params: Promise<{ id: string }>;
  searchParams: Promise<Record<string, string | string[] | undefined>>;
};

export default async function WorkDetailRoute({ params, searchParams }: DetailRouteProps) {
  const [{ id }, incoming] = await Promise.all([params, searchParams]);
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(incoming)) {
    if (Array.isArray(value)) value.forEach(item => query.append(key, item));
    else if (value !== undefined) query.set(key, value);
  }
  query.set("work", id);
  redirect(`${adminRoutes.works}?${query}`);
}

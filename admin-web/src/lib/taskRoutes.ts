import { adminRoutes } from "@/lib/adminRoutes";

type LinkableTask = {
  id?: string | null;
  kind?: string | null;
};

const TASK_TABS: Record<string, "downloads" | "imports" | "admin"> = {
  download: "downloads",
  import: "imports",
  admin: "admin",
};

/** Return the canonical task drawer URL for a task that has a detail surface. */
export function taskRunDestination(task: LinkableTask): string | null {
  if (!task.id || !task.kind) return null;
  const tab = TASK_TABS[task.kind];
  if (!tab) return null;
  return `${adminRoutes.jobs}?tab=${tab}&task=${encodeURIComponent(task.id)}`;
}

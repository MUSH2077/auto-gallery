/** Canonical Jobs deep-link parsing and URL updates. */

export type JobsTab = "all" | "downloads" | "imports" | "admin";

export function parseJobsRoute(sp: URLSearchParams, pageSize: number) {
  const tabParam = sp.get("tab");
  const activeTab = (tabParam === "downloads" || tabParam === "imports" || tabParam === "admin" ? tabParam : "all") as JobsTab;
  const subscriptionSourceId = sp.get("subscription_source_id") || "";
  const downloadJobId = sp.get("download_job_id") || "";
  const search = sp.get("q") || "";
  const rawPage = Number.parseInt(sp.get("page") || "1", 10);
  const page = Number.isFinite(rawPage) && rawPage > 0 ? rawPage : 1;
  const taskOffset = (page - 1) * pageSize;
  const selectedDownloadJobId = sp.get("job");
  const selectedImportJobId = sp.get("import_job");
  const selectedTaskId = sp.get("task");
  const selectedJobId = selectedTaskId ? null : selectedImportJobId || selectedDownloadJobId;
  return {
    activeTab, subscriptionSourceId, downloadJobId, search, page, taskOffset,
    selectedDownloadJobId, selectedImportJobId, selectedTaskId, selectedJobId,
  };
}

export function updatedJobsHref(
  pathname: string,
  sp: URLSearchParams,
  updates: Record<string, string | null>,
): string {
  const next = new URLSearchParams(sp.toString());
  Object.entries(updates).forEach(([key, value]) => {
    if (!value) next.delete(key); else next.set(key, value);
  });
  return next.toString() ? `${pathname}?${next.toString()}` : pathname;
}

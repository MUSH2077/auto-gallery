export type TaskListScope = "all" | "attention" | "running" | "waiting";

const TAB_KIND: Record<string, string> = { downloads: "download", imports: "import", admin: "admin" };

function routeScope(value: string | null): TaskListScope {
  return value === "attention" || value === "running" || value === "waiting" ? value : "all";
}

export function updatedTaskListHref(pathname: string, current: URLSearchParams, patch: Record<string, string | null>, resetPage = false) {
  const next = new URLSearchParams(current.toString());
  const route = parseTaskListRoute(current);
  // Promote legacy aliases before changing one filter so the others survive.
  if (current.has("tab")) { next.delete("tab"); if (route.kind) next.set("kind", route.kind); if (route.records) next.set("records", route.records); }
  if (current.has("view")) { next.delete("view"); if (route.scope !== "all") next.set("scope", route.scope); }
  if (current.has("page")) { next.delete("page"); if (route.page) next.set("p", String(route.page)); }
  if ("kind" in patch && (patch.kind || "") !== route.kind) {
    next.delete("download_job_id"); next.delete("subscription_source_id"); next.delete("records");
  }
  for (const [key, value] of Object.entries(patch)) {
    if (value === null || value === "") next.delete(key);
    else next.set(key, value);
  }
  if (resetPage) {
    next.delete("p");
    if (!("task" in patch)) next.delete("task");
  }
  return next.toString() ? `${pathname}?${next}` : pathname;
}

/** Read both the gallery query contract and the legacy Jobs query contract. */
export function parseTaskListRoute(sp: URLSearchParams) {
  const tab = sp.get("tab");
  let records = sp.get("records") || (tab === "downloads" ? "download" : tab === "imports" ? "import" : "");
  const search = sp.get("q") || "";
  const downloadJobFilterId = sp.get("download_job_id") || "";
  const subscriptionSourceId = sp.get("subscription_source_id") || "";
  const q = search;
  const kind = sp.get("kind") || (tab ? TAB_KIND[tab] || "" : "");
  const scope = routeScope(sp.get("scope") || sp.get("view"));
  if (!records && kind === "import" && downloadJobFilterId) records = "import";
  if (!records && kind === "download" && subscriptionSourceId) records = "download";
  const status = sp.get("status") || "";
  const source = sp.get("source") || "";
  const sort = sp.get("sort") || "created-desc";
  const order = sp.get("order") || "";
  const rawPage = Number(sp.get("p") ?? (sp.get("page") ? Number(sp.get("page")) - 1 : 0));
  const page = Number.isSafeInteger(rawPage) && rawPage >= 0 ? rawPage : 0;
  return {
    q,
    search,
    kind,
    records: records === "download" || records === "import" ? records : "",
    status,
    source,
    sort,
    order,
    scope,
    page,
    taskId: sp.get("task"),
    downloadJobId: sp.get("job"),
    importJobId: sp.get("import_job"),
    downloadJobFilterId: downloadJobFilterId || null,
    subscriptionSourceId: subscriptionSourceId || null,
  };
}

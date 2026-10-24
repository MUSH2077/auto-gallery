import { queryKeys } from "@/lib/api";
export const TASK_PAGE_SIZE = 12;
export const taskListKey = (q = "", kind = "", status = "", scope = "all", page = 0) => [...queryKeys.tasks.all, "gallery", q, kind, status, scope, page] as const;

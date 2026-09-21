export const JOB_EVENT_QUERY_KEYS = [
  ["system", "workbench"],
  ["download-jobs"],
  ["import-jobs"],
  ["tasks"],
  ["tasks", "operations", "attention"],
  ["admin-operation-task"],
  ["admin-operation-snapshot"],
  ["notifications"],
] as const;

type QueryInvalidator = {
  invalidateQueries: (filters: { queryKey: readonly unknown[] }) => Promise<unknown>;
};

export async function invalidateJobEventQueries(queryClient: QueryInvalidator): Promise<void> {
  await Promise.all(
    JOB_EVENT_QUERY_KEYS.map((queryKey) => queryClient.invalidateQueries({ queryKey })),
  );
}

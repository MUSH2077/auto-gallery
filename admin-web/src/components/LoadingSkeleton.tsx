export function CardSkeleton() {
  return <div className="card p-4 animate-pulse"><div className="mb-2 h-4 w-3/4 rounded bg-subtle dark:bg-subtle" /><div className="h-3 w-1/2 rounded bg-subtle dark:bg-subtle" /></div>;
}

export function TableSkeleton({ rows = 5 }: { rows?: number }) {
  return (
    <div className="animate-pulse space-y-2">
      {Array.from({ length: rows }).map((_, i) => (
        <div key={i} className="h-10 rounded-md bg-subtle dark:bg-subtle" />
      ))}
    </div>
  );
}

type Status = "up" | "down" | "running" | "pending" | "failed" | "completed" | "stale" | "unknown";

const colors: Record<Status, string> = {
  up: "bg-green-100 text-green-800 border-green-300",
  down: "bg-red-100 text-red-800 border-red-300",
  running: "bg-blue-100 text-blue-800 border-blue-300",
  pending: "bg-yellow-100 text-yellow-800 border-yellow-300",
  failed: "bg-red-100 text-red-800 border-red-300",
  completed: "bg-green-100 text-green-800 border-green-300",
  stale: "bg-gray-100 text-gray-600 border-gray-300",
  unknown: "bg-gray-100 text-gray-500 border-gray-200",
};

export default function StatusBadge({ status }: { status: string }) {
  const s = (status || "unknown").toLowerCase() as Status;
  return (
    <span className={`inline-flex items-center gap-1.5 px-2.5 py-0.5 rounded-full text-xs font-medium border ${colors[s] || colors.unknown}`}>
      <span className={`w-1.5 h-1.5 rounded-full ${s === "up" || s === "completed" ? "bg-green-500" : s === "down" || s === "failed" ? "bg-red-500" : s === "running" ? "bg-blue-500 animate-pulse" : s === "pending" ? "bg-yellow-500" : "bg-gray-400"}`} />
      {status}
    </span>
  );
}

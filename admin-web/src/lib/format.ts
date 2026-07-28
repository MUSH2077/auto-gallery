// Shared formatting utilities
export function formatBytes(bytes: number): string {
  if (bytes === 0) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  const i = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
  return `${(bytes / Math.pow(1024, i)).toFixed(i === 0 ? 0 : 1)} ${units[i]}`;
}

export function shortId(id: string): string {
  return id.slice(0, 8);
}

export function formatDate(date: string | Date, locale: string = "zh-CN"): string {
  return new Date(date).toLocaleDateString(locale);
}

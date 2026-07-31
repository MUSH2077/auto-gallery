export type MediaKind = "image" | "animated_image" | "video" | "archive" | "unknown";

export interface MediaAssetData {
  id: string;
  file_name?: string;
  file_path?: string;
  file_size?: number;
  width?: number;
  height?: number;
  duration?: number;
  mime_type?: string;
  media_kind?: MediaKind;
  thumb_sm_path?: string;
  thumb_md_path?: string;
  thumb_lg_path?: string;
  thumb_url?: string;
  poster_url?: string;
  preview_url?: string;
  original_url?: string;
  created_at?: string;
}

export function resolveMediaKind(asset: Pick<MediaAssetData, "media_kind" | "mime_type" | "file_name">): MediaKind {
  if (asset.media_kind) return asset.media_kind;
  const mime = (asset.mime_type || "").toLowerCase();
  const name = (asset.file_name || "").toLowerCase();
  if (mime.startsWith("video/") || name.endsWith(".mp4") || name.endsWith(".webm")) return "video";
  if (mime === "image/gif" || mime === "image/apng" || name.endsWith(".gif") || name.endsWith(".apng")) {
    return "animated_image";
  }
  if (mime.startsWith("image/") || /\.(jpe?g|png|webp|bmp)$/.test(name)) return "image";
  if (mime === "application/zip" || name.endsWith(".zip")) return "archive";
  return "unknown";
}

export function isBrowserPlayableVideo(
  asset: Pick<MediaAssetData, "mime_type" | "file_name">,
): boolean {
  const mime = (asset.mime_type || "").toLowerCase().split(";", 1)[0].trim();
  const name = (asset.file_name || "").toLowerCase();
  return mime === "video/mp4"
    || mime === "video/webm"
    || name.endsWith(".mp4")
    || name.endsWith(".webm");
}

export function formatMediaDuration(seconds?: number | null): string | null {
  if (seconds === undefined || seconds === null || !Number.isFinite(seconds) || seconds < 0) return null;
  const rounded = Math.floor(seconds);
  const hours = Math.floor(rounded / 3600);
  const minutes = Math.floor((rounded % 3600) / 60);
  const remaining = rounded % 60;
  return hours > 0
    ? `${hours}:${String(minutes).padStart(2, "0")}:${String(remaining).padStart(2, "0")}`
    : `${minutes}:${String(remaining).padStart(2, "0")}`;
}

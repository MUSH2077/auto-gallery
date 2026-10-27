"use client";
import { ChevronLeft, ChevronRight } from "lucide-react";
import type { DerivativeStatus, MediaKind } from "@/lib/media";

export interface AssetData {
  id: string;
  file_name: string;
  file_path: string;
  width?: number;
  height?: number;
  mime_type?: string;
  media_kind?: MediaKind;
  duration?: number;
  thumb_sm_path?: string;
  thumb_url?: string;
  preview_url?: string;
  poster_url?: string;
  original_url?: string;
  derivative_status?: DerivativeStatus;
  created_at: string;
}

export function ArrowIcon({ direction }: { direction: "left" | "right" }) {
  return direction === "left"
    ? <ChevronLeft className="h-5 w-5" aria-hidden="true" />
    : <ChevronRight className="h-5 w-5" aria-hidden="true" />;
}

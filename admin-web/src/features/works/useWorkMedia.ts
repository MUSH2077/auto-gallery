"use client";

import { useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { pollInterval } from "@/lib/polling";
import type { AssetData } from "@/components/WorkViewerParts";

export function useWorkMedia(workId: string) {
  const assets = useQuery({
    queryKey: ["works", workId, "assets"],
    queryFn: () => api.getWorkAssets(workId),
    refetchInterval: query => query.state.data?.some(
      asset => asset.derivative_status === "pending" || asset.derivative_status === "processing",
    ) ? pollInterval(true) : false,
    refetchIntervalInBackground: false,
  });
  const [activeIndex, setActiveIndex] = useState(0);
  const [fullAsset, setFullAsset] = useState<AssetData | null>(null);
  const viewerRef = useRef<HTMLDivElement | null>(null);

  return {
    assets,
    activeIndex,
    setActiveIndex,
    fullAsset,
    setFullAsset,
    viewerRef,
    totalPages: assets.data?.length || 0,
  };
}

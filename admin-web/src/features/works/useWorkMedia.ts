"use client";
import {useEffect,useRef,useState} from 'react';
import {useQuery} from '@tanstack/react-query';
import {api} from '@/lib/api';
import {useT} from '@/lib/i18n';
import {pollInterval} from '@/lib/polling';
import type {AssetData} from '@/components/WorkViewerParts';
export function useWorkMedia(workId:string, options: { bindKeyboard?: boolean; bindWheel?: boolean } = {}){
  const bindKeyboard = options.bindKeyboard !== false;
  const bindWheel = options.bindWheel !== false;
  const t = useT();
  const assets = useQuery({
    queryKey: ["works", workId, "assets"],
    queryFn: () => api.getWorkAssets(workId),
    refetchInterval: (query) => query.state.data?.some(
      (asset) => asset.derivative_status === "pending" || asset.derivative_status === "processing",
    ) ? pollInterval(true) : false,
    refetchIntervalInBackground: false,
  });
  const [activeIndex, setActiveIndex] = useState(0);
  const [fullAsset, setFullAsset] = useState<AssetData | null>(null);
  const wheelDelta = useRef(0);
  const viewerRef = useRef<HTMLDivElement | null>(null);

  const totalPages = assets.data?.length || 0;
  const changePage = (delta: number) => {
    if (!totalPages) return;
    setActiveIndex((current) => (current + delta + totalPages) % totalPages);
  };

  useEffect(() => {
    const viewer = viewerRef.current;
    if (!bindWheel || !viewer || totalPages <= 1) return;
    const onWheel = (event: WheelEvent) => {
      if ((event.target as HTMLElement).closest("video")) return;
      wheelDelta.current += event.deltaY;
      if (Math.abs(wheelDelta.current) < 80) return;
      event.preventDefault();
      setActiveIndex((current) => (current + (wheelDelta.current > 0 ? 1 : -1) + totalPages) % totalPages);
      wheelDelta.current = 0;
    };
    viewer.addEventListener("wheel", onWheel, { passive: false });
    return () => viewer.removeEventListener("wheel", onWheel);
  }, [bindWheel, totalPages]);

  useEffect(() => {
    if (!bindKeyboard || !totalPages) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.defaultPrevented) return;
      const target = event.target as HTMLElement | null;
      if (target?.closest("video, input, textarea, select, button, a, [role='slider']")) return;
      if (event.key === "ArrowLeft") changePage(-1);
      if (event.key === "ArrowRight") changePage(1);
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [bindKeyboard, totalPages]);


return {t,assets,activeIndex,setActiveIndex,fullAsset,setFullAsset,wheelDelta,viewerRef,totalPages,changePage};
}

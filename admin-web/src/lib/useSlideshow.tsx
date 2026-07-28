"use client";
import { useCallback, useState, type ReactNode } from "react";
import { SlideshowPlayer, type SlideItem } from "@/components/SlideshowPlayer";

export function useSlideshow(): { open: (items: SlideItem[], startIndex?: number) => void; node: ReactNode } {
  const [state, setState] = useState<{ items: SlideItem[]; startIndex: number; open: boolean }>({
    items: [],
    startIndex: 0,
    open: false,
  });

  const open = useCallback((items: SlideItem[], startIndex = 0) => {
    setState({ items, startIndex, open: true });
  }, []);

  const node = (
    <SlideshowPlayer
      items={state.items}
      startIndex={state.startIndex}
      open={state.open}
      onClose={() => setState((s) => ({ ...s, open: false }))}
    />
  );

  return { open, node };
}

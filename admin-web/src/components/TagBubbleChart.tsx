"use client";

import { packSiblings } from "d3-hierarchy";
import Link from "next/link";
import { useRouter } from "next/navigation";
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type KeyboardEvent as ReactKeyboardEvent,
  type PointerEvent as ReactPointerEvent,
} from "react";

import { useChartTheme } from "@/components/charts/useChartTheme";
import type { Tag } from "@/lib/api";
import { useT } from "@/lib/i18n";
import { quoteSearchValue, searchUrl } from "@/lib/search-query";

interface BubbleNode {
  data: Tag;
  r: number;
  x: number;
  y: number;
}

interface BubbleLayout {
  nodes: BubbleNode[];
  width: number;
  height: number;
  buckets: Map<string, BubbleNode[]>;
}

interface ViewState {
  centerX: number;
  centerY: number;
  scale: number;
}

interface VisibleNode {
  node: BubbleNode;
  x: number;
  y: number;
  radius: number;
}

interface HoverState {
  node: BubbleNode;
  x: number;
  y: number;
}

const LAYOUT_PADDING = 48;
const SPATIAL_BUCKET_SIZE = 192;
const MAX_LABEL_NODES = 1_600;
const LABEL_RADIUS_THRESHOLD = 13;

function hashString(value: string): number {
  let hash = 0;
  for (let index = 0; index < value.length; index += 1) {
    hash = ((hash << 5) - hash) + value.charCodeAt(index);
    hash |= 0;
  }
  return Math.abs(hash);
}

function bubbleRadius(tag: Tag, minCount: number, maxCount: number): number {
  if (maxCount <= minCount) return 34;
  const minRoot = Math.sqrt(Math.max(0, minCount));
  const maxRoot = Math.sqrt(Math.max(1, maxCount));
  const ratio = (Math.sqrt(Math.max(0, tag.usage_count)) - minRoot) / (maxRoot - minRoot);
  return 22 + Math.max(0, Math.min(1, ratio)) * 66;
}

function bucketKey(x: number, y: number): string {
  return `${Math.floor(x / SPATIAL_BUCKET_SIZE)}:${Math.floor(y / SPATIAL_BUCKET_SIZE)}`;
}

function findNodeAt(layout: BubbleLayout, x: number, y: number): BubbleNode | null {
  const bucketX = Math.floor(x / SPATIAL_BUCKET_SIZE);
  const bucketY = Math.floor(y / SPATIAL_BUCKET_SIZE);
  for (let offsetY = -1; offsetY <= 1; offsetY += 1) {
    for (let offsetX = -1; offsetX <= 1; offsetX += 1) {
      const nodes = layout.buckets.get(`${bucketX + offsetX}:${bucketY + offsetY}`) || [];
      for (const node of nodes) {
        const dx = x - node.x;
        const dy = y - node.y;
        if ((dx * dx) + (dy * dy) <= node.r * node.r) return node;
      }
    }
  }
  return null;
}

function buildLayout(tags: Tag[]): BubbleLayout {
  if (!tags.length) {
    return { nodes: [], width: 0, height: 0, buckets: new Map() };
  }
  let minCount = Number.POSITIVE_INFINITY;
  let maxCount = 0;
  for (const tag of tags) {
    minCount = Math.min(minCount, tag.usage_count);
    maxCount = Math.max(maxCount, tag.usage_count);
  }
  const nodes: BubbleNode[] = tags
    .map((tag) => ({
      data: tag,
      r: bubbleRadius(tag, minCount, maxCount),
      x: 0,
      y: 0,
    }))
    .sort((left, right) => (
      right.r - left.r
      || right.data.usage_count - left.data.usage_count
      || left.data.normalized_name.localeCompare(right.data.normalized_name)
    ));

  packSiblings(nodes);
  let minX = Number.POSITIVE_INFINITY;
  let maxX = Number.NEGATIVE_INFINITY;
  let minY = Number.POSITIVE_INFINITY;
  let maxY = Number.NEGATIVE_INFINITY;
  for (const node of nodes) {
    minX = Math.min(minX, node.x - node.r);
    maxX = Math.max(maxX, node.x + node.r);
    minY = Math.min(minY, node.y - node.r);
    maxY = Math.max(maxY, node.y + node.r);
  }

  const buckets = new Map<string, BubbleNode[]>();
  for (const node of nodes) {
    node.x += LAYOUT_PADDING - minX;
    node.y += LAYOUT_PADDING - minY;
    const key = bucketKey(node.x, node.y);
    const bucket = buckets.get(key);
    if (bucket) bucket.push(node);
    else buckets.set(key, [node]);
  }
  return {
    nodes,
    width: Math.ceil(maxX - minX + LAYOUT_PADDING * 2),
    height: Math.ceil(maxY - minY + LAYOUT_PADDING * 2),
    buckets,
  };
}

export default function TagBubbleChart({
  tags,
  ariaLabel,
}: {
  tags: Tag[];
  ariaLabel: string;
}) {
  const t = useT();
  const router = useRouter();
  const theme = useChartTheme();
  const viewportRef = useRef<HTMLDivElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const dragRef = useRef<{ x: number; y: number; moved: boolean } | null>(null);
  const [viewport, setViewport] = useState({ width: 960, height: 640 });
  const [view, setView] = useState<ViewState>({ centerX: 0, centerY: 0, scale: 1 });
  const [hovered, setHovered] = useState<HoverState | null>(null);
  const [dragging, setDragging] = useState(false);
  const layout = useMemo(() => buildLayout(tags), [tags]);

  useEffect(() => {
    const element = viewportRef.current;
    if (!element) return;
    const update = () => {
      const width = Math.max(280, Math.round(element.clientWidth));
      const height = Math.max(360, Math.round(element.clientHeight));
      setViewport((current) => (
        current.width === width && current.height === height
          ? current
          : { width, height }
      ));
    };
    update();
    const observer = new ResizeObserver(update);
    observer.observe(element);
    return () => observer.disconnect();
  }, []);

  const fitScale = useMemo(() => {
    if (!layout.width || !layout.height) return 1;
    return Math.min(
      viewport.width / layout.width,
      viewport.height / layout.height,
    ) * 0.94;
  }, [layout.height, layout.width, viewport.height, viewport.width]);

  const clampView = useCallback((candidate: ViewState): ViewState => {
    const minimumScale = Math.max(0.001, fitScale);
    const maximumScale = Math.max(4, minimumScale * 12);
    const scale = Math.max(minimumScale, Math.min(maximumScale, candidate.scale));
    const halfWidth = viewport.width / (2 * scale);
    const halfHeight = viewport.height / (2 * scale);
    const centerX = layout.width <= halfWidth * 2
      ? layout.width / 2
      : Math.max(halfWidth, Math.min(layout.width - halfWidth, candidate.centerX));
    const centerY = layout.height <= halfHeight * 2
      ? layout.height / 2
      : Math.max(halfHeight, Math.min(layout.height - halfHeight, candidate.centerY));
    return { centerX, centerY, scale };
  }, [fitScale, layout.height, layout.width, viewport.height, viewport.width]);

  const resetView = useCallback(() => {
    setView({
      centerX: layout.width / 2,
      centerY: layout.height / 2,
      scale: fitScale,
    });
    setHovered(null);
  }, [fitScale, layout.height, layout.width]);

  useEffect(() => {
    resetView();
  }, [resetView]);

  const zoomAt = useCallback((factor: number, screenX: number, screenY: number) => {
    setView((current) => {
      const worldX = current.centerX + (screenX - viewport.width / 2) / current.scale;
      const worldY = current.centerY + (screenY - viewport.height / 2) / current.scale;
      const nextScale = current.scale * factor;
      return clampView({
        centerX: worldX - (screenX - viewport.width / 2) / nextScale,
        centerY: worldY - (screenY - viewport.height / 2) / nextScale,
        scale: nextScale,
      });
    });
    setHovered(null);
  }, [clampView, viewport.height, viewport.width]);

  useEffect(() => {
    const element = viewportRef.current;
    if (!element) return;
    const handleWheel = (event: WheelEvent) => {
      if (!event.ctrlKey) return;
      event.preventDefault();
      const rect = element.getBoundingClientRect();
      const factor = Math.exp(-event.deltaY * 0.002);
      zoomAt(factor, event.clientX - rect.left, event.clientY - rect.top);
    };
    element.addEventListener("wheel", handleWheel, { passive: false });
    return () => element.removeEventListener("wheel", handleWheel);
  }, [zoomAt]);

  const visibleNodes = useMemo(() => {
    const nodes: VisibleNode[] = [];
    for (const node of layout.nodes) {
      const x = (node.x - view.centerX) * view.scale + viewport.width / 2;
      const y = (node.y - view.centerY) * view.scale + viewport.height / 2;
      const radius = node.r * view.scale;
      if (
        radius >= LABEL_RADIUS_THRESHOLD
        && x + radius >= 0
        && y + radius >= 0
        && x - radius <= viewport.width
        && y - radius <= viewport.height
      ) {
        nodes.push({ node, x, y, radius });
        if (nodes.length >= MAX_LABEL_NODES) break;
      }
    }
    return nodes;
  }, [layout.nodes, view, viewport.height, viewport.width]);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas || !viewport.width || !viewport.height) return;
    const pixelRatio = Math.min(window.devicePixelRatio || 1, 2);
    const pixelWidth = Math.round(viewport.width * pixelRatio);
    const pixelHeight = Math.round(viewport.height * pixelRatio);
    if (canvas.width !== pixelWidth || canvas.height !== pixelHeight) {
      canvas.width = pixelWidth;
      canvas.height = pixelHeight;
    }
    const context = canvas.getContext("2d");
    if (!context) return;
    context.setTransform(pixelRatio, 0, 0, pixelRatio, 0, 0);
    context.clearRect(0, 0, viewport.width, viewport.height);
    context.fillStyle = theme.surface;
    context.fillRect(0, 0, viewport.width, viewport.height);
    const dark = document.documentElement.classList.contains("dark");

    for (const node of layout.nodes) {
      const x = (node.x - view.centerX) * view.scale + viewport.width / 2;
      const y = (node.y - view.centerY) * view.scale + viewport.height / 2;
      const radius = node.r * view.scale;
      if (
        radius < 0.35
        || x + radius < 0
        || y + radius < 0
        || x - radius > viewport.width
        || y - radius > viewport.height
      ) continue;
      const hue = hashString(node.data.category || node.data.normalized_name) % 360;
      const isHovered = hovered?.node.data.id === node.data.id;
      context.beginPath();
      context.arc(x, y, Math.max(0.7, radius), 0, Math.PI * 2);
      context.fillStyle = dark
        ? `hsl(${hue} 38% ${isHovered ? 34 : 24}%)`
        : `hsl(${hue} 58% ${isHovered ? 82 : 90}%)`;
      context.fill();
      if (radius >= 3) {
        context.strokeStyle = dark
          ? `hsl(${hue} 42% ${isHovered ? 62 : 42}%)`
          : `hsl(${hue} 45% ${isHovered ? 54 : 76}%)`;
        context.lineWidth = isHovered ? 2 : 1;
        context.stroke();
      }
    }
  }, [hovered?.node.data.id, layout.nodes, theme.surface, view, viewport.height, viewport.width]);

  const worldPoint = useCallback((event: ReactPointerEvent<HTMLCanvasElement>) => {
    const rect = event.currentTarget.getBoundingClientRect();
    const screenX = event.clientX - rect.left;
    const screenY = event.clientY - rect.top;
    return {
      screenX,
      screenY,
      x: view.centerX + (screenX - viewport.width / 2) / view.scale,
      y: view.centerY + (screenY - viewport.height / 2) / view.scale,
    };
  }, [view, viewport.height, viewport.width]);

  const handlePointerDown = (event: ReactPointerEvent<HTMLCanvasElement>) => {
    if (event.button !== 0) return;
    event.currentTarget.setPointerCapture(event.pointerId);
    dragRef.current = { x: event.clientX, y: event.clientY, moved: false };
    setDragging(true);
  };

  const handlePointerMove = (event: ReactPointerEvent<HTMLCanvasElement>) => {
    const drag = dragRef.current;
    if (drag) {
      const deltaX = event.clientX - drag.x;
      const deltaY = event.clientY - drag.y;
      if (Math.abs(deltaX) + Math.abs(deltaY) > 2) drag.moved = true;
      drag.x = event.clientX;
      drag.y = event.clientY;
      setView((current) => clampView({
        ...current,
        centerX: current.centerX - deltaX / current.scale,
        centerY: current.centerY - deltaY / current.scale,
      }));
      setHovered(null);
      return;
    }
    const point = worldPoint(event);
    const node = findNodeAt(layout, point.x, point.y);
    setHovered(node ? { node, x: point.screenX, y: point.screenY } : null);
  };

  const handlePointerUp = (event: ReactPointerEvent<HTMLCanvasElement>) => {
    const drag = dragRef.current;
    dragRef.current = null;
    setDragging(false);
    if (event.currentTarget.hasPointerCapture(event.pointerId)) {
      event.currentTarget.releasePointerCapture(event.pointerId);
    }
    if (!drag?.moved) {
      const point = worldPoint(event);
      const node = findNodeAt(layout, point.x, point.y);
      if (node) {
        router.push(searchUrl(
          "/admin/works",
          `type:work tag:${quoteSearchValue(node.data.normalized_name)}`,
        ));
      }
    }
  };

  const handleKeyDown = (event: ReactKeyboardEvent<HTMLCanvasElement>) => {
    if (event.key === "+" || event.key === "=") {
      event.preventDefault();
      zoomAt(1.25, viewport.width / 2, viewport.height / 2);
    } else if (event.key === "-") {
      event.preventDefault();
      zoomAt(0.8, viewport.width / 2, viewport.height / 2);
    } else if (event.key === "0") {
      event.preventDefault();
      resetView();
    }
  };

  const zoomRatio = fitScale > 0 ? view.scale / fitScale : 1;

  return (
    <div
      ref={viewportRef}
      className="relative h-[68vh] min-h-[360px] max-h-[820px] w-full overflow-hidden rounded-lg border border-border bg-surface sm:min-h-[520px]"
      data-testid="tag-bubble-chart"
      data-tag-count={tags.length}
      data-zoom-level={zoomRatio.toFixed(3)}
    >
      <canvas
        ref={canvasRef}
        role="img"
        aria-label={ariaLabel}
        tabIndex={0}
        className={`absolute inset-0 h-full w-full touch-none outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-accent ${dragging ? "cursor-grabbing" : hovered ? "cursor-pointer" : "cursor-grab"}`}
        onKeyDown={handleKeyDown}
        onPointerDown={handlePointerDown}
        onPointerMove={handlePointerMove}
        onPointerUp={handlePointerUp}
        onPointerCancel={() => { dragRef.current = null; setDragging(false); }}
        onPointerLeave={() => { if (!dragRef.current) setHovered(null); }}
      />

      <div className="pointer-events-none absolute bottom-3 left-3 z-20 rounded-md border border-border/80 bg-surface/90 px-2.5 py-1.5 text-xs text-muted shadow-sm backdrop-blur-sm sm:bottom-auto sm:top-3">
        {t("tags.zoom_hint")}
      </div>
      <div className="absolute right-3 top-3 z-20 flex items-center gap-1 rounded-md border border-border/80 bg-surface/90 p-1 shadow-sm backdrop-blur-sm">
        <button
          type="button"
          className="inline-flex h-9 w-9 items-center justify-center rounded text-lg text-fg hover:bg-subtle"
          aria-label={t("tags.zoom_out")}
          onClick={() => zoomAt(0.8, viewport.width / 2, viewport.height / 2)}
        >
          −
        </button>
        <button
          type="button"
          className="min-w-16 rounded px-2 py-1 text-xs font-medium text-muted hover:bg-subtle hover:text-fg"
          aria-label={t("tags.zoom_reset")}
          onClick={resetView}
        >
          {Math.round(zoomRatio * 100)}%
        </button>
        <button
          type="button"
          className="inline-flex h-9 w-9 items-center justify-center rounded text-lg text-fg hover:bg-subtle"
          aria-label={t("tags.zoom_in")}
          onClick={() => zoomAt(1.25, viewport.width / 2, viewport.height / 2)}
        >
          +
        </button>
      </div>

      {visibleNodes.map(({ node, x, y, radius }) => {
        const style = {
          left: x - radius,
          top: y - radius,
          width: radius * 2,
          height: radius * 2,
          fontSize: `${Math.max(10, Math.min(18, radius * 0.24))}px`,
        } as CSSProperties;
        return (
          <Link
            key={node.data.id}
            href={searchUrl(
              "/admin/works",
              `type:work tag:${quoteSearchValue(node.data.normalized_name)}`,
            )}
            title={`${node.data.normalized_name} · ${node.data.category || "tag"} · ${node.data.usage_count}`}
            aria-label={`${node.data.normalized_name}, ${node.data.category || "tag"}, ${node.data.usage_count}`}
            className="absolute z-[1] flex flex-col items-center justify-center overflow-hidden rounded-full text-center text-fg outline-none hover:bg-white/10 focus-visible:ring-2 focus-visible:ring-accent"
            style={style}
            onPointerDown={(event) => event.stopPropagation()}
          >
            <span className="max-w-[82%] truncate font-semibold leading-tight">
              {node.data.normalized_name}
            </span>
            {radius >= 18 ? (
              <span className="mt-0.5 text-[10px] font-medium text-muted">{node.data.usage_count}</span>
            ) : null}
          </Link>
        );
      })}

      {hovered ? (
        <div
          className="pointer-events-none absolute z-10 max-w-64 rounded-md border border-border bg-surface px-2.5 py-2 text-xs text-fg shadow-overlay"
          style={{
            left: Math.min(viewport.width - 220, hovered.x + 14),
            top: Math.min(viewport.height - 72, hovered.y + 14),
          }}
        >
          <p className="truncate font-semibold">{hovered.node.data.normalized_name}</p>
          <p className="mt-0.5 text-muted">
            {hovered.node.data.category || "tag"} · {hovered.node.data.usage_count}
          </p>
        </div>
      ) : null}
    </div>
  );
}

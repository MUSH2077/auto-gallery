"use client";

import { packSiblings } from "d3-hierarchy";
import Link from "next/link";
import { useEffect, useMemo, useRef, useState, type CSSProperties } from "react";

import type { Tag } from "@/lib/api";
import { bubbleStaggerDelay, motionConfig, motionTokens, useEnterOnce } from "@/lib/motion";
import { quoteSearchValue, searchUrl } from "@/lib/search-query";

interface BubbleNode {
  data: Tag;
  r: number;
  x: number;
  y: number;
}

function hashString(value: string): number {
  let hash = 0;
  for (let index = 0; index < value.length; index += 1) {
    hash = ((hash << 5) - hash) + value.charCodeAt(index);
    hash |= 0;
  }
  return Math.abs(hash);
}

function bubbleRadius(tag: Tag, minCount: number, maxCount: number, maxRadius: number): number {
  if (maxCount <= minCount) return Math.min(maxRadius, 34);
  const minRoot = Math.sqrt(Math.max(0, minCount));
  const maxRoot = Math.sqrt(Math.max(1, maxCount));
  const ratio = (Math.sqrt(Math.max(0, tag.usage_count)) - minRoot) / (maxRoot - minRoot);
  return 22 + Math.max(0, Math.min(1, ratio)) * (maxRadius - 22);
}

export default function TagBubbleChart({
  tags,
  ariaLabel,
}: {
  tags: Tag[];
  ariaLabel: string;
}) {
  const wrapperRef = useRef<HTMLDivElement>(null);
  const [availableWidth, setAvailableWidth] = useState(720);
  const [motionEnabled, setMotionEnabled] = useState(true);
  const tagKeys = useMemo(() => tags.map((tag) => tag.id), [tags]);
  const isNew = useEnterOnce(tagKeys, motionTokens.stagger.bubbleCap);

  useEffect(() => {
    const wrapper = wrapperRef.current;
    if (!wrapper) return;
    const update = () => setAvailableWidth(Math.max(280, wrapper.clientWidth));
    update();
    const observer = new ResizeObserver(update);
    observer.observe(wrapper);
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    setMotionEnabled(motionConfig.shouldAnimate());
  }, []);

  const layout = useMemo(() => {
    if (!tags.length) return { nodes: [] as BubbleNode[], width: availableWidth, height: 0 };
    const counts = tags.map((tag) => tag.usage_count);
    const minCount = Math.min(...counts);
    const maxCount = Math.max(...counts);
    const maxRadius = Math.max(54, Math.min(88, availableWidth * 0.15));
    const nodes: BubbleNode[] = tags
      .map((tag) => ({
        data: tag,
        r: bubbleRadius(tag, minCount, maxCount, maxRadius),
        x: 0,
        y: 0,
      }))
      .sort((left, right) => (
        right.r - left.r
        || right.data.usage_count - left.data.usage_count
        || left.data.normalized_name.localeCompare(right.data.normalized_name)
      ));

    packSiblings(nodes);
    const padding = 8;
    const minX = Math.min(...nodes.map((node) => node.x - node.r));
    const maxX = Math.max(...nodes.map((node) => node.x + node.r));
    const minY = Math.min(...nodes.map((node) => node.y - node.r));
    const maxY = Math.max(...nodes.map((node) => node.y + node.r));
    const packedWidth = Math.ceil(maxX - minX + padding * 2);
    const packedHeight = Math.ceil(maxY - minY + padding * 2);
    const width = Math.max(availableWidth, packedWidth);
    const offsetX = (width - packedWidth) / 2 + padding - minX;
    const offsetY = padding - minY;
    for (const node of nodes) {
      node.x += offsetX;
      node.y += offsetY;
    }
    return { nodes, width, height: packedHeight };
  }, [availableWidth, tags]);

  return (
    <div ref={wrapperRef} className="overflow-x-auto py-2">
      <div
        role="list"
        aria-label={ariaLabel}
        className="relative mx-auto"
        style={{ width: layout.width, height: layout.height }}
      >
        {layout.nodes.map((node, index) => {
          const hue = hashString(node.data.category || node.data.normalized_name) % 360;
          const enter = motionEnabled && isNew(node.data.id);
          const diameter = node.r * 2;
          const style = {
            left: node.x - node.r,
            top: node.y - node.r,
            width: diameter,
            height: diameter,
            "--bubble-hue": hue,
            "--bubble-delay": bubbleStaggerDelay(index),
          } as CSSProperties;
          const showCategory = node.r >= 58 && node.data.category;
          return (
            <div
              role="listitem"
              key={node.data.id}
              className={`absolute rounded-full ${enter ? "tag-bubble-enter" : ""}`}
              style={style}
            >
              <Link
                href={searchUrl("/admin/works", `type:work tag:${quoteSearchValue(node.data.normalized_name)}`)}
                title={`${node.data.normalized_name} · ${node.data.category || "tag"} · ${node.data.usage_count}`}
                aria-label={`${node.data.normalized_name}, ${node.data.category || "tag"}, ${node.data.usage_count}`}
                className="tag-bubble flex h-full w-full flex-col items-center justify-center overflow-hidden rounded-full border text-center focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent focus-visible:ring-offset-2"
              >
                <span
                  className="max-w-[82%] truncate font-semibold leading-tight"
                  style={{ fontSize: `${Math.max(11, Math.min(20, node.r * 0.25))}px` }}
                >
                  {node.data.normalized_name}
                </span>
                <span className="mt-0.5 text-[11px] font-medium opacity-70">{node.data.usage_count}</span>
                {showCategory && <span className="mt-0.5 text-[10px] opacity-55">{node.data.category}</span>}
              </Link>
            </div>
          );
        })}
      </div>
    </div>
  );
}

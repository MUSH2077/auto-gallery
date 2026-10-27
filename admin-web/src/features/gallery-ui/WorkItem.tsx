'use client';
import { type MouseEventHandler, type ReactNode, useEffect, useRef, useState } from 'react';
import { Icon } from './Icon';
import { useGalleryCopy } from './copy';
export interface WorkItemProps { preview?: { enabled: boolean; delayMs: number; wheelThreshold?: number; assetIds: string[]; pageIndex?: number; onOpen: (preview: { workId: string; anchor: DOMRect; assetIds: string[]; pageIndex: number }) => void; onEnter: () => void; onLeave: () => void; onPage: (pageIndex: number) => void }; id: string; title: string; creator: string; source: string; dateTime: string; dateLabel: string; ratio: string; assets: number; adult: boolean; ai: boolean; href: string; onOpen: MouseEventHandler<HTMLAnchorElement>; media: ReactNode; selected: boolean; onSelect?: () => void; favorite?: {active: boolean; busy: boolean; disabled: boolean; error?: string; onToggle: () => void}; trashActions?: ReactNode; }
export function WorkItem({preview,id,title,creator,source,dateTime,dateLabel,ratio,assets,adult,ai,href,onOpen,media,selected,onSelect,favorite,trashActions}: WorkItemProps) {
  const cardRef = useRef<HTMLElement>(null);
  const timer = useRef<number | null>(null);
  const [pageIndex, setPageIndex] = useState(preview?.pageIndex || 0);
  const [previewOpen, setPreviewOpen] = useState(false);
  const wheelDelta = useRef(0);
  const assetIds = preview?.assetIds || [];
  useEffect(() => () => { if (timer.current !== null) window.clearTimeout(timer.current); }, []);
  useEffect(() => { setPageIndex(0); setPreviewOpen(false); }, [assetIds.join("|")]);
  useEffect(() => { if (preview?.pageIndex != null) setPageIndex(preview.pageIndex); }, [preview?.pageIndex]);
  function enter() { preview?.onEnter(); if (!preview?.enabled || !assetIds.length || window.matchMedia("(pointer: coarse)").matches || !cardRef.current) return; timer.current = window.setTimeout(() => { if (cardRef.current) { setPreviewOpen(true); preview.onOpen({ workId: id, anchor: cardRef.current.getBoundingClientRect(), assetIds, pageIndex }); } }, preview.delayMs); }
  function leave() { if (timer.current !== null) window.clearTimeout(timer.current); timer.current = null; wheelDelta.current = 0; setPreviewOpen(false); preview?.onLeave(); }
  useEffect(() => {
    const card = cardRef.current;
    if (!card || !previewOpen || !preview?.enabled || assetIds.length <= 1 || window.matchMedia("(pointer: coarse)").matches) return;
    const threshold = preview.wheelThreshold ?? 80;
    const onWheel = (event: WheelEvent) => {
      if (event.ctrlKey) return;
      event.preventDefault();
      wheelDelta.current += event.deltaY;
      if (Math.abs(wheelDelta.current) < threshold) return;
      const next = (pageIndex + (wheelDelta.current > 0 ? 1 : -1) + assetIds.length) % assetIds.length;
      wheelDelta.current = 0;
      setPageIndex(next);
      preview.onPage(next);
    };
    card.addEventListener("wheel", onWheel, { passive: false });
    return () => card.removeEventListener("wheel", onWheel);
  }, [assetIds.join("|"), pageIndex, preview?.enabled, preview?.wheelThreshold, previewOpen]);
  const {copy}=useGalleryCopy();
  function toggleFavorite() { if (!favorite || favorite.disabled || favorite.busy) return; favorite.onToggle(); }
  return <article ref={cardRef} onMouseEnter={enter} onMouseLeave={leave} className={'work-item ratio-'+ratio+(selected?' is-selected':'')} data-work-id={id}><div className="work-picture">
    <a href={href} onClick={event=>{leave();onOpen(event);}} aria-label={copy('viewWork',{title})}>{media}</a>
    {onSelect?<label className="work-select"><input type="checkbox" aria-label={copy('selectWork',{title})} checked={selected} onChange={onSelect}/><span className="sr-only">{copy('selectWork',{title})}</span></label>:null}
    {assets>1?<span className="asset-count">{copy('assets',{count:assets})}</span>:null}
  </div><div className="work-caption"><div className="work-title-line"><h2><a href={href} onClick={event=>{leave();onOpen(event);}}>{title}</a></h2>{favorite?<button disabled={favorite.disabled} className={'icon-button favorite-button '+(favorite.active?'is-favorite':'')} aria-label={copy(favorite.active?'unfavorite':'favorite',{title})} aria-pressed={favorite.active} aria-busy={favorite.busy||undefined} aria-disabled={favorite.busy||undefined} onClick={toggleFavorite}><Icon name="heart"/></button>:null}</div>
    <p className="creator-name">{creator}</p><div className="work-meta"><span>{source}</span><time dateTime={dateTime}>{dateLabel}</time>{adult?<span>{copy('adult')}</span>:null}{ai?<span>{copy('ai')}</span>:null}</div>{favorite?.error?<p className="layout-favorite-error" role="alert">{favorite.error}</p>:null}{trashActions}
  </div></article>;
}

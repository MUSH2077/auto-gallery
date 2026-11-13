'use client';
import { type MouseEventHandler, type ReactNode } from 'react';
import { Icon } from './Icon';
import { useGalleryCopy } from './copy';
export interface WorkItemProps { id: string; title: string; creator: string; source: string; dateTime: string; dateLabel: string; ratio: string; assets: number; adult: boolean; ai: boolean; href: string; onOpen: MouseEventHandler<HTMLAnchorElement>; media: ReactNode; selected: boolean; onSelect?: () => void; favorite?: {active: boolean; busy: boolean; disabled: boolean; error?: string; onToggle: () => void}; trashActions?: ReactNode; }
export function WorkItem({id,title,creator,source,dateTime,dateLabel,ratio,assets,adult,ai,href,onOpen,media,selected,onSelect,favorite,trashActions}: WorkItemProps) {
  const {copy}=useGalleryCopy();
  function toggleFavorite() { if (!favorite || favorite.disabled) return; favorite.onToggle(); }
  return <article className={'work-item ratio-'+ratio+(selected?' is-selected':'')} data-work-id={id}><div className="work-picture">
    <a href={href} onClick={onOpen} aria-label={copy('viewWork',{title})}>{media}</a>
    {onSelect?<label className="work-select"><input type="checkbox" aria-label={copy('selectWork',{title})} checked={selected} onChange={onSelect}/><span className="sr-only">{copy('selectWork',{title})}</span></label>:null}
    {assets>1?<span className="asset-count">{copy('assets',{count:assets})}</span>:null}
  </div><div className="work-caption"><div className="work-title-line"><h2><a href={href} onClick={onOpen}>{title}</a></h2>{favorite?<button disabled={favorite.disabled} className={'icon-button favorite-button '+(favorite.active?'is-favorite':'')} aria-label={copy(favorite.active?'unfavorite':'favorite',{title})} aria-pressed={favorite.active} aria-busy={favorite.busy||undefined} onClick={toggleFavorite}><Icon name="heart"/></button>:null}</div>
    <p className="creator-name">{creator}</p><div className="work-meta"><span>{source}</span><time dateTime={dateTime}>{dateLabel}</time>{adult?<span>{copy('adult')}</span>:null}{ai?<span>{copy('ai')}</span>:null}</div>{favorite?.error?<p className="layout-favorite-error" role="alert">{favorite.error}</p>:null}{trashActions}
  </div></article>;
}

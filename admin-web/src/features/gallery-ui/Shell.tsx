'use client';
import type { MouseEventHandler, ReactNode } from 'react';
import { Icon } from './Icon';
import { useGalleryCopy } from './copy';

export interface GalleryLink { href: string; onClick: MouseEventHandler<HTMLAnchorElement>; }
export function BrandWordmark({href, className=''}: {href: string; className?: string}) {
  return <a className={'layout-brand-wordmark layout-brand-link'+(className?' '+className:'')} translate="no" aria-label="Auto-Gallery" href={href} onClick={event=>{
    if(event.button===0&&!event.metaKey&&!event.ctrlKey&&!event.shiftKey&&!event.altKey)event.preventDefault();
  }}>Auto-Gallery</a>;
}
export function BrandNavigation({href}: {href: string}) {
  return <header className="layout-product-nav"><BrandWordmark href={href}/></header>;
}
export function BottomNavigation({works, jobs, active, children}: { works?: GalleryLink; jobs?: GalleryLink; active: GalleryNavigationSection; children?: ReactNode; }) {
  const {copy}=useGalleryCopy();
  if (!works && !jobs && !children) return null;
  return <nav className="layout-bottom-nav" aria-label={copy('mainNavigation')}>
    {works?<a href={works.href} onClick={works.onClick} aria-current={active==='works'?'page':undefined}><Icon name="image"/><span>{copy('works')}</span></a>:null}
    {jobs?<a href={jobs.href} onClick={jobs.onClick} aria-current={active==='jobs'?'page':undefined}><Icon name="jobs"/><span>{copy('jobs')}</span></a>:null}
    {children}
  </nav>;
}
export type GalleryNavigationSection = 'works'|'jobs'|null;
export interface GalleryNavigationRoutes { works: string; jobs: string; }
function belongsToRouteFamily(pathname: string, href: string): boolean {
  const base = href.endsWith('/') ? href.slice(0, -1) : href;
  return pathname === base || pathname.startsWith(base + '/');
}
export function galleryNavigationSection(pathname: string, routes: GalleryNavigationRoutes): GalleryNavigationSection {
  if (belongsToRouteFamily(pathname, routes.jobs)) return 'jobs';
  if (belongsToRouteFamily(pathname, routes.works)) return 'works';
  return null;
}

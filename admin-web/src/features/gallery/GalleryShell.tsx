'use client';
import {useEffect,useRef,useState,type ReactNode,type MouseEvent} from 'react';
import dynamic from 'next/dynamic';
import Link from 'next/link';
import {usePathname,useRouter,useSearchParams} from 'next/navigation';
import {BrandNavigation,BottomNavigation,galleryNavigationSection} from '@/features/gallery-ui/Shell';
import {usePermissions} from '@/lib/usePermissions';
import {useAuth} from '@/lib/auth';
import {useI18n,useT} from '@/lib/i18n';
import {galleryCopy} from '@/features/gallery-ui/copy';
import {ThemeToggle,LangToggle} from '@/lib/theme';
import {NotificationBell} from '@/components/NotificationCenter';
import {ADMIN_NAV_LINKS,ADMIN_LINK_MODULE,findAdminNavLink,hasAdminPermission} from '@/lib/adminNavigation';
import {adminRoutes} from '@/lib/adminRoutes';
import GalleryRoot from './GalleryRoot';
import {defaultGalleryRoutes} from './routes';
const CommandPalette=dynamic(()=>import('@/components/CommandPalette'),{loading:()=>null});
export default function GalleryShell({children}:{children:ReactNode}) {
  const pathname=usePathname(),sp=useSearchParams(),router=useRouter(),permissions=usePermissions(),{user,logout}=useAuth(),t=useT(),{lang}=useI18n();
  const main=useRef<HTMLElement>(null),menu=useRef<HTMLDetailsElement>(null),previousPath=useRef(pathname);
  const activeAccountHref=pathname===adminRoutes.profile?adminRoutes.profile:findAdminNavLink(pathname)?.href;
  const [commandRequested,setCommandRequested]=useState(false),[commandOpen,setCommandOpen]=useState(false);
  const commandReturnFocus=useRef<HTMLElement|null>(null);
  function openCommand() {commandReturnFocus.current=document.activeElement as HTMLElement|null;setCommandRequested(true);setCommandOpen(true);}
  const canWorks=permissions.has('library'),canJobs=permissions.has('tasks')||permissions.has('system');
  const {works:worksHref,jobs:jobsHref}=defaultGalleryRoutes;
  const selfHref=pathname+(sp.toString()?'?'+sp.toString():'');
  function navigate(event:MouseEvent<HTMLAnchorElement>,href:string) {
    if(event.button||event.metaKey||event.ctrlKey||event.shiftKey||event.altKey)return;
    event.preventDefault();router.push(href);
  }
  useEffect(()=>{
    if(previousPath.current===pathname)return;previousPath.current=pathname;
    if(menu.current)menu.current.open=false;
    window.scrollTo({top:0,behavior:'instant'});
    const frame=requestAnimationFrame(()=>main.current?.focus({preventScroll:true}));
    return()=>cancelAnimationFrame(frame);
  },[pathname]);
  useEffect(()=>{
    if(!canWorks)return;
    const key=(event:KeyboardEvent)=>{if(event.isComposing||!(event.ctrlKey||event.metaKey)||event.key.toLowerCase()!=='k')return;event.preventDefault();if(commandOpen)setCommandOpen(false);else openCommand();};
    document.addEventListener('keydown',key);return()=>document.removeEventListener('keydown',key);
  },[canWorks,commandOpen]);
  const otherLinks=ADMIN_NAV_LINKS.filter(link=>link.href!==adminRoutes.works&&link.href!==adminRoutes.jobs&&(!link.adminOnly||permissions.isAdmin)&&hasAdminPermission(ADMIN_LINK_MODULE[link.href],permissions.has));
  return <GalleryRoot>
    <details ref={menu} className="gallery-account" onKeyDown={event=>{if(event.key==='Escape'){event.preventDefault();event.currentTarget.open=false;event.currentTarget.querySelector('summary')?.focus();}}}>
      <summary>{galleryCopy(lang,"account")}<span aria-hidden="true">⌄</span></summary>
      <div className="gallery-account-menu">
        <p>{user?.display_name||user?.username}</p>
        <div className="gallery-account-utilities">{canWorks?<button type="button" className="text-button" onClick={openCommand}>{galleryCopy(lang,"search")}</button>:null}{canJobs?<NotificationBell/>:null}<ThemeToggle/><LangToggle/></div>
        {otherLinks.map(link=><Link key={link.href} href={link.href} aria-current={activeAccountHref===link.href?'page':undefined}>{t(link.labelKey)}</Link>)}
        <Link href={adminRoutes.profile} aria-current={activeAccountHref===adminRoutes.profile?'page':undefined}>{t('auth.change_password')}</Link><button type="button" className="text-button" onClick={()=>void logout()}>{t('auth.logout')}</button>
      </div>
    </details>
    <div className="app-shell"><BrandNavigation href={selfHref}/>
      <main ref={main} className="app-main" id="main-content" tabIndex={-1} onFocusCapture={event=>{
        if(!event.target.matches(':focus-visible'))return;const dock=document.querySelector<HTMLElement>('.layout-bottom-nav');if(!dock)return;
        const box=event.target.getBoundingClientRect(),nav=dock.getBoundingClientRect();
        if(box.right>nav.left&&box.left<nav.right&&box.bottom>nav.top&&box.top<nav.bottom)window.scrollBy({top:box.bottom-nav.top+16,behavior:'instant'});
      }}>{children}</main>
      <BottomNavigation active={galleryNavigationSection(pathname,defaultGalleryRoutes)} works={canWorks?{href:worksHref,onClick:event=>navigate(event,worksHref)}:undefined} jobs={canJobs?{href:jobsHref,onClick:event=>navigate(event,jobsHref)}:undefined}/>
    </div>
    {commandRequested?<CommandPalette open={commandOpen} onClose={()=>setCommandOpen(false)} returnFocusRef={commandReturnFocus}/>:null}
  </GalleryRoot>;
}

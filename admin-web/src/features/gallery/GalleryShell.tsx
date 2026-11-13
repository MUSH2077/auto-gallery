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
import {adminRoutes} from '@/lib/adminRoutes';
import GalleryRoot from './GalleryRoot';
import {defaultGalleryRoutes} from './routes';
const CommandPalette=dynamic(()=>import('@/components/CommandPalette'),{loading:()=>null});
const AccountPopover=dynamic(()=>import('./AccountPopover'),{loading:()=>null});
const MoreNavigation=dynamic(()=>import('./MoreNavigation'),{loading:()=>null});
export default function GalleryShell({children}:{children:ReactNode}) {
  const pathname=usePathname(),sp=useSearchParams(),router=useRouter(),permissions=usePermissions(),{user,logout}=useAuth(),t=useT(),{lang}=useI18n();
  const main=useRef<HTMLElement>(null),accountTrigger=useRef<HTMLButtonElement>(null),moreTrigger=useRef<HTMLButtonElement>(null),previousPath=useRef(pathname);
  const [accountOpen,setAccountOpen]=useState(false),[moreOpen,setMoreOpen]=useState(false);
  const [commandRequested,setCommandRequested]=useState(false),[commandOpen,setCommandOpen]=useState(false);
  const commandReturnFocus=useRef<HTMLElement|null>(null);
  function openCommand(returnFocus?:HTMLElement|null) {const focused=document.activeElement as HTMLElement|null;commandReturnFocus.current=returnFocus||(focused?.closest(".gallery-more-menu")?moreTrigger.current:focused);setMoreOpen(false);setCommandRequested(true);setCommandOpen(true);}
  const canWorks=permissions.has('library'),canJobs=permissions.has('tasks')||permissions.has('system');
  const {works:worksHref,jobs:jobsHref}=defaultGalleryRoutes;
  const selfHref=pathname+(sp.toString()?'?'+sp.toString():'');
  function navigate(event:MouseEvent<HTMLAnchorElement>,href:string) {
    if(event.button||event.metaKey||event.ctrlKey||event.shiftKey||event.altKey)return;
    event.preventDefault();router.push(href);
  }
  useEffect(()=>{
    if(previousPath.current===pathname)return;previousPath.current=pathname;
    setAccountOpen(false);setMoreOpen(false);
    window.scrollTo({top:0,behavior:'instant'});
    const frame=requestAnimationFrame(()=>main.current?.focus({preventScroll:true}));
    return()=>cancelAnimationFrame(frame);
  },[pathname]);
  useEffect(()=>{
    if(!canWorks)return;
    const key=(event:KeyboardEvent)=>{if(event.isComposing||!(event.ctrlKey||event.metaKey)||event.key.toLowerCase()!=='k')return;event.preventDefault();if(commandOpen)setCommandOpen(false);else openCommand();};
    document.addEventListener('keydown',key);return()=>document.removeEventListener('keydown',key);
  },[canWorks,commandOpen]);
  return <GalleryRoot>
    <AccountPopover open={accountOpen} onOpenChange={setAccountOpen} triggerRef={accountTrigger} label={galleryCopy(lang,"account")}>
        <div className="gallery-account-header"><span>{user?.display_name||user?.username}</span></div>
        <div className="gallery-account-utilities"><ThemeToggle/><LangToggle/></div>
        <Link href={adminRoutes.profile} aria-current={pathname===adminRoutes.profile?'page':undefined} onClick={()=>setAccountOpen(false)}>{t('auth.change_password')}</Link>
        <button type="button" className="text-button" onClick={()=>void logout()}>{t('auth.logout')}</button>
    </AccountPopover>
    <div className="app-shell"><BrandNavigation href={selfHref}/>
      <main ref={main} className="app-main" id="main-content" tabIndex={-1} onFocusCapture={event=>{
        if(!event.target.matches(':focus-visible'))return;const dock=document.querySelector<HTMLElement>('.layout-bottom-nav');if(!dock)return;
        const box=event.target.getBoundingClientRect(),nav=dock.getBoundingClientRect();
        if(box.right>nav.left&&box.left<nav.right&&box.bottom>nav.top&&box.top<nav.bottom)window.scrollBy({top:box.bottom-nav.top+16,behavior:'instant'});
      }}>{children}</main>
      <BottomNavigation active={galleryNavigationSection(pathname,defaultGalleryRoutes)} works={canWorks?{href:worksHref,onClick:event=>navigate(event,worksHref)}:undefined} jobs={canJobs?{href:jobsHref,onClick:event=>navigate(event,jobsHref)}:undefined}><MoreNavigation open={moreOpen} onOpenChange={setMoreOpen} triggerRef={moreTrigger} pathname={pathname} has={permissions.has} isAdmin={permissions.isAdmin} onSearch={()=>{setMoreOpen(false);openCommand(moreTrigger.current);}}/></BottomNavigation>
    </div>
    {commandRequested?<CommandPalette open={commandOpen} onClose={()=>setCommandOpen(false)} returnFocusRef={commandReturnFocus}/>:null}
  </GalleryRoot>;
}

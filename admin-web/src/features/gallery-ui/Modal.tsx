'use client';
import {useEffect,useId,useRef,type ReactNode} from 'react';
import {Icon} from './Icon';
import {useGalleryCopy} from './copy';

export function Modal({title,children,onClose,sheet=false,closeLabel}: {
  title:string;children:ReactNode;onClose:()=>void;sheet?:boolean;closeLabel?:string;
}) {
  const {copy}=useGalleryCopy();
  const ref=useRef<HTMLDialogElement>(null),titleId=useId();
  const closeRef=useRef(onClose);closeRef.current=onClose;
  useEffect(()=>{
    const dialog=ref.current,previous=document.activeElement as HTMLElement|null;
    dialog?.showModal();
    return ()=>{
      dialog?.close();
      if(previous?.isConnected&&!previous.closest('[inert]'))previous.focus();
      else document.getElementById('main-content')?.focus();
    };
  },[]);
  return <dialog ref={ref} className={'dialog '+(sheet?'detail-sheet':'confirm-dialog')} aria-labelledby={titleId}
    onKeyDown={event=>{
      if(event.key!=="Tab"||event.ctrlKey||event.metaKey||event.altKey)return;
      const controls=Array.from(event.currentTarget.querySelectorAll<HTMLElement>("button:not([disabled]),a[href],input:not([disabled]):not([type=hidden]),select:not([disabled]),textarea:not([disabled]),summary,[tabindex]:not([tabindex=\"-1\"])"))
        .filter(element=>element.tabIndex>=0&&element.getClientRects().length>0&&!element.closest("[inert]"));
      const first=controls[0],last=controls[controls.length-1];
      if(!first)return;
      if(event.shiftKey&&(document.activeElement===first||document.activeElement===event.currentTarget)){event.preventDefault();last.focus();}
      else if(!event.shiftKey&&document.activeElement===last){event.preventDefault();first.focus();}
    }}
    onCancel={event=>{event.preventDefault();closeRef.current();}}>
    <div className="dialog-top"><h2 id={titleId}>{title}</h2><button type="button" className="icon-button" onClick={onClose} aria-label={closeLabel||copy('close',{title})}><Icon name="close"/></button></div>
    {children}
  </dialog>;
}

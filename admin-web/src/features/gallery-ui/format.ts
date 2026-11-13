'use client';
import {useGalleryCopy} from './copy';
export function useGalleryFormat() {
  const {lang}=useGalleryCopy();
  const locale=lang==='zh'?'zh-CN':'en-US';
  return {
    date:(value?:string|null)=>value&&Number.isFinite(Date.parse(value))?new Intl.DateTimeFormat(locale,{month:'short',day:'numeric',timeZone:'Asia/Shanghai'}).format(new Date(value)):'—',
    number:(value:number)=>new Intl.NumberFormat(locale).format(value),
  };
}

'use client';

import { useEffect, useRef, useState } from 'react';
import * as Select from '@radix-ui/react-select';
import * as Popover from '@radix-ui/react-popover';
import type { WorksViewMode } from '../works/view';
import { Icon } from './Icon';
import { WORK_VIEW_ORDER } from './work-view';
import { useGalleryCopy } from './copy';

export type Choice = { value: string; label: string };
const allValue = '__all__';
const moreValue = '__more__';
function Chevron() { return <svg className="layout-tool-chevron" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="m6 9 6 6 6-6"/></svg>; }

export function ToolbarSelect({label,value,options,onValueChange,onMore,moreLabel,id,disabled,describedBy}:{describedBy?:string;id?:string;disabled?:boolean;label:string;value:string;options:Choice[];onValueChange:(value:string)=>void;onMore?:()=>void;moreLabel?:string}) {
  return <Select.Root disabled={disabled} value={value || allValue} onValueChange={next=>{if(next===moreValue){onMore?.();return;}onValueChange(next===allValue?'':next);}}>
    <Select.Trigger id={id} className="layout-tool-trigger layout-select-trigger" aria-label={label} aria-describedby={describedBy} data-filtered={value!==options[0]?.value || undefined}>
      <Select.Value/><Select.Icon asChild><Chevron/></Select.Icon>
    </Select.Trigger>
    <Select.Content className="layout-tool-menu layout-select-menu" position="popper" sideOffset={8} collisionPadding={16}><Select.Viewport>
      {options.map(option=><Select.Item className="layout-select-item" key={option.value} value={option.value || allValue}><Select.ItemText>{option.label}</Select.ItemText><Select.ItemIndicator className="layout-choice-indicator"><Icon name="check"/></Select.ItemIndicator></Select.Item>)}
      {onMore ? <><Select.Separator className="layout-menu-separator"/><Select.Item className="layout-select-item" value={moreValue}><Select.ItemText>{moreLabel}</Select.ItemText></Select.Item></> : null}
    </Select.Viewport></Select.Content>
  </Select.Root>;
}

export function DisplayOptions({view, onViewChange, blurNsfw, onBlurChange, onAdvanced, advancedLabel}: { view: WorksViewMode; onViewChange: (view: WorksViewMode) => void; blurNsfw: boolean; onBlurChange: (blur: boolean) => void; onAdvanced?: () => void; advancedLabel?: string; }) {
  const {copy}=useGalleryCopy();
  const [open,setOpen]=useState(false); const content=useRef<HTMLDivElement>(null);
  return <Popover.Root open={open} onOpenChange={setOpen}><Popover.Trigger asChild>
    <button type="button" className="layout-tool-trigger layout-display-trigger" aria-label={copy('display')}><Icon name={view}/><span className="layout-tool-text">{copy('display')}</span><Chevron/></button>
  </Popover.Trigger><Popover.Content ref={content} className="layout-tool-menu layout-display-menu" aria-label={copy('displaySettings')} align="end" sideOffset={8} collisionPadding={16} onOpenAutoFocus={event=>{event.preventDefault();content.current?.querySelector<HTMLInputElement>('input[type=radio]:checked')?.focus();}}>
    <fieldset><legend>{copy('viewMode')}</legend>{WORK_VIEW_ORDER.map(mode=><label className="layout-view-choice" key={mode}><input type="radio" name="layout-work-view" value={mode} checked={view===mode} onClick={event=>{if(event.detail>0)setOpen(false);}} onChange={()=>onViewChange(mode)} onKeyDown={event=>{if(event.key==='Enter'){event.preventDefault();setOpen(false);}}}/><span>{copy(mode==='masonry'?'masonry':mode==='grid'?'grid':'list')}</span><Icon name="check"/></label>)}</fieldset>
    <label className="layout-blur-choice"><input type="checkbox" checked={blurNsfw} onChange={event=>onBlurChange(event.target.checked)}/>{copy('blurAdult')}</label>
    {onAdvanced ? <button type="button" className="text-button layout-display-more" onClick={() => { setOpen(false); onAdvanced(); }}>{advancedLabel}</button> : null}
  </Popover.Content></Popover.Root>;
}

export function SearchField({value, onChange, placeholder, onComposingChange}: { value: string; onChange: (value: string) => void; placeholder: string; onComposingChange?:(composing:boolean)=>void; }) {
  const {copy}=useGalleryCopy(); const input=useRef<HTMLInputElement>(null); const composing=useRef(false); const [draft,setDraft]=useState(value);
  useEffect(()=>{if(!composing.current)setDraft(value);},[value]);
  return <div className="search-field"><Icon name="search"/><input ref={input} type="search" name="query" aria-label={copy('searchLabel')} placeholder={placeholder} autoComplete="off" value={draft}
    onCompositionStart={()=>{composing.current=true;onComposingChange?.(true);}}
    onCompositionEnd={event=>{composing.current=false;onComposingChange?.(false);onChange(event.currentTarget.value);}}
    onChange={event=>{setDraft(event.target.value);if(!composing.current)onChange(event.target.value);}}/>{draft?<button type="button" className="icon-button" onClick={()=>{composing.current=false;onComposingChange?.(false);setDraft('');onChange('');input.current?.focus();}} aria-label={copy('clearSearch')}><Icon name="close"/></button>:null}</div>;
}

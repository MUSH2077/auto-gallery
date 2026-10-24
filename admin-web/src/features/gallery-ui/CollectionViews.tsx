'use client';
import type { MouseEventHandler, ReactNode } from 'react';
import type { WorksViewMode } from '../works/view';
import { useGalleryCopy } from './copy';

export interface ScopeLink { value: string; label: string; href: string; active: boolean; count?: string; countSuffix?: string; onClick: MouseEventHandler<HTMLAnchorElement>; }
function ScopeTabs({items, jobs=false}: {items: ScopeLink[]; jobs?: boolean}) {
  const {copy}=useGalleryCopy();
  return <div className="layout-scope-search"><nav className={'layout-scope-tabs'+(jobs?' layout-task-tabs':'')} aria-label={copy(jobs?'taskScope':'workScope')}>{items.map(item=><a key={item.value} href={item.href} onClick={item.onClick} aria-current={item.active?'page':undefined}>{item.label}{item.count!==undefined?<span className={jobs?undefined:'layout-scope-count'} aria-live={item.countSuffix?'polite':undefined}>{item.count}{item.countSuffix?<span className="sr-only">{item.countSuffix}</span>:null}</span>:null}</a>)}</nav></div>;
}
export function WorksView({heading, scopes, toolbar, selection, children, pagination}: { heading: string; scopes: ScopeLink[]; toolbar: ReactNode; selection?: ReactNode; children: ReactNode; pagination?: ReactNode; }) {
  const {copy}=useGalleryCopy();
  return <section className="layout-works" aria-labelledby="layout-page-title"><h1 className="sr-only" id="layout-page-title">{heading}</h1><div className="layout-workspace-tools"><ScopeTabs items={scopes}/><div className="layout-content-toolbar" role="group" aria-label={copy('workTools')}>{toolbar}</div></div>{selection}{children}{pagination}</section>;
}
export function WorkCollection({mode, children}: {mode: WorksViewMode; children: ReactNode}) { return <div className={'work-collection work-'+mode} data-works-layout={mode}>{children}</div>; }
export function JobsView({scopes, toolbar, selection, failures, children}: { scopes: ScopeLink[]; toolbar: ReactNode; selection?: ReactNode; failures?: ReactNode; children: ReactNode; }) {
  const {copy}=useGalleryCopy();
  return <section className="layout-jobs" aria-labelledby="layout-page-title"><h1 className="sr-only" id="layout-page-title">{copy('jobsHeading')}</h1><div className="layout-workspace-tools"><ScopeTabs items={scopes} jobs/><div className="layout-content-toolbar" role="group" aria-label={copy('taskTools')}>{toolbar}</div></div>{selection}{failures}{children}</section>;
}
export interface TaskRow { id: string; title: string; href: string; onOpen: MouseEventHandler<HTMLAnchorElement>; kind: string; creator: string; dateTime: string; dateLabel: string; reason?: string; danger: boolean; selected: boolean; onSelect: () => void; status: ReactNode; statusLabel: string; progress?: {value: number; resources: string}; emptyProgress: string; actions: ReactNode; }
export function TaskTable({rows, selecting, selectionDisabled=false}: {rows: TaskRow[]; selecting: boolean; selectionDisabled?:boolean}) {
  const {copy}=useGalleryCopy();
  return <table className={'layout-tasks'+(selecting?' is-selecting':'')}><caption className="sr-only">{copy('taskCaption')}</caption><colgroup>{selecting?<col className="layout-task-check-col"/>:null}<col/><col className="layout-task-status-col"/><col className="layout-task-progress-col"/><col className="layout-task-actions-col"/></colgroup><thead><tr>{selecting?<th scope="col"><span className="sr-only">{copy('choose')}</span></th>:null}<th scope="col">{copy('taskAndSource')}</th><th scope="col">{copy('status')}</th><th scope="col">{copy('progress')}</th><th scope="col">{copy('actions')}</th></tr></thead><tbody>{rows.map(row=><tr key={row.id} data-task-id={row.id} className={row.selected?'is-selected':''}>
      {selecting?<td className="layout-task-check"><label><input type="checkbox" aria-label={copy('selectTask',{id:row.id})} checked={row.selected} disabled={selectionDisabled} onChange={()=>{if(!selectionDisabled)row.onSelect();}}/><span className="sr-only">{copy('selectTask',{id:row.title})}</span></label></td>:null}
      <td className="layout-task-identity"><a className="task-title" href={row.href} onClick={row.onOpen}>{row.title}</a><p className="task-meta"><span>{row.kind}</span><span>{row.creator}</span><time dateTime={row.dateTime}>{row.dateLabel}</time></p>{row.reason?<p className={'layout-task-reason'+(row.danger?' danger-text':'')}>{row.reason}</p>:null}</td>
      <td className="layout-task-status" aria-label={copy('status')+'：'+row.statusLabel}>{row.status}</td><td className={'layout-task-progress'+(!row.progress?' is-empty':'')}>{row.progress?<div className="task-progress"><span><span className="layout-mobile-label">{copy('progress')} </span>{row.progress.resources}</span><progress max={100} value={row.progress.value} aria-label={copy('taskProgress',{title:row.title})}/></div>:<span className="muted">{row.emptyProgress}</span>}</td><td className="layout-task-actions">{row.actions}</td>
    </tr>)}</tbody></table>;
}

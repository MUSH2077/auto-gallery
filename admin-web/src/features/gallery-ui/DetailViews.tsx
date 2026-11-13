'use client';
import type {ReactNode} from 'react';
import {useGalleryCopy} from './copy';
export interface DetailFact {label:string;value:ReactNode}
function Facts({items}:{items:DetailFact[]}) { return items.length?<dl>{items.map((item,index)=><div key={index}><dt>{item.label}</dt><dd>{item.value}</dd></div>)}</dl>:null; }
export function WorkDetailView({media,creator,facts,actions,feedback,children}: {media:ReactNode;creator:string;facts:DetailFact[];actions?:ReactNode;feedback?:ReactNode;children?:ReactNode;}) { return <div className="work-detail"><div className="detail-image">{media}</div><aside className="detail-info"><p className="detail-creator">{creator}</p><Facts items={facts}/>{actions?<div className="detail-actions">{actions}</div>:null}{feedback}{children}</aside></div>; }
export function TaskDetailView({id,title,status,facts,reason,actions,children,beforeActions}: {id:string;title:string;status:ReactNode;facts:DetailFact[];reason?:ReactNode;actions?:ReactNode;children?:ReactNode;beforeActions?:ReactNode;}) {
  const {copy}=useGalleryCopy();
  return <div className="task-detail"><span className="detail-id" translate="no">{id}</span><h3>{title}</h3>{status}<Facts items={facts}/>{reason?<section className="detail-reason"><h4>{copy('situation')}</h4><p>{reason}</p></section>:null}{beforeActions}{actions?<div className="detail-task-actions">{actions}</div>:null}{children}</div>;
}

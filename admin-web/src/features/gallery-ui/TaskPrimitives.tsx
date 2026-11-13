'use client';
export function TaskStatus({value,label}:{value:string;label:string}) {
  return <span className={'task-status status-'+value}><i aria-hidden="true"/><span>{label}</span></span>;
}

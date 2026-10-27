"use client";
import Link from "next/link";
import {useEffect,useRef,useState} from "react";
import {useIsMutating,useMutation,useQueryClient} from "@tanstack/react-query";
import {JobDetailDrawer} from "@/components/JobDrawers";
import {Modal} from "@/features/gallery-ui/Modal";
import {useTaskCommands} from "@/lib/useTaskCommand";
import {usePermissions} from "@/lib/usePermissions";
import {useT} from "@/lib/i18n";
import {actionErrorReason,repeatSyncConflict,readRepeatSyncIntent,clearRepeatSyncIntent,type TaskAction} from "@/lib/task-actions";

type JobKind="download"|"import";
type Confirmation="repeat_sync"|"delete";
export default function LegacyJobDrawer({kind,id,onClose,onRepeatAccepted}:{kind:JobKind;id:string;onClose:()=>void;onRepeatAccepted?:(id:string)=>void}){
 const t=useT(),{user}=usePermissions(),commands=useTaskCommands(user?.id),client=useQueryClient();
 const mutationKey=["jobs","record-command",kind,id],busy=useIsMutating({mutationKey})>0;
 const active=useRef(true),pending=useRef(false),sequence=useRef(0);
 const [confirmation,setConfirmation]=useState<Confirmation|null>(null);
 useEffect(()=>{active.current=true;return()=>{active.current=false;sequence.current++;};},[]);
 function closeConfirmation(){sequence.current++;setConfirmation(null);}
 const action=useMutation({mutationKey,
  mutationFn:({name}:{name:TaskAction;token:number})=>name==="repeat_sync"?commands.repeat({id},"download"):commands.execute({id},name,kind),
  onSuccess:(result,variables)=>{
   if(!active.current||sequence.current!==variables.token)return;
   closeConfirmation();
   if(variables.name==="delete")onClose();
   if(variables.name==="repeat_sync"&&result&&typeof result==="object"&&"job_id" in result&&typeof result.job_id==="string")onRepeatAccepted?.(result.job_id);
  },
  onSettled:()=>{pending.current=false;commands.invalidate();}
 });
 function run(name:TaskAction){if(pending.current||client.isMutating({mutationKey}))return;pending.current=true;action.mutate({name,token:sequence.current});}
 function confirm(name:Confirmation){if(pending.current||busy)return;sequence.current++;action.reset();setConfirmation(name);}
 const conflict=confirmation==="repeat_sync"&&action.error?repeatSyncConflict(action.error):null;
 const confirmationTitle=confirmation==="repeat_sync"?t("jobs.repeat_sync_title"):t(kind==="download"?"jobs.delete_dl_title":"jobs.delete_im_title");
 return <>
  <Modal title={t(kind==="download"?"jobs.download_detail":"jobs.import_detail")} onClose={onClose} sheet>
   <JobDetailDrawer embedded kind={kind} id={id} onClose={onClose}
    onRetryDownload={()=>run("retry")} onPauseDownload={()=>run("pause")} onResumeDownload={()=>run("resume")} onRepeatDownload={()=>confirm("repeat_sync")} onDeleteDownload={()=>confirm("delete")}
    onRetryImport={()=>run("retry")} onDeleteImport={()=>confirm("delete")} actionPending={busy} actionError={action.error}/>
  </Modal>
  {confirmation&&<Modal title={confirmationTitle} onClose={closeConfirmation}>
   <p className="confirm-message">{confirmation==="repeat_sync"?t("jobs.repeat_sync_confirm"):t(kind==="download"?"jobs.delete_dl_msg":"jobs.delete_im_msg")}</p>
   {busy&&<p className="confirm-pending" role="status">{t("jobs.action_submitted_close")}</p>}
   {action.error&&<p className="confirm-error" role="alert">{actionErrorReason(action.error)}</p>}
   {conflict?.kind==="existing"&&<Link className="text-link" href={`/admin/jobs?tab=downloads&job=${encodeURIComponent(conflict.existingJobId)}`}>{t("jobs.open_existing_download")}</Link>}
   {conflict?.kind==="identity"&&<button type="button" className="button" onClick={()=>{const intent=user?.id?readRepeatSyncIntent(user.id,id):null;if(intent)clearRepeatSyncIntent(intent);action.reset();}}>{t("jobs.repeat_new_intent")}</button>}
   <div className="confirm-actions"><button type="button" className="button" onClick={closeConfirmation}>{t(busy?"common.close":"common.cancel")}</button><button type="button" className="button primary" aria-disabled={busy||undefined} aria-busy={busy||undefined} onClick={()=>run(confirmation)}>{t("common.confirm")}</button></div>
  </Modal>}
 </>;
}

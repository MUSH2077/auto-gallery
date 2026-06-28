from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import RequireAdmin, get_admin_key
from app.database import get_db
from app.services.task_engine import TaskEngine, TaskEngineError
from app.services.tasks import TaskService, task_payload

router = APIRouter(dependencies=[RequireAdmin])


@router.get("")
async def list_tasks(
    kind: str | None = None,
    status: str | None = None,
    operation_type: str | None = None,
    source: str | None = None,
    q: str | None = None,
    offset: int = 0,
    limit: int = 50,
    db: AsyncSession = Depends(get_db),
):
    svc = TaskService(db)
    total, tasks = await svc.list_tasks(
        kind=kind,
        status=status,
        operation_type=operation_type,
        source=source,
        q=q,
        offset=offset,
        limit=limit,
    )
    return {"total": total, "items": [task_payload(task) for task in tasks]}


@router.get("/{task_id}")
async def get_task(task_id: UUID, db: AsyncSession = Depends(get_db)):
    svc = TaskService(db)
    task = await svc.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    events = await svc.task_events(task_id)
    return task_payload(task, events)


async def _control_task(
    task_id: UUID,
    action: str,
    db: AsyncSession,
    operator: str,
    note: str | None = None,
):
    svc = TaskService(db)
    task = await svc.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if not task.subject_id or task.subject_type not in {"download_job", "import_job"}:
        raise HTTPException(status_code=400, detail="This task type does not support direct control yet")

    engine = TaskEngine(db)
    try:
        if task.subject_type == "download_job":
            if action == "retry":
                result = await engine.retry_download(task.subject_id, operator=operator)
            elif action == "pause":
                result = await engine.pause_download(task.subject_id, note=note, operator=operator)
            elif action == "resume":
                result = await engine.resume_download(task.subject_id, operator=operator)
            elif action == "cancel":
                result = await engine.cancel_download(task.subject_id, note=note, operator=operator)
            else:
                raise HTTPException(status_code=400, detail="Unknown action")
            await svc.update_task(task, status=result.get("status"))
        else:
            if action == "retry":
                result = await engine.retry_import(task.subject_id, operator=operator)
            elif action == "pause":
                result = await engine.pause_import(task.subject_id, note=note, operator=operator)
            elif action == "resume":
                result = await engine.resume_import(task.subject_id, operator=operator)
            elif action == "cancel":
                result = await engine.cancel_import(task.subject_id, note=note, operator=operator)
            else:
                raise HTTPException(status_code=400, detail="Unknown action")
            await svc.update_task(task, status=result.get("status"))
        await db.commit()
        return {"task_id": str(task.id), **result}
    except TaskEngineError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/{task_id}/retry")
async def retry_task(task_id: UUID, db: AsyncSession = Depends(get_db), operator: str = Depends(get_admin_key)):
    return await _control_task(task_id, "retry", db, operator)


@router.post("/{task_id}/pause")
async def pause_task(task_id: UUID, data: dict | None = None, db: AsyncSession = Depends(get_db), operator: str = Depends(get_admin_key)):
    return await _control_task(task_id, "pause", db, operator, note=(data or {}).get("note"))


@router.post("/{task_id}/resume")
async def resume_task(task_id: UUID, db: AsyncSession = Depends(get_db), operator: str = Depends(get_admin_key)):
    return await _control_task(task_id, "resume", db, operator)


@router.post("/{task_id}/cancel")
async def cancel_task(task_id: UUID, data: dict | None = None, db: AsyncSession = Depends(get_db), operator: str = Depends(get_admin_key)):
    return await _control_task(task_id, "cancel", db, operator, note=(data or {}).get("note"))

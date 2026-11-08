import asyncio

from fastapi import APIRouter, HTTPException

from ..services import activity, approvals, followups

router = APIRouter(tags=["activity"])


@router.get("/activity")
def list_tasks() -> dict:
    return {"tasks": activity.list_tasks()}


@router.get("/activity/{task_id}")
def task_detail(task_id: str) -> dict:
    detail = activity.task_detail(task_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="No such task")
    return detail


@router.get("/reminders")
def list_reminders() -> dict:
    return {"reminders": activity.list_reminders()}


@router.post("/reminders/{reminder_id}/cancel")
def cancel_reminder(reminder_id: int) -> dict:
    if not activity.cancel_reminder(reminder_id):
        raise HTTPException(status_code=404, detail="No such reminder")
    return {"ok": True}


@router.post("/reminders/{reminder_id}/resume")
def resume_reminder(reminder_id: int) -> dict:
    if not activity.resume_reminder(reminder_id):
        raise HTTPException(status_code=404, detail="No such reminder")
    return {"ok": True}


@router.get("/followups")
def list_follow_ups() -> dict:
    return {"followups": activity.list_follow_ups()}


@router.post("/followups/{follow_up_id}/stop")
def stop_follow_up(follow_up_id: str) -> dict:
    if not followups.stop(follow_up_id):
        raise HTTPException(status_code=404, detail="Nothing open with that id")
    return {"ok": True}


@router.get("/approvals")
def list_approvals() -> dict:
    return {"approvals": [
        {"id": a.id, "to": a.to, "subject": a.subject, "preview": a.preview}
        for a in approvals.get_approval_store().pending()
    ]}


@router.post("/approvals/{approval_id}/approve")
async def approve(approval_id: str) -> dict:
    try:
        done = await asyncio.to_thread(approvals.approve, approval_id)
    except approvals.NotReady as exc:  # nothing was claimed; it's still waiting
        raise HTTPException(status_code=503, detail=str(exc))
    if done is None:  # already sent, declined, or never existed: nothing was sent now
        raise HTTPException(status_code=409, detail="Already handled")
    approvals.report_to_poke(done)
    return {"status": done.status, "result": done.result}


@router.post("/approvals/{approval_id}/decline")
async def decline(approval_id: str) -> dict:  # async: report_to_poke needs the event loop
    done = approvals.decline(approval_id)
    if done is None:
        raise HTTPException(status_code=409, detail="Already handled")
    approvals.report_to_poke(done)
    return {"status": "declined"}

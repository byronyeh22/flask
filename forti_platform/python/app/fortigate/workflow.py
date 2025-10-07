# app/fortigate/workflow.py

from enum import Enum

class DraftStatus(str, Enum):
    Pending_Submit   = "Pending_Submit"
    Preparing_Deploy = "Preparing_Deploy"
    Verify_Failed    = "Verify_Failed"
    Awaiting_Approval= "Awaiting_Approval"
    Deploying        = "Deploying"
    Deploy_Succeeded = "Deploy_Succeeded"
    Deploy_Failed    = "Deploy_Failed"
    Rejected         = "Rejected"
    Canceled         = "Canceled"
    Partial_Failed   = "Partial_Failed"

class TaskStatus(str, Enum):
    pending  = "pending"
    queued   = "queued"
    running  = "running"
    success  = "success"
    failed   = "failed"
    canceled = "canceled"

# ---- 標準化工具（忽略大小寫與空白/底線）----
def _canon_label(s: str) -> str:
    return (s or "").strip().replace(" ", "_").lower()

_DRAFT_CANON = { _canon_label(x.value): x for x in DraftStatus }
_TASK_CANON  = { _canon_label(x.value): x for x in TaskStatus }

def normalize_draft_status(s: str) -> DraftStatus:
    key = _canon_label(s)
    if key in _DRAFT_CANON:
        return _DRAFT_CANON[key]
    raise ValueError(f"Unknown draft status: {s}")

def normalize_task_status(s: str) -> TaskStatus:
    key = _canon_label(s)
    if key in _TASK_CANON:
        return _TASK_CANON[key]
    raise ValueError(f"Unknown task status: {s}")

# 允許重新送出的草稿狀態（對齊 DB enum）
def is_submit_allowed_status(s: str) -> bool:
    try:
        st = normalize_draft_status(s)
        return st in (DraftStatus.Pending_Submit, DraftStatus.Rejected, DraftStatus.Verify_Failed)
    except Exception:
        return False

# ---- 終態（不可變更，除同態外）----
_TERMINAL_DRAFT = {
    DraftStatus.Deploy_Succeeded,
    DraftStatus.Deploy_Failed,
    DraftStatus.Partial_Failed,
    DraftStatus.Canceled,
}
_TERMINAL_TASK = {
    TaskStatus.success, TaskStatus.failed, TaskStatus.canceled
}

def can_transition_draft(cur: str, nxt: str) -> bool:
    cur_e = normalize_draft_status(cur)
    nxt_e = normalize_draft_status(nxt)

    if cur_e in _TERMINAL_DRAFT and nxt_e != cur_e:
        return False

    if cur_e in (DraftStatus.Pending_Submit, DraftStatus.Rejected, DraftStatus.Verify_Failed) and \
       nxt_e == DraftStatus.Preparing_Deploy:
        return True

    if cur_e == DraftStatus.Preparing_Deploy and nxt_e in (DraftStatus.Awaiting_Approval, DraftStatus.Verify_Failed):
        return True

    if cur_e == DraftStatus.Awaiting_Approval and nxt_e == DraftStatus.Deploying:
        return True
    if cur_e == DraftStatus.Deploying and nxt_e in (
        DraftStatus.Canceled, DraftStatus.Partial_Failed, DraftStatus.Deploy_Succeeded, DraftStatus.Deploy_Failed
        ):
        return True
    if cur_e in (DraftStatus.Awaiting_Approval, DraftStatus.Preparing_Deploy) and nxt_e in (
        DraftStatus.Canceled, DraftStatus.Partial_Failed, DraftStatus.Deploy_Succeeded, DraftStatus.Deploy_Failed
    ):
        return True

    return cur_e == nxt_e

def can_transition_task(cur: str, nxt: str) -> bool:
    cur_e = normalize_task_status(cur); nxt_e = normalize_task_status(nxt)
    if cur_e in _TERMINAL_TASK and nxt_e != cur_e:
        return False
    if cur_e == TaskStatus.pending and nxt_e in (TaskStatus.queued, TaskStatus.failed, TaskStatus.canceled):
        return True
    if cur_e == TaskStatus.queued and nxt_e in (TaskStatus.running, TaskStatus.canceled):
        return True
    if cur_e == TaskStatus.running and nxt_e in (TaskStatus.success, TaskStatus.failed, TaskStatus.canceled):
        return True
    return cur_e == nxt_e


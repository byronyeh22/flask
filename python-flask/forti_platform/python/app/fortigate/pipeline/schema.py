# app/fortigate/pipeline/schema.py
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple
from flask import request


class PayloadError(Exception):
    """Raised when callback payload is invalid."""


# 必填欄位定義（依回呼種類）
_REQUIRED_FIELDS = {
    "validate": ["version", "task_id", "draft_id", "pipeline_id", "job_id", "result", "report"],
    "apply_start": ["version", "task_id", "draft_id", "pipeline_id", "job_id", "ts"],
    "apply": ["version", "task_id", "draft_id", "pipeline_id", "job_id", "result", "results"],
    "canceled": ["version", "task_id", "draft_id", "pipeline_id", "job_id", "reason"],
}

# 可接受的 result 值
_RESULT_ENUMS = {
    "validate": {"passed", "failed"},
    "apply": {"success", "failed", "partial"},
}


def _coerce_int(v: Any, default: Optional[int] = None) -> Optional[int]:
    if v is None:
        return default
    try:
        return int(v)
    except Exception:
        return default


def _ensure_required(payload: Dict[str, Any], required: List[str]) -> None:
    missing = [k for k in required if k not in payload]
    if missing:
        raise PayloadError(f"missing fields: {', '.join(missing)}")


def _normalize_common(payload: Dict[str, Any]) -> Dict[str, Any]:
    """共通欄位的型別/結構正規化。"""
    payload["task_id"] = _coerce_int(payload.get("task_id"))
    payload["draft_id"] = _coerce_int(payload.get("draft_id"))
    payload["pipeline_id"] = _coerce_int(payload.get("pipeline_id"))
    payload["job_id"] = _coerce_int(payload.get("job_id"))
    # 允許 chunked 回寫
    if "chunk_index" in payload:
        payload["chunk_index"] = _coerce_int(payload.get("chunk_index"))
    if "chunk_total" in payload:
        payload["chunk_total"] = _coerce_int(payload.get("chunk_total"))
    return payload


def _normalize_validate(payload: Dict[str, Any]) -> Dict[str, Any]:
    payload = _normalize_common(payload)
    result = str(payload.get("result", "")).lower()
    if result not in _RESULT_ENUMS["validate"]:
        raise PayloadError(f"invalid validate.result: {result!r}")
    # report 至少要是 dict
    if not isinstance(payload.get("report"), dict):
        raise PayloadError("report must be an object")
    return payload


def _normalize_apply_start(payload: Dict[str, Any]) -> Dict[str, Any]:
    payload = _normalize_common(payload)
    # ts 僅做存在性檢查，格式由後端保存原字串
    if not payload.get("ts"):
        raise PayloadError("ts is required")
    return payload


def _normalize_apply(payload: Dict[str, Any]) -> Dict[str, Any]:
    payload = _normalize_common(payload)
    result = str(payload.get("result", "")).lower()
    if result not in _RESULT_ENUMS["apply"]:
        raise PayloadError(f"invalid apply.result: {result!r}")
    # results 一律正規化為 list
    res = payload.get("results")
    if res is None:
        payload["results"] = []
    elif isinstance(res, list):
        payload["results"] = res
    else:
        raise PayloadError("results must be an array")
    # summary（若有）需為 dict
    if "summary" in payload and not isinstance(payload.get("summary"), dict):
        raise PayloadError("summary must be an object")
    return payload


def _normalize_canceled(payload: Dict[str, Any]) -> Dict[str, Any]:
    payload = _normalize_common(payload)
    if not payload.get("reason"):
        payload["reason"] = "unknown"
    return payload


_NORMALIZERS = {
    "validate": _normalize_validate,
    "apply_start": _normalize_apply_start,
    "apply": _normalize_apply,
    "canceled": _normalize_canceled,
}


def parse_callback_payload(
    kind: Optional[str] = None,
    payload: Optional[Dict[str, Any]] = None,
    required: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """
    解析並驗證 GitLab→Flask 回呼 payload。

    用法兼容：
      - parse_callback_payload(kind="validate")            # 從 request.json 取資料
      - parse_callback_payload("apply", payload=...)       # 指定 kind 與 payload
      - parse_callback_payload(payload=..., required=[...])# 自訂必填欄位（kind 可省略）

    參數：
      kind      : "validate" | "apply_start" | "apply" | "canceled" | None
      payload   : 指定要驗證的 dict；若省略，會從 Flask request 讀取 JSON
      required  : 覆寫必填欄位（優先於 kind 的預設）

    回傳：
      正規化後的 payload（含整型轉換、results 陣列化等）
    """
    # 讀入 payload
    if payload is None:
        payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        raise PayloadError("payload must be a JSON object")

    # 推測 kind（若未指定）
    if kind is None:
        # 盡量從特徵欄位判斷
        if "report" in payload and "result" in payload:
            kind = "validate"
        elif "ts" in payload and "pipeline_id" in payload and "job_id" in payload:
            kind = "apply_start"
        elif "results" in payload and "result" in payload:
            kind = "apply"
        elif "reason" in payload:
            kind = "canceled"
        else:
            # 無法推測，當作一般 payload，只做 required 檢查
            kind = "generic"

    # 決定必填欄位
    if required is None:
        required = _REQUIRED_FIELDS.get(kind, [])

    # 先檢查必填
    _ensure_required(payload, required)

    # 各類型正規化
    if kind in _NORMALIZERS:
        payload = _NORMALIZERS[kind](payload)
    else:
        # generic：僅做共通欄位轉型
        payload = _normalize_common(payload)

    return payload


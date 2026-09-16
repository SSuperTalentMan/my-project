"""审计与脱敏（架构文档 §3.4 / §12）。

双写：
1. ``logs/mcp_audit.jsonl`` —— 追加式结构化审计流（含 request_id），
   适合``tail -f`` 现场排障与 ELK/Loki 采集；
2. MySQL ``mcp_audit`` 表 —— 可 SQL 检索，支持按工具/时间/request_id 回溯。

脱敏：``api_key`` / ``password`` / ``token`` / ``phone`` / ``email`` /
``id_card`` / ``address`` 等字段在落盘前统一替换，避免密钥与 PII 泄漏到日志。
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import threading
from typing import Any, Dict, Optional

from commercepivot.core.context import get_request_id
from commercepivot.core.logging import audit_file, get_logger

log = get_logger("commercepivot.mcp.audit")

MASK = "***"
_SENSITIVE_KEYS = {
    "api_key", "apikey", "api_secret", "password", "passwd", "pwd", "token",
    "access_token", "refresh_token", "secret", "authorization", "auth",
    "phone", "mobile", "telephone", "id_card", "idcard", "email", "address",
}
_PHONE_RE = re.compile(r"(?<!\d)(1[3-9]\d)\d{4}(\d{4})(?!\d)")
_EMAIL_RE = re.compile(r"([\w.+-]{1,3})[\w.+-]*@([\w-]+)(\.[\w.-]+)+")
_MAX_BYTES = 10 * 1024 * 1024  # 单文件 10MB，超出后滚动
_LOCK = threading.Lock()


def mask_value(value: Any) -> Any:
    """对字符串做手机号 / 邮箱二次脱敏。"""
    if not isinstance(value, str):
        return value
    out = _PHONE_RE.sub(r"\1****\2", value)
    return _EMAIL_RE.sub(r"\1***@\2\3", out)


def mask_params(params: Any, depth: int = 0) -> Any:
    """递归脱敏。深度限制 6 层，防止异常结构导致递归爆栈。"""
    if depth > 6:
        return "<too-deep>"
    if isinstance(params, dict):
        out: Dict[str, Any] = {}
        for k, v in params.items():
            if str(k).lower() in _SENSITIVE_KEYS:
                out[k] = MASK
            else:
                out[k] = mask_params(v, depth + 1)
        return out
    if isinstance(params, (list, tuple)):
        return [mask_params(v, depth + 1) for v in params]
    return mask_value(params)


def _shorten(value: Any, limit: int = 4000) -> Any:
    """结果过长时截断，避免审计表被大结果撑爆。"""
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = str(value)
    if len(text) <= limit:
        return value
    return {"_truncated": True, "_bytes": len(text), "_preview": text[:limit]}


def _rotate(path: str) -> None:
    try:
        if os.path.exists(path) and os.path.getsize(path) > _MAX_BYTES:
            backup = path + ".1"
            if os.path.exists(backup):
                os.remove(backup)
            os.replace(path, backup)
    except OSError:
        pass


def write_audit_file(record: Dict[str, Any]) -> None:
    path = audit_file()
    with _LOCK:
        _rotate(path)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


def write_audit(
    tool_name: str,
    params: Any,
    result: Any = None,
    elapsed_ms: int = 0,
    status: str = "ok",
    error: Optional[str] = None,
    write_db: bool = True,
) -> Dict[str, Any]:
    """写一条审计记录，返回落盘的那条记录（便于单测断言）。"""
    record = {
        "ts": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
        "request_id": get_request_id(),
        "tool": tool_name,
        "status": status,
        "elapsed_ms": int(elapsed_ms),
        "params": mask_params(params),
        "error": error,
        "result_preview": _shorten(mask_params(result), 1500) if result is not None else None,
    }
    write_audit_file(record)

    if write_db:
        try:
            from commercepivot.db import repository as repo

            repo.insert_audit(
                tool_name=tool_name,
                params=record["params"],
                result=_shorten(result, 20000),
                elapsed_ms=int(elapsed_ms),
                status=status,
                request_id=record["request_id"],
            )
        except Exception as exc:  # 审计落库失败不能影响主流程
            log.warning("审计落库失败（已仅写 JSONL）", tool=tool_name, error=str(exc))
    return record


__all__ = ["mask_params", "mask_value", "write_audit", "write_audit_file"]

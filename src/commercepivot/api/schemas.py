"""接入层请求 / 响应模型（架构文档 §3.1）。"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class AskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(..., min_length=1, max_length=1000, description="用户问题")
    session_id: Optional[str] = Field(None, max_length=64, description="会话 ID，留空则新建")
    top_k: Optional[int] = Field(None, ge=1, le=50, description="结果条数（排行类问题适用）")
    include_trace: bool = Field(False, description="是否回传全链路 trace（排障用）")
    use_cache: bool = Field(True, description="是否使用问答缓存")
    user_id: Optional[str] = Field(None, max_length=64)


class AskResponse(BaseModel):
    answer: str
    session_id: str
    request_id: str
    answer_source: str = "template"
    cached: bool = False

    intent: Dict[str, Any] = Field(default_factory=dict)
    slots: Dict[str, Any] = Field(default_factory=dict)
    missing_slots: List[str] = Field(default_factory=list)
    defaults_applied: List[str] = Field(default_factory=list)

    plan: List[Dict[str, Any]] = Field(default_factory=list)
    plan_reason: str = ""
    agent_tasks: List[Dict[str, Any]] = Field(default_factory=list)

    table: Optional[Dict[str, Any]] = None
    findings: List[str] = Field(default_factory=list)
    metrics: Dict[str, Any] = Field(default_factory=dict)
    citations: List[Dict[str, Any]] = Field(default_factory=list)
    sources: List[Dict[str, Any]] = Field(default_factory=list)

    degraded: bool = False
    degrade_reasons: List[str] = Field(default_factory=list)
    notes: List[str] = Field(default_factory=list)
    errors: List[str] = Field(default_factory=list)

    node_timings: Dict[str, int] = Field(default_factory=dict)
    elapsed_ms: int = 0
    orchestrator_mode: str = ""
    answer_meta: Dict[str, Any] = Field(default_factory=dict)
    trace: Optional[List[Dict[str, Any]]] = None


class TokenRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subject: str = Field(..., min_length=1, max_length=64, description="用户名 / 主体标识")
    role: str = Field("customer", description="角色：admin / operator / customer")
    expires_minutes: Optional[int] = Field(None, ge=1, le=10080)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    role: str
    expires_minutes: int


class SessionResponse(BaseModel):
    session_id: str
    role: str = "customer"
    messages: List[Dict[str, Any]] = Field(default_factory=list)
    turns: List[Dict[str, Any]] = Field(default_factory=list)


class AgentRpcRequest(BaseModel):
    """A2A JSON-RPC 2.0 请求体（§4.2）。"""

    model_config = ConfigDict(extra="ignore")

    jsonrpc: str = "2.0"
    id: Optional[Any] = None
    method: str = Field(..., description="tasks/send | tasks/get | tasks/cancel | agent/card")
    params: Dict[str, Any] = Field(default_factory=dict)


class AgentRpcResponse(BaseModel):
    jsonrpc: str = "2.0"
    id: Optional[Any] = None
    result: Optional[Dict[str, Any]] = None
    error: Optional[Dict[str, Any]] = None


__all__ = [
    "AgentRpcRequest",
    "AgentRpcResponse",
    "AskRequest",
    "AskResponse",
    "SessionResponse",
    "TokenRequest",
    "TokenResponse",
]

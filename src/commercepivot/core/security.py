"""鉴权与限流（架构文档 §3.1）。

- JWT：HS256，纯标准库实现（hmac + hashlib + base64），不引入额外依赖。
- 角色：admin / operator / customer，权限矩阵集中在 ``ROLE_PERMISSIONS``。
- 限流：Redis 固定窗口，按 ``IP + 接口路径`` 维度计数；Redis 不可用时
  **fail-open**（放行），符合 §11 降级策略。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List

from fastapi import Depends, Header, HTTPException, Request, status

from commercepivot.core.config import ROLES, Role, get_settings
from commercepivot.core.context import set_current_user
from commercepivot.core.errors import AuthError, ForbiddenError
from commercepivot.core.logging import get_logger

log = get_logger("commercepivot.security")

# ---------------------------------------------------------------- 权限矩阵
ROLE_PERMISSIONS: Dict[str, List[str]] = {
    "admin": ["*"],
    "operator": ["chat", "mcp", "mcp:read", "a2a", "report", "ticket"],
    "customer": ["chat"],
}


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    try:
        return base64.urlsafe_b64decode(text + pad)
    except (binascii.Error, ValueError) as exc:  # pragma: no cover
        raise AuthError("token 编码非法") from exc


def create_access_token(
    subject: str,
    role: Role | str = "customer",
    expires_minutes: int | None = None,
    extra: Dict[str, Any] | None = None,
) -> str:
    """签发 HS256 JWT。"""
    settings = get_settings()
    if role not in ROLES:
        raise AuthError(f"未知角色：{role}")
    now = int(time.time())
    payload: Dict[str, Any] = {
        "sub": subject,
        "role": role,
        "iat": now,
        "exp": now + 60 * (expires_minutes or settings.jwt_expire_minutes),
        "iss": "commercepivot",
    }
    if extra:
        payload.update(extra)
    header = {"alg": settings.jwt_algorithm, "typ": "JWT"}
    signing_input = f"{_b64url_encode(json.dumps(header, separators=(',', ':')).encode())}." \
                    f"{_b64url_encode(json.dumps(payload, separators=(',', ':'), ensure_ascii=False).encode())}"
    sig = hmac.new(settings.jwt_secret.encode(), signing_input.encode(), hashlib.sha256).digest()
    return f"{signing_input}.{_b64url_encode(sig)}"


def decode_token(token: str) -> Dict[str, Any]:
    """校验签名与过期时间，返回 payload。"""
    settings = get_settings()
    parts = token.split(".")
    if len(parts) != 3:
        raise AuthError("token 格式非法")
    signing_input = f"{parts[0]}.{parts[1]}"
    expected = hmac.new(
        settings.jwt_secret.encode(), signing_input.encode(), hashlib.sha256
    ).digest()
    if not hmac.compare_digest(expected, _b64url_decode(parts[2])):
        raise AuthError("token 签名校验失败")
    try:
        payload = json.loads(_b64url_decode(parts[1]))
    except json.JSONDecodeError as exc:
        raise AuthError("token payload 解析失败") from exc
    if int(payload.get("exp", 0)) < int(time.time()):
        raise AuthError("token 已过期")
    return payload


def role_allows(role: str, permission: str) -> bool:
    perms = ROLE_PERMISSIONS.get(role, [])
    if "*" in perms:
        return True
    if permission in perms:
        return True
    # mcp:read 蕴含 mcp
    return permission == "mcp" and "mcp:read" in perms


@dataclass(slots=True)
class Principal:
    subject: str
    role: str
    raw_token: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {"subject": self.subject, "role": self.role}

    def can(self, permission: str) -> bool:
        return role_allows(self.role, permission)


def principal_from_request(request: Request) -> Principal:
    """从 Authorization: Bearer / X-Role（仅 debug）解析调用者身份。"""
    settings = get_settings()
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        payload = decode_token(auth[7:].strip())
        return Principal(
            subject=str(payload.get("sub", "anonymous")),
            role=str(payload.get("role", "customer")),
            raw_token=auth[7:].strip(),
        )
    if settings.debug:
        # 本地联调便利：允许显式声明角色，避免每次都要签 token
        dev_role = request.headers.get("x-role")
        if dev_role:
            if dev_role not in ROLES:
                raise AuthError(f"X-Role 非法：{dev_role}")
            return Principal(subject=request.headers.get("x-subject", f"dev-{dev_role}"), role=dev_role)
    raise AuthError("缺少 Bearer Token")


def require(*permissions: str):
    """FastAPI 依赖工厂：要求当前调用者具备指定权限。"""

    async def _dep(request: Request) -> Principal:
        principal = principal_from_request(request)
        for perm in permissions:
            if not principal.can(perm):
                raise ForbiddenError(
                    f"角色 {principal.role} 无权执行 {perm}",
                    details={"required": list(permissions), "role": principal.role},
                )
        set_current_user(principal.as_dict())
        request.state.principal = principal
        return principal

    return _dep


require_auth = require()


# ---------------------------------------------------------------- 限流
class RateLimiter:
    """Redis 固定窗口限流；Redis 异常时 fail-open。"""

    def __init__(self, per_minute: int | None = None, enabled: bool | None = None) -> None:
        settings = get_settings()
        self.per_minute = per_minute or settings.rate_limit_per_minute
        self.enabled = settings.rate_limit_enabled if enabled is None else enabled

    async def check(self, ip: str, path: str) -> Dict[str, Any]:
        from commercepivot.db.redis import get_redis

        info = {"allowed": True, "limit": self.per_minute, "remaining": self.per_minute, "degraded": False}
        if not self.enabled:
            return info

        window = int(time.time() // 60)
        key = f"rate_limit:{ip}:{path}:{window}"
        try:
            r = get_redis()
            count = r.incr(key)
            if count == 1:
                r.expire(key, 120)
            info["remaining"] = max(0, self.per_minute - int(count))
            info["allowed"] = int(count) <= self.per_minute
        except Exception as exc:  # Redis 不可用 -> fail-open
            info["degraded"] = True
            log.warning("限流降级（Redis 不可用，fail-open）", error=str(exc))
        return info


async def rate_limit_dependency(request: Request) -> None:
    limiter = RateLimiter()
    ip = request.client.host if request.client else "unknown"
    key = request.url.path
    info = await limiter.check(ip, key)
    request.state.rate_limit = info
    if not info["allowed"]:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "code": "RATE_LIMITED",
                "message": f"请求过于频繁，限额 {info['limit']} 次/分钟",
                "retry_after": 60,
            },
            headers={"Retry-After": "60"},
        )


def ensure_write_allowed(operation: str) -> None:
    """§11：写操作未开启时直接拒绝，保证默认只读。"""
    if not get_settings().enable_write_ops:
        raise ForbiddenError(
            f"写操作 {operation} 未开启（ENABLE_WRITE_OPS=false）",
            details={"hint": "在 .env 中设置 ENABLE_WRITE_OPS=true 后重启服务"},
        )


def mask_secret(value: str) -> str:
    """脱敏：用于审计与日志。"""
    if not value:
        return ""
    if len(value) <= 6:
        return "***"
    return f"{value[:3]}***{value[-2:]}"


def verify_role_header(role: str | None) -> str:
    return role if role in ROLES else "customer"


__all__ = [
    "Principal",
    "ROLE_PERMISSIONS",
    "RateLimiter",
    "create_access_token",
    "decode_token",
    "ensure_write_allowed",
    "mask_secret",
    "principal_from_request",
    "rate_limit_dependency",
    "require",
    "require_auth",
    "role_allows",
]

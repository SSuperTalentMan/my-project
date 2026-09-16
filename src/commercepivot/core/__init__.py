"""核心层：配置、日志、鉴权、限流、指标、错误。

对应架构文档 §3.1 接入层 与 §9 配置管理、§12 可观测性。
"""

from commercepivot.core.config import BASE_DIR, DATA_DIR, LOG_DIR, Settings, get_settings
from commercepivot.core.errors import PivotError

__all__ = [
    "BASE_DIR",
    "DATA_DIR",
    "LOG_DIR",
    "Settings",
    "get_settings",
    "PivotError",
]

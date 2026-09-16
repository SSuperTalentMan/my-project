"""全局配置中心（架构文档 §9 配置管理）。

设计要点
--------
1. 所有敏感配置只从 环境变量 / 项目根目录 .env 读取，代码内不出现真实密钥。
   真实 API_KEY 只写进控制台环境变量，.env 里保留占位符 ``sk-your-dashscope-api-key``。
2. 配置对象是**单例**（``get_settings()`` 带 lru_cache），全链路共享，
   避免各模块重复解析 .env 造成的不一致。
3. 每个外部依赖都带 *降级开关*：API_KEY 为空 → LLM 走模板兜底；
   模型路径为空 → 走规则/哈希兜底。保证「零外部依赖也能启动」。
"""

from __future__ import annotations

import sys
from functools import lru_cache
from pathlib import Path
from typing import List, Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# src/commercepivot/core/config.py -> parents[3] == 项目根目录 D:\CommercePivot
BASE_DIR = Path(__file__).resolve().parents[3]
DATA_DIR = BASE_DIR / "data"
LOG_DIR = BASE_DIR / "logs"
KNOWLEDGE_DIR = DATA_DIR / "knowledge"

Role = Literal["admin", "operator", "customer"]
ROLES: tuple[str, ...] = ("admin", "operator", "customer")

# .env 中约定的占位符：出现即视为「未配置真实密钥」
PLACEHOLDER_KEYS = (
    "sk-your-dashscope-api-key",
    "your-dashscope-api-key",
    "",
)


def read_windows_env(name: str) -> str:
    """读取 Windows 注册表里的环境变量（用户级优先，其次系统级）。

    **为什么需要它**：环境变量是「进程启动那一刻的快照」。通过控制台 /
    ``setx`` 设置 ``API_KEY`` 之后，**此前已经启动的进程读不到它** —— IDE
    (PyCharm)、终端、服务进程都会中招，表现为「明明配了密钥，却静默降级成
    模板回答」。注册表是权威存储且可实时读取，因此作为最后一道兜底。

    非 Windows 或注册表不可用时静默返回空串（fail-open，不影响主线逻辑）。
    """
    if sys.platform != "win32":
        return ""
    try:
        import winreg
    except ImportError:  # pragma: no cover - 非 Windows 不会有此分支
        return ""

    locations = (
        (winreg.HKEY_CURRENT_USER, "Environment"),
        (
            winreg.HKEY_LOCAL_MACHINE,
            r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment",
        ),
    )
    for root, subkey in locations:
        try:
            with winreg.OpenKey(root, subkey) as key:
                value, _ = winreg.QueryValueEx(key, name)
        except OSError:
            continue
        text = str(value).strip() if value is not None else ""
        if text:
            return text
    return ""


class Settings(BaseSettings):
    """全量运行时配置。字段名即环境变量名（大小写不敏感）。"""

    model_config = SettingsConfigDict(
        env_file=str(BASE_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ------------------------------------------------------------------ 应用
    app_name: str = "商枢 CommercePivot"
    app_version: str = "1.2.0"
    debug: bool = True
    host: str = "0.0.0.0"
    api_port: int = 8000
    mcp_port: int = 8001
    a2a_port_base: int = 8002
    cors_origins: List[str] = Field(default_factory=lambda: ["*"])

    # ------------------------------------------------------------ 大模型（百炼）
    # 默认模型与架构文档 §3.6 一致：qwen3.7-flash。
    # 该模型名在百炼 OpenAI 兼容模式下真实可用（2026-09-14 实测 http=200）。
    # 注意：qwen-flash / qwen-plus / qwen3-max / qwen-max / qwen-turbo 在当前账号下
    # 均报 403 AllocationQuota.FreeTierOnly（免费额度耗尽），不要改回这些名字。
    # 如需换模型，通过环境变量 LLM_MODEL 覆盖。
    api_key: str = ""
    llm_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    llm_model: str = "qwen3.7-flash"
    llm_temperature: float = 0.2
    llm_timeout_s: float = 60.0
    llm_max_retries: int = 2
    # qwen3.7-flash 是**混合推理模型**，默认会先吐一大段思维链（实测 2447 字），
    # 既拖慢响应（12.7s vs 5.3s）又照常扣额度（839 vs 300 completion tokens）。
    # 本项目的生成任务只是「把 MCP 查询结果改写成通顺话术」，不需要推理，
    # 故默认关闭。置 True 则恢复模型默认行为（多步推理场景可开）。
    llm_enable_thinking: bool = False

    # ----------------------------------------------------------------- MySQL
    mysql_host: str = "localhost"
    mysql_port: int = 3306
    mysql_user: str = "root"
    mysql_password: str = "root"
    mysql_db: str = "commercepivot"
    mysql_charset: str = "utf8mb4"
    mysql_pool_size: int = 8
    mysql_connect_timeout: int = 5

    # ----------------------------------------------------------------- Redis
    redis_host: str = "localhost"
    redis_port: int = 6379
    redis_password: str = "1234"
    redis_db: int = 0
    redis_connect_timeout: float = 1.5

    # ------------------------------------------------------- Milvus（Lite 或 Standalone）
    milvus_uri: str = "./data/milvus_demo.db"
    milvus_collection_product: str = "product_kb"
    milvus_collection_faq: str = "faq_kb"
    milvus_dense_dim: int = 1024
    # 远程 Milvus 连接超时：调小可让「服务没起」这种情况快速降级而不是干等
    milvus_connect_timeout_s: float = 3.0
    retrieval_top_k: int = 10
    rerank_top_k: int = 3

    # ------------------------------------------------------------- 本地模型
    # 留空则自动降级：意图走规则，向量走哈希特征
    intent_bert_path: str = ""
    intent_confidence_threshold: float = 0.55
    bge_m3_path: str = ""
    reranker_path: str = ""
    hf_endpoint: str = ""  # 可选 HF 镜像，如 https://hf-mirror.com
    # 是否允许从 HuggingFace 自动下载模型权重。默认关闭：
    # 离线/内网环境下加载会长时间阻塞，本项目统一走「本地有则用，没有则降级」。
    model_autodownload: bool = False

    # ----------------------------------------------------------------- 安全
    jwt_secret: str = "commercepivot-dev-secret-change-me"
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 720
    rate_limit_enabled: bool = True
    rate_limit_per_minute: int = 120
    enable_write_ops: bool = False  # §11：默认只读，写操作需显式开启

    # ------------------------------------------------------------- 链路与熔断
    mcp_tool_timeout_s: float = 10.0
    mcp_tool_retries: int = 1  # §3.4 / §11：失败重试 1 次
    # MCP 调用方式：local=进程内直调（默认，零网络开销）；
    # http=走独立 MCP Server 端口（:8001），用于验证跨进程协议链路
    mcp_transport: str = "local"
    a2a_timeout_s: float = 15.0
    a2a_circuit_fail_threshold: int = 3
    a2a_circuit_reset_s: float = 30.0
    cache_ttl_s: int = 300

    # ------------------------------------------------------------- 日志与审计
    log_level: str = "INFO"
    log_json: bool = False
    # 日志输出流：stdout（默认，长驻服务）| stderr。
    # stdio 传输下 stdout 是 JSON-RPC 专用通道，任何多余输出都会让宿主解析失败，
    # 因此 stdio_server 会强制把它切到 stderr（见 core/logging.py 的说明）。
    log_stream: str = "stdout"
    audit_log_path: str = "logs/mcp_audit.jsonl"

    # ------------------------------------------------------------- 派生属性
    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, v: object) -> object:
        if isinstance(v, str):
            return [x.strip() for x in v.split(",") if x.strip()]
        return v

    @model_validator(mode="after")
    def _fill_api_key_from_registry(self) -> "Settings":
        """环境变量与 .env 都没给出真实密钥时，从注册表兜底取一次。

        只在当前取到的是空值/占位符时生效，因此不会覆盖任何已正确配置的
        环境（环境变量 > .env > 注册表）。这样 IDE 里「一键启动」无需手填
        密钥，也避免把真实密钥写进任何受版本控制的文件。
        """
        if self.api_key.strip() in PLACEHOLDER_KEYS:
            found = read_windows_env("API_KEY")
            if found:
                self.api_key = found
        return self

    @property
    def milvus_abs_uri(self) -> str:
        """把 ./data/xxx.db 解析成绝对路径，避免工作目录变化导致文件漂移。"""
        p = Path(self.milvus_uri)
        return str(p if p.is_absolute() else (BASE_DIR / p).resolve())

    @property
    def audit_abs_path(self) -> Path:
        p = Path(self.audit_log_path)
        return p if p.is_absolute() else (BASE_DIR / p)

    @property
    def has_real_api_key(self) -> bool:
        return self.api_key.strip() not in PLACEHOLDER_KEYS

    @property
    def llm_enabled(self) -> bool:
        return self.has_real_api_key

    @property
    def mcp_call_url(self) -> str:
        return f"http://127.0.0.1:{self.mcp_port}/api/v1/mcp"

    def public_dict(self) -> dict:
        """对外暴露的配置快照，密钥一律脱敏。"""

        def mask(v: str) -> str:
            if not v:
                return "<empty>"
            return f"{v[:3]}***{v[-2:]}" if len(v) > 6 else "***"

        return {
            "app_name": self.app_name,
            "app_version": self.app_version,
            "llm_model": self.llm_model,
            "llm_enabled": self.llm_enabled,
            "llm_enable_thinking": self.llm_enable_thinking,
            "api_key": mask(self.api_key),
            "mysql": f"{self.mysql_user}@{self.mysql_host}:{self.mysql_port}/{self.mysql_db}",
            "redis": f"{self.redis_host}:{self.redis_port}/{self.redis_db}",
            "milvus_uri": self.milvus_abs_uri,
            "enable_write_ops": self.enable_write_ops,
            "rate_limit_per_minute": self.rate_limit_per_minute,
            "mcp_tool_timeout_s": self.mcp_tool_timeout_s,
            "a2a_timeout_s": self.a2a_timeout_s,
        }


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """进程内单例。"""
    for d in (DATA_DIR, LOG_DIR, KNOWLEDGE_DIR):
        d.mkdir(parents=True, exist_ok=True)
    return Settings()

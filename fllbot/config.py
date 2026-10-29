"""配置加载：默认值 <- config.toml <- .env <- 环境变量。

环境变量规则：``FLLBOT_`` + 段落 + ``__`` + 键名，例如 ``FLLBOT_SERVER__PORT=6702``。
值为 JSON 时按 JSON 解析，否则列表按逗号切分，其余交给 pydantic 做宽松转换。
"""

from __future__ import annotations

import copy
import json
import os
import tomllib
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

DEFAULT_CONFIG_FILE = "config.toml"
ENV_PREFIX = "FLLBOT_"
ENV_DELIMITER = "__"


class ServerConfig(BaseModel):
    """HTTP / WebSocket 监听配置。"""

    host: str = "127.0.0.1"
    port: int = 6701
    ws_path: str = "/onebot/v11/ws"
    access_token: str = ""
    heartbeat_timeout: float = 120.0
    # 单次 OneBot API 调用等待响应的秒数。
    call_timeout: float = 30.0
    cors_origins: list[str] = Field(default_factory=lambda: ["*"])


class AdminConfig(BaseModel):
    """管理 API 配置。"""

    enabled: bool = True
    token: str = ""
    token_file: str = "data/admin_token.txt"


class BotConfig(BaseModel):
    """机器人行为配置。"""

    superusers: list[int] = Field(default_factory=list)
    command_prefix: list[str] = Field(default_factory=lambda: ["/"])
    allow_bare_command: bool = True
    error_reply: str = ""


class PermissionConfig(BaseModel):
    """权限与群组开关。"""

    # 非空时，只有列表内的群才会被处理（白名单）。
    allow_groups: list[int] = Field(default_factory=list)
    # 黑名单里的群一律忽略。
    deny_groups: list[int] = Field(default_factory=list)
    # 未登记过的群默认是否启用。
    default_group_enabled: bool = True
    # 是否把群主/管理员视为高权限（可用 group_admin 权限的处理器）。
    group_admin_permission: bool = True
    # 是否允许私聊使用命令（False 时只响应群聊）。
    allow_private: bool = True


class PluginConfig(BaseModel):
    """插件加载配置。"""

    dirs: list[str] = Field(default_factory=lambda: ["plugins"])
    builtin: bool = True
    hot_reload: bool = True
    watch_interval: float = 2.0
    max_concurrency: int = 8
    handler_timeout: float = 60.0


class StorageConfig(BaseModel):
    """持久化配置。"""

    path: str = "data/fllbot.db"


class LoggingConfig(BaseModel):
    """日志配置。"""

    level: str = "INFO"
    dir: str = "logs"
    json_lines: bool = True
    file: str = "fllbot.log"
    ring_size: int = 500


class LLMConfig(BaseModel):
    """大模型对话配置（OpenAI 兼容接口）。"""

    enabled: bool = False
    base_url: str = "https://api.deepseek.com"
    api_key: str = ""
    model: str = "deepseek-chat"
    timeout: float = 60.0
    max_tokens: int = 800
    temperature: float = 0.8
    max_context_messages: int = 16
    max_context_chars: int = 4000
    enable_private: bool = True
    enable_group: bool = True
    trigger: str = "at"
    trigger_prefix: str = "ai"
    persona: str = "fllbot"
    chunk_size: int = 500
    cooldown_seconds: float = 3.0
    system_extra: str = ""


class AppConfig(BaseModel):
    """完整配置树。"""

    server: ServerConfig = Field(default_factory=ServerConfig)
    admin: AdminConfig = Field(default_factory=AdminConfig)
    bot: BotConfig = Field(default_factory=BotConfig)
    permission: PermissionConfig = Field(default_factory=PermissionConfig)
    plugin: PluginConfig = Field(default_factory=PluginConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)


# --------------------------------------------------------------------------- #
# 加载
# --------------------------------------------------------------------------- #


def deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """递归合并字典，返回新对象；``patch`` 覆盖 ``base``。"""
    result = copy.deepcopy(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _parse_env_value(raw: str) -> Any:
    """把环境变量字符串还原成 Python 值。"""
    text = raw.strip()
    if not text:
        return ""
    if text[0] in "[{" and text[-1] in "]}":
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return raw
    if text.lower() in {"true", "false"}:
        return text.lower() == "true"
    if "," in text:
        parts = [p.strip() for p in text.split(",")]
        if all(p.replace("-", "").isdigit() for p in parts if p):
            return [int(p) for p in parts if p]
        return [p for p in parts if p]
    return raw


def parse_dotenv(path: Path) -> dict[str, str]:
    """极简 .env 解析，支持 ``KEY=VALUE``、引号与 ``#`` 注释。"""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


def env_overrides(environ: dict[str, str] | None = None, prefix: str = ENV_PREFIX) -> dict[str, Any]:
    """收集 ``FLLBOT_*`` 环境变量并展开成嵌套字典。"""
    environ = os.environ if environ is None else environ
    result: dict[str, Any] = {}
    for key, raw in environ.items():
        upper = key.upper()
        if not upper.startswith(prefix):
            continue
        path = [part.lower() for part in upper[len(prefix) :].split(ENV_DELIMITER) if part]
        if not path:
            continue
        node = result
        for part in path[:-1]:
            child = node.get(part)
            if not isinstance(child, dict):
                child = {}
                node[part] = child
            node = child
        node[path[-1]] = _parse_env_value(raw)
    return result


def load_config_dict(
    path: Path | str | None = DEFAULT_CONFIG_FILE,
    *,
    root: Path | None = None,
    environ: dict[str, str] | None = None,
) -> dict[str, Any]:
    """读取 TOML 与环境变量，返回可交给 :class:`AppConfig` 校验的字典。"""
    root = Path(root) if root is not None else Path.cwd()
    merged: dict[str, Any] = {}

    config_path = Path(path) if path is not None else None
    if config_path is not None and not config_path.is_absolute():
        config_path = root / config_path
    if config_path is not None and config_path.is_file():
        with config_path.open("rb") as handle:
            merged = deep_merge(merged, tomllib.load(handle))

    dotenv = parse_dotenv(root / ".env")
    # .env 里也允许直接写 FLLBOT_* 或以 DEEPSEEK_API_KEY 提供密钥。
    if "DEEPSEEK_API_KEY" in dotenv and "FLLBOT_LLM__API_KEY" not in dotenv:
        dotenv["FLLBOT_LLM__API_KEY"] = dotenv["DEEPSEEK_API_KEY"]
    environ = dict(os.environ) if environ is None else dict(environ)
    dotenv_env = {k: v for k, v in dotenv.items() if k.startswith(ENV_PREFIX)}
    merged = deep_merge(merged, env_overrides({**dotenv_env, **environ}))

    if not merged.get("llm", {}).get("api_key"):
        fallback = dotenv.get("DEEPSEEK_API_KEY") or environ.get("DEEPSEEK_API_KEY") or environ.get("OPENAI_API_KEY")
        if fallback:
            merged.setdefault("llm", {})["api_key"] = fallback
    return merged


def load_config(
    path: Path | str | None = DEFAULT_CONFIG_FILE,
    *,
    root: Path | None = None,
    environ: dict[str, str] | None = None,
) -> AppConfig:
    """加载并校验配置。配置文件不存在时全部使用默认值。"""
    data = load_config_dict(path, root=root, environ=environ)
    return AppConfig.model_validate(data)


def resolve_path(root: Path, value: str) -> Path:
    """把配置里的相对路径解析到项目根目录。"""
    candidate = Path(value).expanduser()
    return candidate if candidate.is_absolute() else (root / candidate)


def mask_secrets(config: AppConfig) -> dict[str, Any]:
    """导出配置用于 API 展示，隐藏密钥。"""
    data = config.model_dump()
    data["llm"]["api_key"] = _mask(config.llm.api_key)
    data["admin"]["token"] = _mask(config.admin.token)
    data["server"]["access_token"] = _mask(config.server.access_token)
    return data


def _mask(secret: str) -> str:
    if not secret:
        return ""
    if len(secret) <= 8:
        return "*" * len(secret)
    return f"{secret[:4]}...{secret[-4:]}"


class ConfigStore:
    """持有当前生效配置，并支持运行时覆盖（持久化在数据库 meta 表）。"""

    def __init__(self, base: dict[str, Any]) -> None:
        self._base = copy.deepcopy(base)
        self._overrides: dict[str, Any] = {}
        self.current = AppConfig.model_validate(self._base)

    def patch(self, overrides: dict[str, Any]) -> AppConfig:
        """合并覆盖项并重新校验；失败时抛出 ``ValidationError`` 且不生效。"""
        merged_overrides = deep_merge(self._overrides, overrides)
        data = deep_merge(self._base, merged_overrides)
        config = AppConfig.model_validate(data)
        self._overrides = merged_overrides
        self.current = config
        return config

    def restore(self, overrides: dict[str, Any]) -> AppConfig:
        """启动时从数据库恢复运行时覆盖。"""
        self._overrides = {}
        return self.patch(overrides) if overrides else self.current

    @property
    def overrides(self) -> dict[str, Any]:
        return copy.deepcopy(self._overrides)

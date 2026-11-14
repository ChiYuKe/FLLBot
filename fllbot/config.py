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
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

DEFAULT_CONFIG_FILE = "config.toml"
ENV_PREFIX = "FLLBOT_"
ENV_DELIMITER = "__"

# These values determine constructed components or local management access.
# They must come from the startup configuration, never a runtime DB override.
STARTUP_FIELDS = frozenset({
    'server.host', 'server.port', 'server.cors_origins', 'admin.enabled', 'admin.token_file',
    'plugin.dirs', 'plugin.builtin', 'plugin.hot_reload', 'plugin.watch_interval',
    'plugin.max_concurrency', 'plugin.max_concurrent_per_group', 'plugin.max_pending_events', 'plugin.max_pending_per_conversation', 'storage.path',
    'logging.dir', 'logging.file', 'logging.json_lines', 'logging.ring_size',
})


class StartupConfigError(ValueError):
    def __init__(self, fields: list[str]) -> None:
        self.fields = fields
        super().__init__('这些参数只能在启动配置文件或启动命令中修改并重启：' + '、'.join(fields) + '；本次未保存任何改动')


class ConfigConflictError(ValueError):
    pass


def split_startup_overrides(overrides: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    active = copy.deepcopy(overrides)
    archived = {}
    for path in sorted(STARTUP_FIELDS):
        section, key = path.split('.')
        if isinstance(active.get(section), dict) and key in active[section]:
            archived[path] = active[section].pop(key)
            if not active[section]:
                active.pop(section)
    return active, archived


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
    # auto preserves existing allow-list semantics; listed also supports an empty list.
    group_access: Literal["auto", "all", "listed"] = "auto"
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
    max_concurrency: int = Field(default=6, ge=1, le=100)
    max_concurrent_per_group: int = Field(default=6, ge=1, le=100)
    max_pending_events: int = Field(default=1000, ge=1, le=100_000)
    max_pending_per_conversation: int = Field(default=32, ge=1, le=10000)
    event_deadline_seconds: float = Field(default=360.0, gt=0, le=3600)
    queue_timeout_seconds: float = Field(default=30.0, gt=0, le=3600)
    handler_timeout: float = 360.0


class StorageConfig(BaseModel):
    """持久化配置。"""

    path: str = "data/fllbot.db"
    backup_dir: str = "data/backups"


class LoggingConfig(BaseModel):
    """日志配置。"""

    level: str = "INFO"
    dir: str = "logs"
    json_lines: bool = True
    file: str = "fllbot.log"
    ring_size: int = 500
    capture_message_content: bool = False
    message_content_hours: int = Field(default=24,ge=1,le=168)


class ModelCapabilities(BaseModel):
    """显式能力声明；默认保持旧版兼容行为，不根据模型名称猜测。"""

    context_window: int | None = Field(default=None, ge=1, le=10_000_000)
    supports_tools: bool = True
    supports_images: bool = True
    supports_temperature: bool = True
    token_parameter: Literal["max_tokens", "max_completion_tokens"] = "max_tokens"
    image_token_budget: int = Field(default=4096, ge=1)


class LLMProviderConfig(ModelCapabilities):
    """单个模型提供方（OpenAI 兼容协议）。留空的字段继承 ``[llm]`` 里的默认值。"""

    base_url: str = ""
    api_key: str = ""
    api_key_env: str = ""
    inherit_api_key: bool = True
    model: str = ""
    timeout: float | None = None
    max_tokens: int | None = Field(default=None, ge=1, le=1_000_000)
    temperature: float | None = None
    no_stream: bool = False
    input_price: float = Field(default=0.0, ge=0, allow_inf_nan=False)
    output_price: float = Field(default=0.0, ge=0, allow_inf_nan=False)
    max_concurrency: int = Field(default=6, ge=1, le=100)



class LLMConfig(ModelCapabilities):
    """大模型对话配置（OpenAI 兼容接口）。"""

    enabled: bool = False
    base_url: str = "https://api.deepseek.com"
    api_key: str = ""
    model: str = "deepseek-chat"
    timeout: float = 60.0
    max_tokens: int = Field(default=4000, ge=1, le=1_000_000)
    temperature: float = 0.8
    context_mode: Literal["model", "custom"] = "model"
    max_context_messages: int = 16
    max_context_chars: int = 4000
    enable_private: bool = True
    enable_group: bool = True
    trigger: str = "at"
    trigger_prefix: str = "ai"
    persona: str = ""
    chunk_size: int = 500
    cooldown_seconds: float = 3.0
    system_extra: str = ""
    default: str = "default"
    providers: dict[str, LLMProviderConfig] = Field(default_factory=dict)
    routes: dict[str, str] = Field(default_factory=dict)
    fallback_providers: list[str] = Field(default_factory=list)
    group_daily_token_limit: int = Field(default=0, ge=0)
    group_daily_cost_limit: float = Field(default=0.0, ge=0, allow_inf_nan=False)
    max_concurrency: int = Field(default=6, ge=1, le=100)

    input_price: float = Field(default=0.0, ge=0, allow_inf_nan=False)
    output_price: float = Field(default=0.0, ge=0, allow_inf_nan=False)

    @field_validator("providers", mode="before")
    @classmethod
    def omit_removed_providers(cls, value):
        if isinstance(value, dict):
            return {name: provider for name, provider in value.items() if provider is not None}
        return value

    @model_validator(mode="after")
    def validate_fallbacks(self):
        unknown = set(self.fallback_providers) - set(self.provider_names())
        if unknown:
            raise ValueError("备用模型服务不存在：" + ", ".join(sorted(unknown)))
        self.fallback_providers = list(dict.fromkeys(self.fallback_providers))
        return self

    def provider_names(self) -> list[str]:
        return list(self.providers) or ["default"]

    def resolve(self, name: str | None = None) -> tuple[str, LLMProviderConfig]:
        """返回 ``(provider 名, 合并默认值后的配置)``。"""
        requested = (name or self.default or "default").strip() or "default"
        raw = self.providers.get(requested)
        if raw is None:
            # 名字不存在时退回第一个已配置的 provider，兼容旧配置或手误。
            if self.providers:
                requested = next(iter(self.providers))
                raw = self.providers[requested]
            else:
                raw = LLMProviderConfig()
        return requested, LLMProviderConfig(
            **{
                key: getattr(raw, key) if key in raw.model_fields_set else getattr(self, key)
                for key in ModelCapabilities.model_fields
            },
            base_url=raw.base_url or self.base_url,
            api_key=raw.api_key or (self.api_key if raw.inherit_api_key else ""),
            api_key_env=raw.api_key_env,
            model=raw.model or self.model,
            timeout=raw.timeout if raw.timeout is not None else self.timeout,
            max_tokens=raw.max_tokens if raw.max_tokens is not None else self.max_tokens,
            temperature=raw.temperature if raw.temperature is not None else self.temperature,
            no_stream=raw.no_stream,
            input_price=raw.input_price if "input_price" in raw.model_fields_set else self.input_price,
            output_price=raw.output_price if "output_price" in raw.model_fields_set else self.output_price,
            max_concurrency=raw.max_concurrency if "max_concurrency" in raw.model_fields_set else self.max_concurrency,
        )

    def route(self, *, scene: str | None = None, task: str | None = None) -> str:
        """按任务 / 场景路由到 provider 名，最后退回 ``default``。"""
        for key in (task, scene):
            if key:
                target = self.routes.get(key)
                if target:
                    return target
        return self.default or "default"


class ChatConfig(BaseModel):
    """What chat information may be included in outbound model messages."""

    send_nickname: bool = True
    send_user_id: bool = True
    send_role: bool = True
    send_time: bool = False
    allow_images: bool = False
    read_forward: bool = False
    group_memory: bool = True
    memory_session_gap_minutes: int = Field(default=30, ge=5, le=1440)
    memory_recent_messages: int = Field(default=200, ge=10, le=1000)
    memory_context_chars: int = Field(default=400000, ge=4000, le=1_000_000)
    reply_mode: Literal["auto", "auto_image", "forward", "image", "split"] = "auto_image"
    forward_threshold: int = Field(default=1200, ge=200, le=20000)
    reply_chunk_size: int = Field(default=0, ge=0, le=5000)
    reply_interval_ms: int = Field(default=350, ge=0, le=5000)
    quote_reply: bool = False
    at_sender: bool = False
    wake_word: str = Field(default="", max_length=80)
    wake_only_mode: Literal["prompt", "raw", "fixed", "ignore"] = "prompt"
    wake_only_prompt: str = Field(default="（用户只叫了你的名字，没有说具体内容，请简短自然地回应。）", min_length=1, max_length=2000)
    wake_only_replies: list[str] = Field(default_factory=list, max_length=50)

    @field_validator("wake_only_replies")
    @classmethod
    def _clean_wake_only_replies(cls, value: list[str]) -> list[str]:
        """去掉空行和首尾空白，只保留实际可发送的固定回复。"""
        return [item.strip() for item in value if item and item.strip()][:50]


class SandboxConfig(BaseModel):
    """智能体执行沙盒：能力开关、工作区围栏、联网范围与本轮额度。"""

    enabled: bool = True
    # 相对路径按项目根目录解析；工具只能读写这个目录内的文件。
    workspace: str = "data/agent_workspace"
    # shared：所有会话共享一个工作区；scene：按群/私聊自动分目录，避免跨群读取。
    workspace_scope: Literal["shared", "scene"] = "scene"
    allow_read: bool = True
    allow_write: bool = False
    allow_network: bool = True
    allow_network_write: bool = False
    allow_private_network: bool = False
    # 发送电子邮件会从绑定账号的邮箱发出；默认关闭，需管理员显式开启。
    allow_email: bool = False
    allow_qq_send: bool = True
    allow_destructive: bool = False
    # 截屏会暴露宿主桌面内容，默认关闭；开启后仍受超级管理员与明确请求约束。
    allow_screen: bool = False
    allow_process: bool = False
    destructive_requires_superuser: bool = True
    # 自动参与群聊时，写入/管理类操作必须由群友明确请求。
    write_requires_addressed: bool = True
    allowed_hosts: list[str] = Field(default_factory=list)
    denied_hosts: list[str] = Field(default_factory=list)
    deny_paths: list[str] = Field(default_factory=lambda: [
        ".env", ".env.*", "config.toml", "data/admin_token.txt", "data/fllbot.db",
        "data/backups", "data/backups/*", "logs", "logs/*", ".git", ".git/*",
        "*.pem", "*.key", "*.pfx",
    ])
    max_read_bytes: int = Field(default=200_000, ge=1_000, le=20_000_000)
    max_write_bytes: int = Field(default=200_000, ge=1_000, le=20_000_000)
    # 发送/复制工作区文件的上限；不受单次文本读取上限限制。
    max_file_bytes: int = Field(default=5_000_000, ge=1_000, le=50_000_000)
    max_fetch_bytes: int = Field(default=2_000_000, ge=1_000, le=50_000_000)
    request_timeout: float = Field(default=10.0, gt=0, le=120)
    max_redirects: int = Field(default=3, ge=0, le=10)
    max_calls_per_turn: int = Field(default=0, ge=0, le=200)
    max_write_calls_per_turn: int = Field(default=4, ge=0, le=100)


class RetentionConfig(BaseModel):
    enabled: bool = False
    interval_seconds: int = Field(default=3600, ge=60, le=86400)
    messages_days: int = Field(default=0, ge=0, le=3650)
    conversations_days: int = Field(default=0, ge=0, le=3650)
    usage_days: int = Field(default=0, ge=0, le=3650)
    changes_days: int = Field(default=0, ge=0, le=3650)


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
    chat: ChatConfig = Field(default_factory=ChatConfig)
    sandbox: SandboxConfig = Field(default_factory=SandboxConfig)
    retention: RetentionConfig = Field(default_factory=RetentionConfig)


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


def mask_secrets(config: AppConfig | dict[str, Any]) -> dict[str, Any]:
    """导出配置用于 API 展示，隐藏密钥。"""
    data = config.model_dump() if isinstance(config, AppConfig) else copy.deepcopy(config)
    llm = data.get("llm", {})
    if "api_key" in llm:
        llm["api_key"] = _mask(llm["api_key"])
    for provider in llm.get("providers", {}).values():
        if isinstance(provider, dict) and "api_key" in provider:
            provider["api_key"] = _mask(provider["api_key"])
    for section, key in (("admin", "token"), ("server", "access_token")):
        if key in data.get(section, {}):
            data[section][key] = _mask(data[section][key])
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
        self._startup_values: dict[str, Any] | None = None

    def freeze_startup_values(self) -> dict[str, Any]:
        """Pin values used to construct the instance; later writes cannot change them."""
        values = self.current.model_dump()
        self._startup_values = {}
        for path in STARTUP_FIELDS:
            section, key = path.split('.')
            value = copy.deepcopy(values[section][key])
            self._startup_values[path] = value
            self._base.setdefault(section, {})[key] = value
        self._overrides, _ = split_startup_overrides(self._overrides)
        return copy.deepcopy(self._startup_values)

    def patch(self, overrides: dict[str, Any]) -> AppConfig:
        """合并覆盖项并重新校验；失败时抛出 ``ValidationError`` 且不生效。"""
        config, merged_overrides = self.prepare_patch(overrides)
        self._overrides = merged_overrides
        self.current = config
        return config

    def prepare_patch(self, overrides: dict[str, Any]) -> tuple[AppConfig, dict[str, Any]]:
        """Validate a candidate without publishing it before persistence succeeds."""
        merged_overrides = deep_merge(self._overrides, overrides)
        data = deep_merge(self._base, merged_overrides)
        config = AppConfig.model_validate(data)
        if self._startup_values is not None:
            current = config.model_dump()
            changed = sorted(path for path, value in self._startup_values.items()
                             if current[path.split('.')[0]][path.split('.')[1]] != value)
            if changed:
                raise StartupConfigError(changed)
            merged_overrides, _ = split_startup_overrides(merged_overrides)
        return config, merged_overrides

    def restore(self, overrides: dict[str, Any]) -> AppConfig:
        """启动时从数据库恢复运行时覆盖。"""
        self._overrides = {}
        return self.patch(overrides) if overrides else self.current

    @property
    def overrides(self) -> dict[str, Any]:
        return copy.deepcopy(self._overrides)

"""日志：控制台 + 滚动 JSONL 文件 + 内存环形缓冲（供 /api/logs 查询）。"""

from __future__ import annotations

import json
import logging
import sys
from collections import deque
from collections.abc import Iterable
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

DEFAULT_FORMAT = "%(asctime)s %(levelname)-5s %(name)s: %(message)s"
DATE_FORMAT = "%H:%M:%S"

_STANDARD_ATTRS = {
    "name",
    "msg",
    "args",
    "levelname",
    "levelno",
    "pathname",
    "filename",
    "module",
    "exc_info",
    "exc_text",
    "stack_info",
    "lineno",
    "funcName",
    "created",
    "msecs",
    "relativeCreated",
    "thread",
    "threadName",
    "processName",
    "process",
    "taskName",
    "message",
    "asctime",
}

_ring: deque[dict[str, Any]] = deque(maxlen=500)
_configured = False


def _extras(record: logging.LogRecord) -> dict[str, Any]:
    return {
        key: value
        for key, value in record.__dict__.items()
        if key not in _STANDARD_ATTRS and not key.startswith("_")
    }


def record_to_dict(record: logging.LogRecord) -> dict[str, Any]:
    """把日志记录转成可 JSON 序列化的字典。"""
    timestamp = datetime.fromtimestamp(record.created).astimezone().isoformat(timespec="milliseconds")
    payload: dict[str, Any] = {
        "ts": timestamp,
        "level": record.levelname,
        "logger": record.name,
        "message": record.getMessage(),
    }
    extras = _extras(record)
    if extras:
        payload["extra"] = extras
    if record.exc_info:
        payload["exc"] = logging.Formatter().formatException(record.exc_info)
    return payload


class JsonFormatter(logging.Formatter):
    """单行 JSON，便于机器读取与日志检索。"""

    def format(self, record: logging.LogRecord) -> str:
        return json.dumps(record_to_dict(record), ensure_ascii=False)


class RingBufferHandler(logging.Handler):
    """把最近的日志留在内存里。"""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            _ring.append(record_to_dict(record))
        except Exception:  # pragma: no cover - 日志不应反过来炸掉主流程
            self.handleError(record)


class _SafeFormatter(logging.Formatter):
    """带异常的普通文本格式。"""


def configure_logging(
    *,
    level: str = "INFO",
    log_dir: Path | str | None = None,
    json_mode: bool = True,
    filename: str = "fllbot.log",
    ring_size: int = 500,
    force: bool = False,
) -> None:
    """配置根日志器，并让 uvicorn 的日志汇入同一套输出。"""
    global _configured, _ring
    if _configured and not force:
        return
    _ring = deque(maxlen=max(1, ring_size))

    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()

    numeric_level = getattr(logging, str(level).upper(), logging.INFO)
    root.setLevel(numeric_level)

    console = logging.StreamHandler(stream=sys.stderr)
    console.setFormatter(_SafeFormatter(DEFAULT_FORMAT, datefmt=DATE_FORMAT))
    root.addHandler(console)

    if log_dir is not None:
        directory = Path(log_dir)
        directory.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            directory / filename,
            maxBytes=5 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        file_handler.setFormatter(JsonFormatter() if json_mode else _SafeFormatter(DEFAULT_FORMAT, datefmt=DATE_FORMAT))
        root.addHandler(file_handler)

    ring = RingBufferHandler()
    ring.setLevel(max(numeric_level, logging.INFO))
    root.addHandler(ring)

    # uvicorn 自己会挂 handler，这里统一改为向上传递，避免重复输出。
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    _configured = True


def get_ring(
    limit: int = 200,
    *,
    level: str | None = None,
    since: str | None = None,
) -> list[dict[str, Any]]:
    """按时间顺序返回内存中的日志。"""
    items: Iterable[dict[str, Any]] = list(_ring)
    if level:
        wanted = str(level).upper()
        order = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}
        threshold = order.get(wanted, 0)
        items = [item for item in items if order.get(str(item.get("level")).upper(), 0) >= threshold]
    if since:
        items = [item for item in items if str(item.get("ts", "")) > since]
    result = list(items)
    return result[-limit:] if limit else result


def clear_ring() -> None:
    _ring.clear()

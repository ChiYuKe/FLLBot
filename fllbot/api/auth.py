"""管理 API 鉴权。"""

from __future__ import annotations

import hmac
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from fastapi import HTTPException, Request

if TYPE_CHECKING:  # pragma: no cover
    from ..app import FLLBot


def build_admin_dependency(bot: FLLBot) -> Callable[[Request], Awaitable[None]]:
    """令牌校验依赖：支持 ``Authorization: Bearer``、``X-Admin-Token`` 与 ``?token=``。"""

    async def dependency(request: Request) -> None:
        expected = bot.admin_token
        if not expected:
            return

        offered = request.headers.get("x-admin-token", "")
        if not offered:
            header = request.headers.get("authorization", "")
            if header.lower().startswith("bearer "):
                offered = header[7:].strip()
        if not offered:
            offered = request.query_params.get("token") or request.query_params.get("access_token") or ""

        if not hmac.compare_digest(str(offered), str(expected)):
            raise HTTPException(
                status_code=401,
                detail="管理令牌无效",
                headers={"WWW-Authenticate": "Bearer"},
            )

    return dependency


__all__ = ["build_admin_dependency"]

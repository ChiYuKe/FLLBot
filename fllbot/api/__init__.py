"""管理 API 包。"""

from __future__ import annotations

from .auth import build_admin_dependency
from .routes import create_api_router

__all__ = ["build_admin_dependency", "create_api_router"]

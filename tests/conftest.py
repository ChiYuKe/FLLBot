"""pytest 公共夹具。"""

from __future__ import annotations

from pathlib import Path

import pytest

from helpers import make_bot


@pytest.fixture
async def bot(tmp_path: Path):
    """带内置插件的机器人。"""
    instance = await make_bot(tmp_path, builtin=True)
    try:
        yield instance
    finally:
        await instance.stop()


@pytest.fixture
async def bare_bot(tmp_path: Path):
    """只有用户插件目录、不含内置插件的机器人。"""
    instance = await make_bot(tmp_path, builtin=False)
    try:
        yield instance
    finally:
        await instance.stop()

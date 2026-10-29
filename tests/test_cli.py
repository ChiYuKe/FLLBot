"""CLI 辅助函数测试。"""

from __future__ import annotations

import socket

from fllbot.cli import port_available


def test_busy_port_unavailable() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        assert port_available("127.0.0.1", port) is False


def test_free_port_available() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    assert port_available("127.0.0.1", port) is True


def test_zero_port_invalid() -> None:
    assert port_available("127.0.0.1", 0) is False

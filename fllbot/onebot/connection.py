"""反向 WebSocket 连接：收发 JSON、echo 关联、事件回调。"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from starlette.websockets import WebSocket, WebSocketDisconnect

from .segments import MessageSegment, as_segments


class ActionError(RuntimeError):
    """OneBot API 调用失败（协议层返回失败或超时）。"""

    def __init__(self, action: str, *, retcode: int | None = None, message: str = "") -> None:
        self.action = action
        self.retcode = retcode
        self.message = message or "调用失败"
        detail = f"action={action}"
        if retcode is not None:
            detail += f" retcode={retcode}"
        if self.message:
            detail += f" msg={self.message}"
        super().__init__(detail)


EventCallback = Callable[["OneBotConnection", dict[str, Any]], Awaitable[None]]


class OneBotConnection:
    """一条来自 NapCat 的反向 WS 连接。"""

    def __init__(
        self,
        websocket: WebSocket,
        *,
        on_event: EventCallback,
        self_id: int | None = None,
        client_role: str = "",
        client_name: str = "",
        call_timeout: float = 30.0,
        logger: logging.Logger | None = None,
    ) -> None:
        self.ws = websocket
        self.self_id = int(self_id or 0)
        self.nickname = ""
        self.client_role = client_role
        self.client_name = client_name
        self.call_timeout = call_timeout
        self.logger = logger or logging.getLogger("fllbot.onebot")

        self._on_event = on_event
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._send_lock = asyncio.Lock()
        self._closed = False

        self.connected_at = time.time()
        self.last_seen_wall = time.time()
        self.last_seen_monotonic = time.monotonic()
        self.events_received = 0
        self.actions_sent = 0
        self.errors = 0

    # -- 状态 ------------------------------------------------------------- #
    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def online(self) -> bool:
        return not self._closed

    @property
    def key(self) -> str:
        return str(self.self_id) if self.self_id else f"anon-{id(self):x}"

    @property
    def label(self) -> str:
        return f"{self.self_id}" if self.self_id else f"未识别({self.client_role or 'unknown'})"

    def touch(self) -> None:
        self.last_seen_wall = time.time()
        self.last_seen_monotonic = time.monotonic()

    def idle_seconds(self) -> float:
        return max(0.0, time.monotonic() - self.last_seen_monotonic)

    def summary(self) -> dict[str, Any]:
        from datetime import datetime

        def iso(value: float) -> str:
            return datetime.fromtimestamp(value).astimezone().isoformat(timespec="seconds")

        return {
            "self_id": self.self_id or None,
            "nickname": self.nickname,
            "online": self.online,
            "connected_at": iso(self.connected_at),
            "last_seen": iso(self.last_seen_wall),
            "idle_seconds": round(self.idle_seconds(), 1),
            "events_received": self.events_received,
            "actions_sent": self.actions_sent,
            "errors": self.errors,
            "client_role": self.client_role,
            "client_name": self.client_name,
        }

    # -- 收发 ------------------------------------------------------------- #
    async def send_json(self, payload: dict[str, Any]) -> None:
        if self._closed:
            raise ConnectionError("连接已关闭")
        text = json.dumps(payload, ensure_ascii=False)
        async with self._send_lock:
            try:
                await self.ws.send_text(text)
            except Exception as exc:  # pragma: no cover - 网络异常
                self._closed = True
                self.errors += 1
                raise ConnectionError(f"发送失败: {exc}") from exc

    async def call(self, action: str, params: dict[str, Any] | None = None, *, timeout: float | None = None) -> Any:
        """调用 OneBot API 并等待同 echo 的响应，返回 ``data`` 字段。"""
        if self._closed:
            raise ActionError(action, message="连接已关闭")

        echo = uuid.uuid4().hex
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending[echo] = future
        self.actions_sent += 1
        try:
            await self.send_json({"action": action, "params": params or {}, "echo": echo})
            response = await asyncio.wait_for(future, timeout or self.call_timeout)
        except TimeoutError as exc:
            raise ActionError(action, message="调用超时") from exc
        except asyncio.CancelledError:
            raise
        finally:
            self._pending.pop(echo, None)

        status = str(response.get("status") or "")
        retcode = response.get("retcode")
        if status == "failed" or (retcode is not None and retcode not in (0, 1)):
            self.errors += 1
            raise ActionError(
                action,
                retcode=retcode,
                message=str(response.get("msg") or response.get("wording") or ""),
            )
        data = response.get("data")
        return data if data is not None else response

    async def read_loop(self) -> None:
        """持续读取 NapCat 上报，直到连接断开。"""
        while not self._closed:
            try:
                raw = await self.ws.receive_text()
            except WebSocketDisconnect:
                break
            except RuntimeError:  # 连接已被关闭
                break
            except Exception as exc:  # pragma: no cover - 异常断开
                self.logger.debug("接收循环异常结束: %s", exc)
                break

            self.touch()
            if not raw:
                continue
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                self.logger.warning("收到非法 JSON（前 200 字符）: %s", raw[:200])
                continue
            if not isinstance(payload, dict):
                continue
            try:
                await self._handle_payload(payload)
            except Exception:  # pragma: no cover - 单条事件不应拖垮连接
                self.logger.exception("处理上报数据失败")

    async def _handle_payload(self, payload: dict[str, Any]) -> None:
        echo = payload.get("echo")
        if echo is not None:
            future = self._pending.get(str(echo))
            if future is not None and not future.done():
                future.set_result(payload)
                return
            # 迟到或未知的响应，忽略即可。

        if "post_type" not in payload:
            self.logger.debug("忽略非事件数据: %s", str(payload)[:200])
            return

        self.events_received += 1
        self.touch()
        self_id = payload.get("self_id")
        if not self.self_id and self_id:
            self.self_id = int(self_id)
            self.logger.info("连接识别为账号 %s", self.self_id)
        await self._on_event(self, payload)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self._closed = True
        for future in list(self._pending.values()):
            if not future.done():
                future.set_exception(ConnectionError("连接已关闭"))
        self._pending.clear()
        try:
            await self.ws.close(code=code, reason=reason)
        except Exception:  # pragma: no cover
            pass

    # -- 便捷 API --------------------------------------------------------- #
    @staticmethod
    def _message_payload(message: str | list[MessageSegment]) -> list[dict[str, Any]]:
        return [segment.to_dict() for segment in as_segments(message)]

    async def send_group_msg(self, group_id: int, message: str | list[MessageSegment], **extra: Any) -> Any:
        params: dict[str, Any] = {"group_id": int(group_id), "message": self._message_payload(message)}
        params.update(extra)
        return await self.call("send_group_msg", params)

    async def send_private_msg(self, user_id: int, message: str | list[MessageSegment], **extra: Any) -> Any:
        params: dict[str, Any] = {"user_id": int(user_id), "message": self._message_payload(message)}
        params.update(extra)
        return await self.call("send_private_msg", params)

    async def send_msg(
        self,
        message: str | list[MessageSegment],
        *,
        group_id: int | None = None,
        user_id: int | None = None,
        **extra: Any,
    ) -> Any:
        if group_id:
            return await self.send_group_msg(group_id, message, **extra)
        if user_id:
            return await self.send_private_msg(user_id, message, **extra)
        raise ValueError("send_msg 需要 group_id 或 user_id")

    async def delete_msg(self, message_id: int) -> Any:
        return await self.call("delete_msg", {"message_id": int(message_id)})

    async def get_login_info(self) -> Any:
        return await self.call("get_login_info")

    async def get_group_list(self) -> Any:
        return await self.call("get_group_list")

    async def get_group_info(self, group_id: int) -> Any:
        return await self.call("get_group_info", {"group_id": int(group_id)})

    async def get_group_member_info(self, group_id: int, user_id: int, *, no_cache: bool = False) -> Any:
        return await self.call(
            "get_group_member_info",
            {"group_id": int(group_id), "user_id": int(user_id), "no_cache": bool(no_cache)},
        )

    async def get_friend_list(self) -> Any:
        return await self.call("get_friend_list")

    async def set_group_ban(self, group_id: int, user_id: int, duration: int = 600) -> Any:
        return await self.call(
            "set_group_ban",
            {"group_id": int(group_id), "user_id": int(user_id), "duration": int(duration)},
        )

    async def set_group_whole_ban(self, group_id: int, enable: bool = True) -> Any:
        return await self.call("set_group_whole_ban", {"group_id": int(group_id), "enable": bool(enable)})

    async def set_group_kick(self, group_id: int, user_id: int, *, reject_add_request: bool = False) -> Any:
        return await self.call(
            "set_group_kick",
            {"group_id": int(group_id), "user_id": int(user_id), "reject_add_request": bool(reject_add_request)},
        )

    async def set_group_card(self, group_id: int, user_id: int, card: str = "") -> Any:
        return await self.call("set_group_card", {"group_id": int(group_id), "user_id": int(user_id), "card": card})

    async def set_friend_add_request(self, flag: str, approve: bool = True, remark: str = "") -> Any:
        return await self.call(
            "set_friend_add_request", {"flag": flag, "approve": bool(approve), "remark": remark}
        )

    async def set_group_add_request(self, flag: str, sub_type: str, approve: bool = True, reason: str = "") -> Any:
        return await self.call(
            "set_group_add_request",
            {"flag": flag, "sub_type": sub_type, "approve": bool(approve), "reason": reason},
        )

    async def get_msg(self, message_id: int) -> Any:
        return await self.call("get_msg", {"message_id": int(message_id)})

    async def get_status(self) -> Any:
        return await self.call("get_status")

    async def get_version_info(self) -> Any:
        return await self.call("get_version_info")

    async def send_private_forward_msg(self, user_id: int, messages: list[dict[str, Any]]) -> Any:
        return await self.call("send_private_forward_msg", {"user_id": int(user_id), "messages": messages})

    async def send_group_forward_msg(self, group_id: int, messages: list[dict[str, Any]]) -> Any:
        return await self.call("send_group_forward_msg", {"group_id": int(group_id), "messages": messages})


    # -- QQ 交互与 NapCat 扩展 -------------------------------------------- #

    async def get_group_member_list(self, group_id: int, *, no_cache: bool = False) -> Any:
        """获取完整群成员列表。"""
        return await self.call("get_group_member_list", {"group_id": int(group_id), "no_cache": bool(no_cache)})

    async def get_stranger_info(self, user_id: int, *, no_cache: bool = False) -> Any:
        """获取用户资料。"""
        return await self.call("get_stranger_info", {"user_id": int(user_id), "no_cache": bool(no_cache)})

    async def group_poke(self, group_id: int, user_id: int) -> Any:
        """在群里戳一戳。"""
        return await self.call("group_poke", {"group_id": int(group_id), "user_id": int(user_id)})

    async def friend_poke(self, user_id: int) -> Any:
        """戳一戳好友。"""
        return await self.call("friend_poke", {"user_id": int(user_id)})

    async def send_like(self, user_id: int, times: int = 1) -> Any:
        """为好友资料点赞。"""
        return await self.call("send_like", {"user_id": int(user_id), "times": int(times)})

    async def set_friend_remark(self, user_id: int, remark: str) -> Any:
        """设置好友备注。"""
        return await self.call("set_friend_remark", {"user_id": int(user_id), "remark": str(remark)})

    async def set_group_admin(self, group_id: int, user_id: int, enable: bool = True) -> Any:
        """设置或取消群管理员。"""
        return await self.call("set_group_admin", {"group_id": int(group_id), "user_id": int(user_id), "enable": bool(enable)})

    async def set_group_name(self, group_id: int, group_name: str) -> Any:
        """修改群名称。"""
        return await self.call("set_group_name", {"group_id": int(group_id), "group_name": str(group_name)})

    async def set_group_leave(self, group_id: int, *, is_dismiss: bool = False) -> Any:
        """退出群；群主可选择解散。"""
        return await self.call("set_group_leave", {"group_id": int(group_id), "is_dismiss": bool(is_dismiss)})

    async def set_group_special_title(self, group_id: int, user_id: int, special_title: str) -> Any:
        """设置群成员专属头衔。"""
        return await self.call("set_group_special_title", {"group_id": int(group_id), "user_id": int(user_id), "special_title": str(special_title)})

    async def set_group_portrait(self, group_id: int, file: str, *, cache: int = 1) -> Any:
        """设置群头像。"""
        return await self.call("set_group_portrait", {"group_id": int(group_id), "file": str(file), "cache": int(cache)})

    async def get_group_honor_info(self, group_id: int, type: str = "all") -> Any:
        """获取群荣誉。"""
        return await self.call("get_group_honor_info", {"group_id": int(group_id), "type": str(type)})

    async def get_group_at_all_remain(self, group_id: int) -> Any:
        """查询 @全体剩余次数。"""
        return await self.call("get_group_at_all_remain", {"group_id": int(group_id)})

    async def get_essence_msg_list(self, group_id: int) -> Any:
        """获取群精华消息。"""
        return await self.call("get_essence_msg_list", {"group_id": int(group_id)})

    async def set_essence_msg(self, message_id: int) -> Any:
        """设为精华消息。"""
        return await self.call("set_essence_msg", {"message_id": int(message_id)})

    async def delete_essence_msg(self, message_id: int) -> Any:
        """移除精华消息。"""
        return await self.call("delete_essence_msg", {"message_id": int(message_id)})

    async def get_group_notice(self, group_id: int) -> Any:
        """获取群公告。"""
        return await self.call("_get_group_notice", {"group_id": int(group_id)})

    async def delete_group_notice(self, group_id: int, notice_id: str) -> Any:
        """删除群公告。"""
        return await self.call("_del_group_notice", {"group_id": int(group_id), "notice_id": str(notice_id)})

    async def get_group_file_system_info(self, group_id: int) -> Any:
        """获取群文件系统信息。"""
        return await self.call("get_group_file_system_info", {"group_id": int(group_id)})

    async def get_group_root_files(self, group_id: int) -> Any:
        """获取群根目录文件。"""
        return await self.call("get_group_root_files", {"group_id": int(group_id)})

    async def get_group_files_by_folder(self, group_id: int, folder_id: str) -> Any:
        """获取群文件夹内容。"""
        return await self.call("get_group_files_by_folder", {"group_id": int(group_id), "folder_id": str(folder_id)})

    async def get_group_file_url(self, group_id: int, file_id: str, busid: int) -> Any:
        """获取群文件下载链接。"""
        return await self.call("get_group_file_url", {"group_id": int(group_id), "file_id": str(file_id), "busid": int(busid)})

    async def delete_group_file(self, group_id: int, file_id: str, busid: int) -> Any:
        """删除群文件。"""
        return await self.call("delete_group_file", {"group_id": int(group_id), "file_id": str(file_id), "busid": int(busid)})

    async def create_group_file_folder(self, group_id: int, name: str) -> Any:
        """创建群文件夹。"""
        return await self.call("create_group_file_folder", {"group_id": int(group_id), "name": str(name)})

    async def delete_group_folder(self, group_id: int, folder_id: str) -> Any:
        """删除群文件夹。"""
        return await self.call("delete_group_folder", {"group_id": int(group_id), "folder_id": str(folder_id)})

    async def move_group_file(self, group_id: int, file_id: str, target_dir: str) -> Any:
        """移动群文件。"""
        return await self.call("move_group_file", {"group_id": int(group_id), "file_id": str(file_id), "target_dir": str(target_dir)})

    async def trans_group_file(self, group_id: int, file_id: str, target_group_id: int) -> Any:
        """转发群文件到其他群。"""
        return await self.call("trans_group_file", {"group_id": int(group_id), "file_id": str(file_id), "target_group_id": int(target_group_id)})

    async def rename_group_file(self, group_id: int, file_id: str, current_parent_directory: str, new_name: str) -> Any:
        """重命名群文件。"""
        return await self.call("rename_group_file", {"group_id": int(group_id), "file_id": str(file_id), "current_parent_directory": str(current_parent_directory), "new_name": str(new_name)})

    async def upload_private_file(self, user_id: int, file: str, name: str) -> Any:
        """上传私聊文件，路径基于 NapCat 所在机器。"""
        return await self.call("upload_private_file", {"user_id": int(user_id), "file": str(file), "name": str(name)})

    async def get_image(self, file: str) -> Any:
        """获取图片信息。"""
        return await self.call("get_image", {"file": str(file)})

    async def get_record(self, file: str, out_format: str = "mp3") -> Any:
        """获取或转换语音。"""
        return await self.call("get_record", {"file": str(file), "out_format": str(out_format)})

    async def can_send_image(self) -> Any:
        """检查图片发送能力。"""
        return await self.call("can_send_image", {})

    async def can_send_record(self) -> Any:
        """检查语音发送能力。"""
        return await self.call("can_send_record", {})

    async def get_forward_msg(self, message_id: str) -> Any:
        """读取合并转发消息。"""
        return await self.call("get_forward_msg", {"message_id": str(message_id)})

    async def send_poke(self, user_id: int, *, group_id: int | None = None) -> Any:
        """按目标会话选择群戳一戳或好友戳一戳。"""
        if group_id is not None:
            return await self.group_poke(group_id, user_id)
        return await self.friend_poke(user_id)

    async def send_group_notice(self, group_id: int, content: str, *, image: str | None = None) -> Any:
        """发布群公告，可附带图片。"""
        params: dict[str, Any] = {"group_id": int(group_id), "content": content}
        if image is not None:
            params["image"] = image
        return await self.call("_send_group_notice", params)

    async def upload_group_file(self, group_id: int, file: str, name: str, *, folder: str | None = None) -> Any:
        """上传群文件；本地路径基于 NapCat 所在机器。"""
        params: dict[str, Any] = {"group_id": int(group_id), "file": file, "name": name}
        if folder is not None:
            params["folder"] = folder
        return await self.call("upload_group_file", params)


__all__ = ["ActionError", "EventCallback", "OneBotConnection"]

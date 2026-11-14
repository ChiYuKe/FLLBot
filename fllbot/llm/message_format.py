"""Pure sender labels shared by context assembly and legacy plugin imports."""

from ..onebot.segments import MessageSegment

_GROUP_ROLE_LABELS = {"owner": "群主", "admin": "管理员", "member": "成员"}


def mention_targets(segments: list[MessageSegment]) -> list[str]:
    targets = (str(segment.get("qq") or "") for segment in segments if segment.type == "at")
    return list(dict.fromkeys(target for target in targets if target == "all" or target.isdecimal()))


def with_group_mentions(text: str, targets: list[str], *, self_id: int, send_user_id: bool) -> str:
    """在本条消息开头标注真实 @ 的对象，便于判断“刚才 @ 过谁”。

    标记放在正文前面而不是结尾：回复正文仍以用户原文结束，引用、追加提醒和
    依赖 ``endswith`` 的旧逻辑都不受标记影响。
    """
    labels = []
    for target in targets:
        if target == str(self_id):
            labels.append("机器人自身")
        elif target == "all":
            labels.append("全体成员")
        else:
            labels.append(f"QQ {target}" if send_user_id else "其他成员")
    return f"[QQ @ 目标：{'、'.join(dict.fromkeys(labels))}]\n{text}" if labels else text


def speaker_label(user_id: int | None, nickname: str | None, sender_role: str | None = None) -> str:
    """群聊说话人标签：``昵称(QQ号，身份)``；没有可用信息时返回空串。"""
    name = (nickname or "").strip()
    uid = int(user_id or 0)
    if not name and uid:
        name = f"QQ {uid}"
    if not name:
        return ""
    tags = [str(uid)] if uid else []
    role_label = _GROUP_ROLE_LABELS.get(str(sender_role or "").strip().lower())
    if role_label:
        tags.append(role_label)
    return f"{name}({'，'.join(tags)})" if tags else name


def group_user_content(
    user_id: int | None,
    nickname: str | None,
    sender_role: str | None,
    content: str,
) -> str:
    """给群聊用户消息加上发送者标签，便于模型区分说话人。"""
    label = speaker_label(user_id, nickname, sender_role)
    return f"{label}：{content}" if label else content

"""Pure sender labels shared by context assembly and legacy plugin imports."""

_GROUP_ROLE_LABELS = {"owner": "群主", "admin": "管理员", "member": "成员"}


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

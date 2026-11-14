# Plugin: email | Role: QQ mailbox delivery tool | Metadata: plugin.toml
"""通过 QQ 邮箱 SMTP 给群友发送纯文本邮件。

群友的邮箱通常就是 ``QQ号@qq.com``，所以工具支持直接填 QQ 号；留空收件人时默认发给
当前发言人自己的 QQ 邮箱。邮件只能由群友明确请求，自动参与会被宿主沙盒拒绝。
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from fllbot.plugin import Context, Plugin

from .service import (
    EmailError,
    normalize_domains,
    parse_body,
    parse_recipients,
    parse_subject,
    send_email,
    validate_config,
)

plugin = Plugin.from_manifest(__file__)

plugin.setting(
    "sender",
    type="string",
    label="发件邮箱",
    default="",
    description="用于发信的 QQ 邮箱，例如 123456789@qq.com；需要先在 QQ 邮箱设置里开启 SMTP 服务。",
)
plugin.setting(
    "auth_code",
    type="string",
    label="SMTP 授权码",
    default="",
    secret=True,
    description="QQ 邮箱设置 → 账户 → 开启 SMTP 服务后生成的授权码，不是 QQ 登录密码。",
)
plugin.setting(
    "smtp_host",
    type="select",
    label="SMTP 服务器",
    default="smtp.qq.com",
    options=("smtp.qq.com", "smtp.exmail.qq.com"),
    description="个人 QQ 邮箱用 smtp.qq.com；QQ 企业邮箱用 smtp.exmail.qq.com。",
)
plugin.setting(
    "smtp_port",
    type="int",
    label="SMTP 端口",
    default=465,
    min=1,
    max=65535,
    description="SSL 常用 465；STARTTLS 常用 587。",
)
plugin.setting(
    "security",
    type="select",
    label="加密方式",
    default="ssl",
    options=("ssl", "starttls"),
    description="QQ 邮箱推荐 ssl + 465；用 587 时选择 starttls。",
)
plugin.setting(
    "timeout",
    type="float",
    label="发送超时（秒）",
    default=15.0,
    min=5.0,
    max=30.0,
    description="连接和发送共用这个超时；上限低于工具超时，避免超时后邮件状态不确定。",
)
plugin.setting(
    "recipient_domains",
    type="list",
    label="允许的收件域名",
    default=["qq.com"],
    description="默认只允许 QQ 邮箱；填写多个时用逗号分隔。QQ 号会自动转换成 QQ号@qq.com。",
)
plugin.setting(
    "max_recipients",
    type="int",
    label="单封最多收件人",
    default=5,
    min=1,
    max=20,
    description="一次工具调用最多发送给几个收件人。",
)
plugin.setting(
    "max_body_chars",
    type="int",
    label="正文最大字数",
    default=5000,
    min=100,
    max=20000,
    description="超过上限时拒绝发送，避免把超长内容塞进邮件。",
)
plugin.agent_instruction(
    "send_email",
    tools=["send_email"],
    text=(
        "只有群友明确要求发邮件，并给出收件人和主题/正文时才调用 send_email。"
        "“发给我/发到我的邮箱”必须把 to 留空，插件会自动发给当前发言人，不能凭聊天记录猜别人的 QQ。"
        "发给别人时，to 可填写当前消息明确写出的 QQ 号、邮箱，或当前消息实际 @ 的其他成员的 QQ 号。"
        "如果用户用历史消息指代收件人，可以列出辨认出的昵称，请发起者在发邮件的消息里直接 @ 收件人。"
        "邮件只能是纯文本，不能带附件；不要主动发邮件，也不要在自动参与、群友互聊或含糊请求时调用。"
        "如果返回 email_recipient_not_requested，请发起者直接 @ 收件人，只有明确发给自己时才可留空 to。"
        "绝不能换一个 QQ 号重试。如果返回 email_request_missing，说明当前消息没有明确要求发邮件，"
        "不要凭上一条消息补发。发送成功后只说明已发送给谁，不要重复正文；"
        "被沙盒拒绝或未配置邮箱时如实说明，不要换工具绕过。"
    ),
)


_EMAIL_INTENT_RE = re.compile(r"(?:邮箱|邮件|email|e-?mail|inbox)", re.IGNORECASE)
_EMAIL_ADDRESS_RE = re.compile(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def _has_email_intent(ctx: Context) -> bool:
    """当前消息必须明确提到邮箱/邮件，或直接给出完整邮箱地址。

    模型有时会把上一条请求带进当前回复；只靠“明确 @”无法区分“发到我的邮箱”和
    随后的“妈妈”。这里用确定性关键词再收一道口，避免上下文串台时发出邮件。
    """
    text = f"{getattr(ctx, 'text', '') or ''}\n{getattr(ctx, 'raw_text', '') or ''}"
    return bool(_EMAIL_INTENT_RE.search(text) or _EMAIL_ADDRESS_RE.search(text))


def _recipient_evidence(ctx: Context) -> tuple[str, set[str]]:
    """收集当前消息里可以证明“收件人是用户自己说的”的证据。

    只读取当前消息正文与 @ 段，不读取历史、引用或成员资料，避免模型把上下文里
    其他人的 QQ 号当成这次请求的收件人。
    """
    parts = [str(getattr(ctx, "text", "") or ""), str(getattr(ctx, "raw_text", "") or "")]
    qq_numbers: set[str] = set()
    for segment in getattr(ctx, "message", []) or []:
        if getattr(segment, "type", "") == "at":
            value = str(segment.get("qq") or "").strip()
            if value.isdigit() and value != str(getattr(ctx, "self_id", 0)):
                qq_numbers.add(value)
    own = str(getattr(ctx, "user_id", 0) or 0)
    if own and own != "0":
        qq_numbers.add(own)
    return "\n".join(parts), qq_numbers


def _recipient_requested(address: str, text: str, allowed_qqs: set[str]) -> bool:
    local, _, domain = address.partition("@")
    if domain.casefold() == "qq.com" and local.isdigit():
        if local in allowed_qqs:
            return True
        return re.search(rf"(?<![0-9]){re.escape(local)}(?![0-9])", text) is not None
    return address.casefold() in text.casefold()


@plugin.on_ai_tool(
    "send_email",
    title="发送邮件",
    effect="side_effecting",
    capabilities=("email.send",),
    risk="write",
    timeout=45,
    max_output_chars=2000,
    description=(
        "通过已配置的 QQ 邮箱发送纯文本邮件。to 可以填 QQ 号（自动补 @qq.com）或完整 QQ 邮箱，"
        "多个收件人用逗号、空格或分号分隔；留空则发送到当前发言人自己的 QQ 邮箱。"
        "to 只接受当前消息写出的 QQ 号/邮箱，或实际 @ 的其他成员的 QQ 号；发给当前发言人时留空。"
        "必须由群友明确要求发邮件并提供主题与正文；不要主动发送，不知道对方 QQ 号时先询问，"
        "也不要把邮件当成群聊回复的替代品。"
        "被沙盒拒绝（email_disabled / automatic_side_effect）或未配置授权码时如实说明限制。"
        "如果返回 email_request_missing，说明当前消息没有明确要求发邮件，不要补发；"
        "如果返回 email_recipient_not_requested，请发起者在发邮件的消息里直接 @ 收件人。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "subject": {
                "type": "string",
                "maxLength": 200,
                "description": "邮件主题，不能包含换行",
            },
            "body": {
                "type": "string",
                "maxLength": 20000,
                "description": "邮件正文，纯文本；保留换行",
            },
            "to": {
                "type": "string",
                "maxLength": 500,
                "description": (
                    "收件人 QQ 号或 QQ 邮箱，多个用逗号/空格/分号分隔；"
                    "可填当前消息明确写出的地址或实际 @ 的其他成员的 QQ 号；留空发给当前发言人"
                ),
            },
        },
        "required": ["subject", "body"],
        "additionalProperties": False,
    },
)
async def send_email_tool(ctx: Context, subject: str, body: str, to: str = "") -> dict[str, Any]:
    settings = ctx.settings
    try:
        config = validate_config(
            sender=settings.get("sender"),
            auth_code=settings.get("auth_code"),
            smtp_host=settings.get("smtp_host"),
            smtp_port=settings.get("smtp_port"),
            security=settings.get("security"),
            timeout=settings.get("timeout"),
        )
        domains = normalize_domains(settings.get("recipient_domains")) or ("qq.com",)
        if not _has_email_intent(ctx):
            return {
                "error": "email_request_missing",
                "message": "当前消息没有明确要求发邮件；请让群友说明要发到哪个邮箱，不要凭上下文补发。",
            }
        recipients = parse_recipients(
            to,
            default_qq=ctx.user_id,
            max_recipients=int(settings.get("max_recipients") or 5),
            allowed_domains=domains,
        )
        if str(to or "").strip():
            text, allowed_qqs = _recipient_evidence(ctx)
            if any(not _recipient_requested(item, text, allowed_qqs) for item in recipients):
                return {
                    "error": "email_recipient_not_requested",
                    "message": "收件人必须出现在当前消息里；发给当前发言人请把 to 留空。",
                }
        clean_subject = parse_subject(subject)
        clean_body = parse_body(body, max_chars=int(settings.get("max_body_chars") or 5000))
    except EmailError as exc:
        return {"error": exc.code, "message": exc.message}

    try:
        result = await asyncio.to_thread(
            send_email,
            sender=config["sender"],
            auth_code=config["auth_code"],
            recipients=recipients,
            subject=clean_subject,
            body=clean_body,
            smtp_host=config["smtp_host"],
            smtp_port=config["smtp_port"],
            security=config["security"],
            timeout=config["timeout"],
        )
    except EmailError as exc:
        ctx.logger.warning("邮件发送失败：%s", exc.code)
        return {"error": exc.code, "message": exc.message}
    except Exception:
        ctx.logger.exception("邮件发送出现未预期错误")
        return {"error": "email_send_failed", "message": "邮件发送失败，请稍后再试"}

    return {
        "sent": True,
        "to": result["recipients"],
        "subject": clean_subject,
        "note": "邮件已发送；只说明发送结果，不要重复正文或声称发送给未列出的地址。",
    }


__all__ = ["plugin", "send_email_tool"]

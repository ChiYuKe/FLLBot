# Plugin: email | Role: SMTP transport and address validation | Metadata: plugin.toml
"""QQ 邮箱 SMTP 发送与地址校验，不依赖宿主事件循环。

这个模块只做三件事：把 QQ 号/邮箱整理成收件人、校验主题和正文、通过 SMTP 发送。
它不读取插件配置，也不记录邮件正文；网络错误统一转换成可安全展示的中文原因。
"""

from __future__ import annotations

import re
import smtplib
import ssl
from collections.abc import Iterable, Sequence
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from typing import Any

_EMAIL_RE = re.compile(
    r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$"
)
_DOMAIN_RE = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$"
)
_SPLIT_RE = re.compile(r"[\s,;，；、]+")
_QQ_RE = re.compile(r"^[1-9][0-9]{4,11}$")
_MAX_SUBJECT_CHARS = 200


class EmailError(ValueError):
    """可安全返回给模型的邮件错误。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def normalize_domains(values: Any) -> tuple[str, ...]:
    """把插件配置里的域名列表统一成小写、不带 @ 的形式。"""
    if values is None:
        raw: Iterable[Any] = ()
    elif isinstance(values, str):
        raw = re.split(r"[\s,;，；、]+", values)
    elif isinstance(values, (list, tuple, set)):
        raw = values
    else:
        raw = (values,)
    result: list[str] = []
    for item in raw:
        domain = str(item or "").strip().lstrip("@").casefold()
        if not domain:
            continue
        if not _DOMAIN_RE.fullmatch(domain):
            raise EmailError("email_config_invalid", "收件域名配置无效，请填写例如 qq.com 的域名")
        if domain not in result:
            result.append(domain)
    return tuple(result)


def _domain_allowed(domain: str, allowed_domains: Sequence[str]) -> bool:
    return not allowed_domains or domain.casefold() in {item.casefold() for item in allowed_domains}


def _email_from_qq(token: str) -> str:
    if not _QQ_RE.fullmatch(token):
        raise EmailError("email_recipient_invalid", "QQ 号格式不正确，应为 5–12 位数字")
    return f"{token}@qq.com"


def parse_recipients(
    raw: Any,
    *,
    default_qq: int = 0,
    max_recipients: int = 5,
    allowed_domains: Sequence[str] = ("qq.com",),
) -> list[str]:
    """接受 QQ 号或完整邮箱，返回去重后的收件人列表。

    ``raw`` 留空时使用 ``default_qq``（当前发言人的 QQ 号），这是“发到我自己邮箱”的快捷方式。
    多个收件人可以用逗号、分号、顿号、空格或换行分隔。
    """
    text = "" if raw is None else str(raw).strip()
    tokens = [item for item in _SPLIT_RE.split(text) if item]
    if not tokens:
        if default_qq:
            tokens = [str(int(default_qq))]
        else:
            raise EmailError("email_recipient_missing", "请提供收件人 QQ 号或 QQ 邮箱")
    if len(tokens) > max_recipients:
        raise EmailError("email_too_many_recipients", f"一次最多发送给 {max_recipients} 个收件人")

    recipients: list[str] = []
    for token in tokens:
        address = _email_from_qq(token) if token.isdigit() else token
        if not _EMAIL_RE.fullmatch(address):
            raise EmailError("email_recipient_invalid", "收件人邮箱格式不正确")
        domain = address.rsplit("@", 1)[-1].casefold()
        if not _domain_allowed(domain, allowed_domains):
            allowed = "、".join(allowed_domains) if allowed_domains else "未配置"
            raise EmailError("email_recipient_domain_denied", f"只允许发送到这些邮箱域名：{allowed}")
        normalized = f"{address.rsplit('@', 1)[0]}@{domain}"
        if normalized.casefold() not in {item.casefold() for item in recipients}:
            recipients.append(normalized)
    return recipients


def parse_subject(raw: Any) -> str:
    subject = str(raw or "").strip()
    if not subject:
        raise EmailError("email_subject_missing", "邮件主题不能为空")
    if any(ord(char) < 32 for char in subject):
        raise EmailError("email_subject_invalid", "邮件主题不能包含换行或控制字符")
    if len(subject) > _MAX_SUBJECT_CHARS:
        raise EmailError("email_subject_too_long", f"邮件主题不能超过 {_MAX_SUBJECT_CHARS} 字")
    return subject


def parse_body(raw: Any, *, max_chars: int = 5000) -> str:
    body = str(raw or "").replace("\r\n", "\n").replace("\r", "\n")
    if not body.strip():
        raise EmailError("email_body_missing", "邮件正文不能为空")
    if len(body) > max_chars:
        raise EmailError("email_body_too_long", f"邮件正文不能超过 {max_chars} 字")
    return body


def validate_config(
    *,
    sender: Any,
    auth_code: Any,
    smtp_host: Any,
    smtp_port: Any,
    security: Any,
    timeout: Any,
) -> dict[str, Any]:
    """校验发件配置；返回可直接交给发送函数的规范化参数。"""
    address = str(sender or "").strip()
    if not address:
        raise EmailError("email_not_configured", "还没有配置发件邮箱，请先在 email 插件设置里填写 QQ 邮箱")
    if "\r" in address or "\n" in address or not _EMAIL_RE.fullmatch(address):
        raise EmailError("email_config_invalid", "发件邮箱格式不正确，请填写完整的 QQ 邮箱地址")
    code = "".join(str(auth_code or "").split())
    if not code:
        raise EmailError(
            "email_not_configured",
            "还没有配置 SMTP 授权码；请到 QQ 邮箱设置里开启 SMTP 服务并生成授权码",
        )
    host = str(smtp_host or "").strip()
    if not _DOMAIN_RE.fullmatch(host):
        raise EmailError("email_config_invalid", "SMTP 服务器地址无效")
    try:
        port = int(smtp_port)
    except (TypeError, ValueError):
        raise EmailError("email_config_invalid", "SMTP 端口无效") from None
    if not 1 <= port <= 65535:
        raise EmailError("email_config_invalid", "SMTP 端口无效")
    if security not in {"ssl", "starttls"}:
        raise EmailError("email_config_invalid", "SMTP 加密方式只支持 ssl 或 starttls")
    try:
        wait = float(timeout)
    except (TypeError, ValueError):
        wait = 15.0
    return {
        "sender": address,
        "auth_code": code,
        "smtp_host": host.casefold(),
        "smtp_port": port,
        "security": security,
        "timeout": max(5.0, min(wait, 30.0)),
    }


def send_email(
    *,
    sender: str,
    auth_code: str,
    recipients: Sequence[str],
    subject: str,
    body: str,
    smtp_host: str = "smtp.qq.com",
    smtp_port: int = 465,
    security: str = "ssl",
    timeout: float = 15.0,
) -> dict[str, Any]:
    """同步发送一封纯文本邮件；调用方应放到线程里执行。"""
    message = EmailMessage()
    message["From"] = sender
    message["To"] = ", ".join(recipients)
    message["Subject"] = subject
    message["Date"] = formatdate(localtime=True)
    message["Message-ID"] = make_msgid(domain=sender.rsplit("@", 1)[-1])
    message.set_content(body, subtype="plain", charset="utf-8")

    client: smtplib.SMTP | smtplib.SMTP_SSL | None = None
    try:
        context = ssl.create_default_context()
        if security == "ssl":
            client = smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=timeout, context=context)
        else:
            client = smtplib.SMTP(smtp_host, smtp_port, timeout=timeout)
            client.ehlo()
            client.starttls(context=context)
            client.ehlo()
        client.login(sender, auth_code)
        refused = client.send_message(message)
    except smtplib.SMTPAuthenticationError as exc:
        raise EmailError("email_auth_failed", "发件邮箱认证失败，请检查 SMTP 授权码是否有效") from exc
    except smtplib.SMTPRecipientsRefused as exc:
        raise EmailError("email_recipient_refused", "邮件服务器拒绝了收件人，请检查邮箱地址") from exc
    except (smtplib.SMTPException, ssl.SSLError, OSError, TimeoutError) as exc:
        raise EmailError("email_send_failed", "邮件发送失败，请稍后再试或检查 SMTP 配置") from exc
    finally:
        if client is not None:
            try:
                client.quit()
            except (smtplib.SMTPException, OSError):
                client.close()

    if refused:
        raise EmailError("email_recipient_refused", "邮件服务器拒绝了部分收件人，请检查邮箱地址")
    return {"sent": True, "recipients": list(recipients)}


__all__ = [
    "EmailError",
    "normalize_domains",
    "parse_body",
    "parse_recipients",
    "parse_subject",
    "send_email",
    "validate_config",
]

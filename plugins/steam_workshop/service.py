"""Plugin: steam_workshop | Role: Steam Workshop data access and summaries | Metadata: plugin.toml."""

from __future__ import annotations

import asyncio
import os
import re
from collections.abc import Awaitable, Callable
from datetime import datetime
from html import unescape
from html.parser import HTMLParser
from typing import Any, TypeVar
from urllib.parse import parse_qs

import httpx

WORKSHOP_URL = "https://steamcommunity.com/sharedfiles/filedetails/?id={file_id}"
DETAILS_URL = "https://api.steampowered.com/ISteamRemoteStorage/GetPublishedFileDetails/v1/"
COLLECTION_URL = "https://api.steampowered.com/ISteamRemoteStorage/GetCollectionDetails/v1/"
APPDETAILS_URL = "https://store.steampowered.com/api/appdetails"
COMMENTS_URL = "https://steamcommunity.com/comment/PublishedFile_Public/render/{owner}/{file_id}/"
PROFILE_URL = "https://steamcommunity.com/profiles/{steam_id}"
STORE_URL = "https://store.steampowered.com/app/{app_id}/"

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
_MAX_FILE_ID = 20
_CHILD_LIMIT = 50
_LOCAL_PROXY_PORTS = (7897, 7890, 7891, 10809, 10808, 1080, 2080, 8889)
_T = TypeVar("_T")

_WORKSHOP_LINK = re.compile(
    r"(?:https?://)?(?:www\.)?steamcommunity\.com/(?:sharedfiles|workshop)/filedetails/?\?([^\s<>\"']+)",
    re.IGNORECASE,
)
_TAG = re.compile(r"<[^>]+>")
_SPACE = re.compile(r"[\s\u00a0\u2002\u2003\u200b]+")
_PAGE_TITLE = re.compile(
    r'<div[^>]*class="[^"]*workshopItemTitle[^"]*"[^>]*>(.*?)</div>', re.DOTALL | re.IGNORECASE
)
_PAGE_BREADCRUMBS = re.compile(
    r'<div[^>]*class="[^"]*breadcrumbs[^"]*"[^>]*>(.*?)</div>', re.DOTALL | re.IGNORECASE
)
_PAGE_AUTHOR = re.compile(
    r'<div[^>]*class="[^"]*friendBlockContent[^"]*"[^>]*>(.*?)<br', re.DOTALL | re.IGNORECASE
)
_PAGE_LINK = re.compile(r"<a[^>]*>(.*?)</a>", re.DOTALL | re.IGNORECASE)
_STATS_TABLE = re.compile(
    r'<table[^>]*class="[^"]*stats_table[^"]*"[^>]*>(.*?)</table>', re.DOTALL | re.IGNORECASE
)
_STAT_ROW = re.compile(r"<td[^>]*>(.*?)</td>\s*<td[^>]*>(.*?)</td>", re.DOTALL | re.IGNORECASE)
_STAT_LEFT = re.compile(
    r'<div[^>]*class="[^"]*detailsStatLeft[^"]*"[^>]*>(.*?)</div>', re.DOTALL | re.IGNORECASE
)
_STAT_RIGHT = re.compile(
    r'<div[^>]*class="[^"]*detailsStatRight[^"]*"[^>]*>(.*?)</div>', re.DOTALL | re.IGNORECASE
)

_BLOCK_TAGS = {
    "address",
    "article",
    "blockquote",
    "dd",
    "div",
    "dl",
    "dt",
    "figcaption",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "hr",
    "li",
    "main",
    "ol",
    "p",
    "pre",
    "section",
    "table",
    "tr",
    "ul",
}
_VOID_TAGS = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "param",
    "source",
    "track",
    "wbr",
}


class WorkshopError(Exception):
    """创意工坊数据无法读取或链接不合法。"""


class WorkshopNotFound(WorkshopError):
    """物品不存在、已删除或当前不可见。"""


# --------------------------------------------------------------------------- #
# 链接与文本工具
# --------------------------------------------------------------------------- #


def find_workshop_id(value: str) -> str | None:
    """从消息文本里找出创意工坊物品 ID；支持链接、带前后文的链接和裸 ID。"""
    text = (value or "").strip()
    if not text:
        return None
    if text.isdigit():
        return text if 0 < len(text) <= _MAX_FILE_ID and int(text) > 0 else None
    for match in _WORKSHOP_LINK.finditer(text):
        for candidate in parse_qs(match.group(1)).get("id") or []:
            candidate = candidate.strip()
            if candidate.isdigit() and len(candidate) <= _MAX_FILE_ID and int(candidate) > 0:
                return candidate
    if "steamcommunity.com" in text.lower():
        fallback = re.search(r"[?&]id=(\d{1,20})", text)
        if fallback and int(fallback.group(1)) > 0:
            return fallback.group(1)
    return None


def extract_id(value: str) -> str:
    file_id = find_workshop_id(value)
    if file_id is None:
        raise WorkshopError("没有识别到 Steam 创意工坊链接或物品 ID")
    return file_id


def _clean_text(fragment: str) -> str:
    return _SPACE.sub(" ", unescape(_TAG.sub("", fragment or ""))).strip()


def _first_group(pattern: re.Pattern[str], text: str) -> str:
    match = pattern.search(text or "")
    return match.group(1) if match else ""


def _normalize_text(value: str) -> str:
    text = (value or "").replace("\r\n", "\n").replace("\r", "\n").replace("\u00a0", " ")
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _clip(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    for separator in ("\n\n", "\n", "。", ". ", "！", "？", "；"):
        position = cut.rfind(separator)
        if position >= limit - 120:
            return cut[:position].rstrip() + "…"
    return cut.rstrip() + "…"


def _link_text(label: str, href: str) -> str:
    label = _clean_text(label)
    href = (href or "").strip()
    if not label or label == href:
        return href
    return f"{label} ({href})"


def bbcode_to_text(value: str) -> str:
    """把创意工坊接口返回的 BBCode 描述转换成可读纯文本。"""
    text = (value or "").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"\[img\][^\[]*\[/img\]", "", text, flags=re.IGNORECASE)
    text = re.sub(
        r"\[url=([^\]]+)\](.*?)\[/url\]",
        lambda match: _link_text(match.group(2), match.group(1)),
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    text = re.sub(r"\[url\](.*?)\[/url\]", r"\1", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"\[h[1-6]\]", "\n\n", text, flags=re.IGNORECASE)
    text = re.sub(r"\[/h[1-6]\]", "\n\n", text, flags=re.IGNORECASE)
    text = re.sub(r"\[/?(?:b|i|u|s|strike|spoiler|noparse)\]", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\[/?(?:list|olist|quote|table|code)\]", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"\[/?tr\]", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"\[/?t[dh]\]", " | ", text, flags=re.IGNORECASE)
    text = re.sub(r"\[\*\]", "\n- ", text)
    text = re.sub(r"\[hr\]", "\n---\n", text, flags=re.IGNORECASE)
    text = re.sub(r"\[/?[a-z][a-z0-9]*(?:=[^\]]*)?\]", "", text, flags=re.IGNORECASE)
    normalized = _normalize_text(unescape(text))
    return re.sub(r"\n{2,}(?=- )", "\n", normalized)


class _DescriptionParser(HTMLParser):
    """提取 ``div.workshopItemDescription`` 内的可读文本。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.depth = 0
        self.capturing = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        classes = (dict(attrs).get("class") or "").split()
        if not self.capturing:
            if tag == "div" and "workshopItemDescription" in classes:
                self.capturing = True
                self.depth = 1
            return
        if tag in {"br", "hr"}:
            self.parts.append("\n")
        elif tag == "li":
            self.parts.append("\n- ")
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")
        elif tag in {"td", "th"}:
            self.parts.append(" | ")
        if tag not in _VOID_TAGS:
            self.depth += 1

    def handle_endtag(self, tag: str) -> None:
        if not self.capturing or tag in _VOID_TAGS:
            return
        self.depth -= 1
        if self.depth <= 0:
            self.capturing = False
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.capturing:
            return
        text = " ".join(data.split())
        if text:
            self.parts.append(text + " ")

    def result(self) -> str:
        return _normalize_text("".join(self.parts))


def parse_page(html: str) -> dict[str, Any]:
    """从创意工坊页面提取标题、游戏名、作者、描述与页面统计。"""
    page_title = _clean_text(_first_group(_PAGE_TITLE, html))
    breadcrumbs = _first_group(_PAGE_BREADCRUMBS, html)
    game = _clean_text(_first_group(_PAGE_LINK, breadcrumbs)) if breadcrumbs else ""
    author = _clean_text(_first_group(_PAGE_AUTHOR, html))
    parser = _DescriptionParser()
    parser.feed(html or "")
    return {
        "title": page_title,
        "game": game,
        "author": author,
        "description": parser.result(),
        "file_size": _parse_page_size(html),
        "stats": _parse_page_stats(html),
    }


def _parse_page_stats(html: str) -> dict[str, int]:
    stats = {"views": 0, "subscriptions": 0, "favorites": 0}
    block = _first_group(_STATS_TABLE, html)
    for first, second in _STAT_ROW.findall(block):
        left = _clean_text(first)
        right = _clean_text(second)
        value_text, label = (left, right) if left.replace(",", "").isdigit() else (right, left)
        try:
            number = int(value_text.replace(",", "").strip())
        except ValueError:
            continue
        lowered = label.lower()
        if "访客" in label or "visitor" in lowered:
            stats["views"] = number
        elif "订阅" in label or "subscriber" in lowered:
            stats["subscriptions"] = number
        elif "收藏" in label or "favorite" in lowered:
            stats["favorites"] = number
    return stats


def _parse_page_size(html: str) -> str:
    labels = [_clean_text(item) for item in _STAT_LEFT.findall(html)]
    values = [_clean_text(item) for item in _STAT_RIGHT.findall(html)]
    for label, value in zip(labels, values, strict=False):
        if label in {"文件大小", "File Size"}:
            return value
    return ""


def _format_size(value: Any) -> str:
    try:
        size = int(value)
    except (TypeError, ValueError):
        return ""
    if size <= 0:
        return ""
    number = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if number < 1024 or unit == "GB":
            return f"{number:.0f} {unit}" if unit == "B" else f"{number:.1f} {unit}"
        number /= 1024
    return ""


def _format_timestamp(value: Any) -> str:
    try:
        timestamp = int(value)
    except (TypeError, ValueError):
        return ""
    if timestamp <= 0:
        return ""
    try:
        moment = datetime.fromtimestamp(timestamp).astimezone()
    except (ValueError, OSError):
        return ""
    return moment.strftime("%Y-%m-%d %H:%M")


def _positive_int(value: Any) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, number)


# --------------------------------------------------------------------------- #
# 代理与网络请求
# --------------------------------------------------------------------------- #


def _new_client(timeout: float, proxy: str | None = None) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=True,
        proxy=proxy,
        headers={"User-Agent": _USER_AGENT, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6"},
    )


def _environment_proxies() -> list[str]:
    values: list[str] = []
    for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        raw = (os.environ.get(name) or "").strip()
        if raw.lower().startswith(("http://", "https://")) and raw not in values:
            values.append(raw)
    return values


async def _local_proxy_candidates() -> list[str]:
    async def probe(port: int) -> str | None:
        try:
            _, writer = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", port), timeout=0.25)
        except OSError:
            return None
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass
        return f"http://127.0.0.1:{port}"

    found = await asyncio.gather(*(probe(port) for port in _LOCAL_PROXY_PORTS))
    return [candidate for candidate in found if candidate]


async def _proxy_candidates(explicit: str, auto_proxy: bool) -> list[str | None]:
    explicit = (explicit or "").strip()
    if explicit:
        return [explicit]
    candidates: list[str | None] = [None]
    if auto_proxy:
        for proxy in [*_environment_proxies(), *await _local_proxy_candidates()]:
            if proxy not in candidates:
                candidates.append(proxy)
    return candidates


async def _try_candidates(
    candidates: list[str | None],
    operation: Callable[[str | None], Awaitable[_T]],
) -> tuple[_T, str | None]:
    failures: list[str] = []
    for candidate in candidates:
        try:
            return await operation(candidate), candidate
        except WorkshopError:
            raise
        except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError, UnicodeError) as exc:
            failures.append(type(exc).__name__)
    detail = failures[-1] if failures else "未知错误"
    raise WorkshopError(
        "无法连接 Steam 创意工坊（"
        + detail
        + "）。如果当前网络不能直连 Steam 社区，请在插件设置「代理地址」里填写可用的 HTTP 代理，"
        "例如 http://127.0.0.1:7897。"
    )


async def _safe(coro: Awaitable[_T]) -> _T | None:
    try:
        return await coro
    except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError, UnicodeError, WorkshopError):
        return None


async def _fetch_details(file_id: str, timeout: float, proxy: str | None) -> dict[str, Any]:
    async with _new_client(timeout, proxy) as client:
        response = await client.post(
            DETAILS_URL,
            data={"itemcount": 1, "publishedfileids[0]": file_id},
        )
        response.raise_for_status()
        payload = response.json() or {}
    details = (payload.get("response") or {}).get("publishedfiledetails") or []
    if not details or not isinstance(details[0], dict):
        raise ValueError("创意工坊没有返回物品信息")
    entry = details[0]
    result = _positive_int(entry.get("result"))
    if result == 9:
        raise WorkshopNotFound("创意工坊里找不到这个物品：可能已删除、设为私密或链接有误")
    if result != 1:
        raise WorkshopNotFound(f"创意工坊无法读取这个物品（result={result or '未知'}）")
    return entry


async def _fetch_collection(file_id: str, timeout: float, proxy: str | None) -> list[dict[str, Any]] | None:
    async with _new_client(timeout, proxy) as client:
        response = await client.post(
            COLLECTION_URL,
            data={"collectioncount": 1, "publishedfileids[0]": file_id},
        )
        response.raise_for_status()
        payload = response.json() or {}
    details = (payload.get("response") or {}).get("collectiondetails") or []
    if not details or not isinstance(details[0], dict):
        return None
    entry = details[0]
    if _positive_int(entry.get("result")) != 1:
        return None
    return [child for child in entry.get("children") or [] if isinstance(child, dict)]


async def _fetch_children(
    children: list[dict[str, Any]], timeout: float, proxy: str | None
) -> list[dict[str, Any]]:
    ids: list[str] = []
    for child in children:
        file_id = str(child.get("publishedfileid") or "").strip()
        if file_id.isdigit() and file_id not in ids:
            ids.append(file_id)
        if len(ids) >= _CHILD_LIMIT:
            break
    if not ids:
        return []
    form: dict[str, Any] = {"itemcount": len(ids)}
    form.update({f"publishedfileids[{index}]": file_id for index, file_id in enumerate(ids)})
    async with _new_client(timeout, proxy) as client:
        response = await client.post(DETAILS_URL, data=form)
        response.raise_for_status()
        payload = response.json() or {}
    details = (payload.get("response") or {}).get("publishedfiledetails") or []
    by_id = {
        str(item.get("publishedfileid") or ""): item
        for item in details
        if isinstance(item, dict)
    }
    items: list[dict[str, Any]] = []
    for index, file_id in enumerate(ids, 1):
        detail = by_id.get(file_id) or {}
        items.append(
            {
                "id": file_id,
                "title": str(detail.get("title") or "").strip(),
                "url": WORKSHOP_URL.format(file_id=file_id),
                "sort_order": index,
            }
        )
    return items


async def _fetch_page(file_id: str, timeout: float, proxy: str | None) -> dict[str, Any]:
    async with _new_client(timeout, proxy) as client:
        response = await client.get(WORKSHOP_URL.format(file_id=file_id))
        response.raise_for_status()
        content_type = response.headers.get("content-type", "").lower()
        if "html" not in content_type:
            raise ValueError("创意工坊返回的不是 HTML 页面")
        return parse_page(response.text)


async def _fetch_app(app_id: int, timeout: float, proxy: str | None) -> dict[str, str] | None:
    async with _new_client(timeout, proxy) as client:
        response = await client.get(APPDETAILS_URL, params={"appids": str(app_id), "l": "schinese"})
        response.raise_for_status()
        payload = response.json() or {}
    entry = payload.get(str(app_id)) or {}
    if not entry.get("success"):
        return None
    name = str((entry.get("data") or {}).get("name") or "").strip()
    return {"name": name} if name else None


# --------------------------------------------------------------------------- #
# 组装与展示
# --------------------------------------------------------------------------- #


def _empty_item(file_id: str) -> dict[str, Any]:
    return {
        "id": file_id,
        "url": WORKSHOP_URL.format(file_id=file_id),
        "title": "",
        "kind": "item",
        "kind_label": "物品",
        "game": {"app_id": 0, "name": "", "name_en": "", "url": ""},
        "author": {"steam_id": "", "name": "", "url": ""},
        "tags": [],
        "file_size": "",
        "file_size_bytes": 0,
        "created_at": "",
        "updated_at": "",
        "stats": {
            "subscriptions": 0,
            "favorites": 0,
            "views": 0,
            "lifetime_subscriptions": 0,
            "lifetime_favorites": 0,
        },
        "description": "",
        "description_truncated": False,
        "preview_url": "",
        "children": [],
        "child_count": 0,
        "banned": False,
        "note": "",
    }


def _page_fields(page: dict[str, Any]) -> dict[str, Any]:
    stats = page.get("stats") or {}
    return {
        "title": page.get("title") or "",
        "description": page.get("description") or "",
        "game": {"app_id": 0, "name": page.get("game") or "", "name_en": "", "url": ""},
        "author": {"steam_id": "", "name": page.get("author") or "", "url": ""},
        "file_size": page.get("file_size") or "",
        "stats": {
            "subscriptions": _positive_int(stats.get("subscriptions")),
            "favorites": _positive_int(stats.get("favorites")),
            "views": _positive_int(stats.get("views")),
            "lifetime_subscriptions": 0,
            "lifetime_favorites": 0,
        },
    }


async def load_item(
    value: str,
    *,
    proxy: str = "",
    auto_proxy: bool = True,
    timeout: float = 20.0,
    description_limit: int = 8000,
) -> dict[str, Any]:
    """读取一个创意工坊物品或合集，返回可直接交给模型的结构化资料。"""
    file_id = extract_id(value)
    candidates = await _proxy_candidates(proxy, auto_proxy)
    try:
        entry, used_proxy = await _try_candidates(
            candidates, lambda candidate: _fetch_details(file_id, timeout, candidate)
        )
    except WorkshopNotFound:
        raise
    except WorkshopError:
        page, _ = await _try_candidates(
            candidates, lambda candidate: _fetch_page(file_id, timeout, candidate)
        )
        data = _empty_item(file_id)
        data.update(_page_fields(page))
        data["note"] = "Steam 官方接口暂时不可用，以下信息来自创意工坊页面，统计可能不完整。"
        return data

    app_id = _positive_int(entry.get("consumer_app_id"))
    page_task = asyncio.create_task(_safe(_fetch_page(file_id, timeout, used_proxy)))
    collection_task = asyncio.create_task(_safe(_fetch_collection(file_id, timeout, used_proxy)))
    app_task = asyncio.create_task(_safe(_fetch_app(app_id, timeout, used_proxy))) if app_id else None
    page = (await page_task) or {}
    collection = await collection_task
    app = (await app_task) if app_task is not None else None
    app = app or {}

    data = _empty_item(file_id)
    page_game = str(page.get("game") or "").strip()
    app_name = str(app.get("name") or "").strip()
    game_name = app_name or page_game
    data["title"] = (
        str(entry.get("title") or "").strip()
        or str(page.get("title") or "").strip()
        or f"创意工坊物品 {file_id}"
    )
    data["game"] = {
        "app_id": app_id,
        "name": game_name,
        "name_en": page_game if page_game and page_game != game_name else "",
        "url": STORE_URL.format(app_id=app_id) if app_id else "",
    }
    creator = str(entry.get("creator") or "").strip()
    data["author"] = {
        "steam_id": creator,
        "name": str(page.get("author") or "").strip(),
        "url": PROFILE_URL.format(steam_id=creator) if creator.isdigit() else "",
    }

    tags: list[str] = []
    for tag in entry.get("tags") or []:
        name = str(tag.get("tag") or "").strip() if isinstance(tag, dict) else ""
        if name and name not in tags:
            tags.append(name)
    data["tags"] = tags
    data["file_size_bytes"] = _positive_int(entry.get("file_size"))
    data["file_size"] = _format_size(entry.get("file_size"))
    data["created_at"] = _format_timestamp(entry.get("time_created"))
    data["updated_at"] = _format_timestamp(entry.get("time_updated"))
    data["stats"] = {
        "subscriptions": _positive_int(entry.get("subscriptions")),
        "favorites": _positive_int(entry.get("favorited")),
        "views": _positive_int(entry.get("views")),
        "lifetime_subscriptions": _positive_int(entry.get("lifetime_subscriptions")),
        "lifetime_favorites": _positive_int(entry.get("lifetime_favorited")),
    }
    data["preview_url"] = str(entry.get("preview_url") or "").strip()
    data["banned"] = bool(entry.get("banned"))

    raw_description = str(entry.get("description") or "")
    if raw_description.strip():
        description = bbcode_to_text(raw_description)
    else:
        description = str(page.get("description") or "")
    data["description_truncated"] = len(description) > description_limit
    data["description"] = _clip(description, description_limit) if data["description_truncated"] else description

    if collection is not None:
        data["kind"] = "collection"
        data["kind_label"] = "合集"
        data["child_count"] = len(collection)
        child_items = await _safe(_fetch_children(collection, timeout, used_proxy))
        if child_items is None:
            child_items = [
                {
                    "id": str(child.get("publishedfileid") or ""),
                    "title": "",
                    "url": WORKSHOP_URL.format(file_id=child.get("publishedfileid")),
                    "sort_order": index,
                }
                for index, child in enumerate(collection[:_CHILD_LIMIT], 1)
            ]
        data["children"] = child_items

    notes: list[str] = []
    if data["banned"]:
        notes.append("该物品已被 Steam 标记为封禁。")
    if _positive_int(entry.get("visibility")) == 2:
        notes.append("该物品可能仅作者或好友可见。")
    data["note"] = " ".join(notes)
    return data



_COMMENT_BLOCK = re.compile(r'<div[^>]*class="[^"]*commentthread_comment[^"]*"[^>]*>(.*?)</div>\s*</div>\s*</div>', re.DOTALL | re.IGNORECASE)
_COMMENT_AUTHOR = re.compile(r'class="[^"]*commentthread_author_link[^"]*"[^>]*>.*?<bdi[^>]*>(.*?)</bdi>', re.DOTALL | re.IGNORECASE)
_COMMENT_TEXT = re.compile(r'class="[^"]*commentthread_comment_text[^"]*"[^>]*>(.*?)</div>', re.DOTALL | re.IGNORECASE)
_COMMENT_TIME = re.compile(r'class="[^"]*commentthread_comment_timestamp[^"]*"[^>]*title="([^"]*)"', re.IGNORECASE)


def parse_comments(fragment: str) -> list[dict[str, str]]:
    comments = []
    for block in _COMMENT_BLOCK.findall(fragment or ""):
        author = _clean_text(_first_group(_COMMENT_AUTHOR, block))
        text = _clean_text(_first_group(_COMMENT_TEXT, block))
        timestamp = _first_group(_COMMENT_TIME, block)
        if author and text:
            comments.append({"author": author, "text": text, "timestamp": timestamp})
    return comments


async def find_comments(value: str, author: str = "", *, proxy: str = "", auto_proxy: bool = True,
                        timeout: float = 20.0, max_comments: int = 500) -> dict[str, Any]:
    """Search a bounded number of public Workshop comments for an exact display name."""
    file_id = extract_id(value)
    target = _normalize_text(author).casefold()
    candidates = await _proxy_candidates(proxy, auto_proxy)
    entry, used_proxy = await _try_candidates(candidates, lambda candidate: _fetch_details(file_id, timeout, candidate))
    owner = str(entry.get("creator") or "")
    if not owner.isdigit():
        raise WorkshopError("无法取得物品作者 Steam ID，不能读取评论区")

    async def search(candidate: str | None) -> dict[str, Any]:
        all_comments = []
        start, total = 0, 0
        async with _new_client(timeout, candidate) as client:
            while start < min(max_comments, 500):
                count = min(100, max_comments - start)
                response = await client.post(COMMENTS_URL.format(owner=owner, file_id=file_id),
                                             data={"start": start, "count": count, "totalcount": 0})
                response.raise_for_status()
                payload = response.json()
                if not payload.get("success"):
                    raise WorkshopError("Steam 评论接口未返回成功状态")
                total = int(payload.get("total_count") or 0)
                page = parse_comments(str(payload.get("comments_html") or ""))
                all_comments.extend(page)
                start += max(1, int(payload.get("pagesize") or len(page) or count))
                if not page or start >= total:
                    break
        matches = [comment for comment in all_comments if target and target in comment["author"].casefold()]
        contextual = all_comments if len(all_comments) <= 30 else all_comments[-20:]
        if matches:
            contextual = list(dict.fromkeys(tuple(comment.items()) for comment in [*contextual, *matches]))
            contextual = [dict(comment) for comment in contextual]
        page = await _safe(_fetch_page(file_id, timeout, candidate))
        return {"item_id": file_id, "target_author": author or None,
                "item_author": {"steam_id": owner, "name": str((page or {}).get("author") or "")},
                "comments": contextual, "matched_comments": matches,
                "total_comments": total, "searched": min(start, max_comments),
                "truncated_search": start < total}

    result, _ = await _try_candidates([used_proxy], search)
    return result

def format_item(data: dict[str, Any], *, description_limit: int = 600) -> str:
    """把结构化资料排版成适合 QQ 文本阅读的摘要。"""
    if data.get("error"):
        return f"读取创意工坊失败：{data['error']}"
    title = data.get("title") or "（无标题）"
    lines = [f"【Steam 创意工坊·{data.get('kind_label') or '物品'}】{title}"]

    game = data.get("game") or {}
    game_text = str(game.get("name") or "")
    if game.get("name_en") and game["name_en"] != game_text:
        game_text = f"{game_text}（{game['name_en']}）"
    meta: list[str] = []
    if game_text:
        meta.append(f"游戏：{game_text}")
    author = str((data.get("author") or {}).get("name") or "")
    if author:
        meta.append(f"作者：{author}")
    if meta:
        lines.append(" ｜ ".join(meta))

    tags = data.get("tags") or []
    if tags:
        shown = "、".join(tags[:10])
        if len(tags) > 10:
            shown += f" 等 {len(tags)} 个"
        lines.append(f"标签：{shown}")

    facts: list[str] = []
    if data.get("file_size"):
        facts.append(f"大小 {data['file_size']}")
    if data.get("created_at"):
        facts.append(f"发布 {data['created_at'][:10]}")
    if data.get("updated_at") and data["updated_at"] != data.get("created_at"):
        facts.append(f"更新 {data['updated_at'][:10]}")
    if facts:
        lines.append(" ｜ ".join(facts))

    stats = data.get("stats") or {}
    stat_parts: list[str] = []
    if stats.get("subscriptions"):
        stat_parts.append(f"订阅 {stats['subscriptions']:,}")
    if stats.get("favorites"):
        stat_parts.append(f"收藏 {stats['favorites']:,}")
    if stats.get("views"):
        stat_parts.append(f"浏览 {stats['views']:,}")
    if stat_parts:
        lines.append(" ｜ ".join(stat_parts))

    if data.get("kind") == "collection":
        children = data.get("children") or []
        total = int(data.get("child_count") or len(children))
        lines.append(f"合集共 {total} 件物品：")
        for index, child in enumerate(children[:10], 1):
            lines.append(f"{index}. {child.get('title') or child.get('id')}")
        if total > 10:
            lines.append(f"…另有 {total - 10} 件")
        if total > len(children):
            lines.append(f"（已读取前 {len(children)} 件标题）")

    description = str(data.get("description") or "").strip()
    lines.append("")
    lines.append("简介：" if description else "简介：（作者没有填写描述）")
    if description:
        lines.append(_clip(description, description_limit))
        if data.get("description_truncated") or len(description) > description_limit:
            lines.append("（简介较长，以上为节选）")
    lines.append(f"链接：{data.get('url')}")
    if data.get("note"):
        lines.append(f"注意：{data['note']}")
    return "\n".join(lines)

# Plugin: web | Role: QQ weather provider | Metadata: plugin.toml
"""腾讯天气（QQ 天气）数据源。

先用 ``wis.qq.com/city/like`` 按城市名解析省/市/区，再取观测、逐小时/逐日预报、空气质量、
日出日落和生活指数。接口来自 QQ 天气网页，免密钥、国内直连，只覆盖国内城市；返回结构与
Open-Meteo 版保持一致，文字天气和天气卡片共用。网络错误交给调用方统一包装。
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

_CITY_URL = "https://wis.qq.com/city/like"
_WEATHER_URL = "https://wis.qq.com/weather/common"
_WEATHER_TYPES = "observe|forecast_1h|forecast_24h|air|rise|index"
_SUFFIX = re.compile(r"(特别行政区|自治区|自治州|地区|城市|市|区|县|省)$")
_WEEKDAYS = "一二三四五六日"
_BEIJING = timezone(timedelta(hours=8))
# 卡片生活指数按 QQ 天气的常见顺序取，缺项再用接口返回的其他指数补齐。
_INDEX_ORDER = ("drying", "sports", "morning", "ultraviolet", "allergy", "clothes", "makeup", "cold")

_THUNDER_CODES = {"04", "05"}
_SNOW_CODES = {"06", "13", "14", "15", "16", "17", "26", "27", "28", "302"}
_FOG_CODES = {"18", "20", "29", "30", "31", "32", "49", "53", "54", "55", "56", "57", "58"}
_RAIN_CODES = {"03", "07", "08", "09", "10", "11", "12", "19", "21", "22", "23", "24", "25", "301"}


class CityNotFound(ValueError):
    """QQ 天气没有匹配到城市；调用方据此给出可读提示。"""


def _strip_suffix(name: str) -> str:
    return _SUFFIX.sub("", name.strip()).strip()


def _number(value: Any, digits: int = 1) -> float | int | None:
    try:
        number = round(float(value), digits)
    except (TypeError, ValueError):
        return None
    # QQ 天气整数字段很多（14、100、1015），文字里显示成 14.0 不好看。
    return int(number) if number == int(number) else number


def _integer(value: Any) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _weekday(date_text: str) -> str:
    try:
        return "星期" + _WEEKDAYS[datetime.strptime(date_text, "%Y-%m-%d").weekday()]
    except (TypeError, ValueError):
        return ""


def _timestamp(value: Any) -> str:
    try:
        return datetime.strptime(str(value or "").strip(), "%Y%m%d%H%M").strftime("%Y-%m-%dT%H:%M")
    except (TypeError, ValueError):
        return ""


def _icon(code: str, text: str, is_day: bool) -> str:
    """把腾讯天气代码映射成卡片使用的图标类型；未知代码退回天气文字判断。"""
    code = str(code or "").strip().zfill(2)
    if code in _THUNDER_CODES:
        return "thunder"
    if code in _SNOW_CODES:
        return "snow"
    if code in _FOG_CODES:
        return "fog"
    if code in _RAIN_CODES:
        return "rain"
    if code == "00":
        return "sun" if is_day else "moon"
    if code == "01":
        return "partly" if is_day else "partly_night"
    if code == "02":
        return "overcast"
    text = str(text or "")
    if "雷" in text:
        return "thunder"
    if "雪" in text or "冰粒" in text:
        return "snow"
    if "雨" in text:
        return "rain"
    if "雾" in text or "霾" in text or "沙" in text or "尘" in text:
        return "fog"
    if "阴" in text:
        return "overcast"
    if "云" in text:
        return "partly" if is_day else "partly_night"
    if "晴" in text:
        return "sun" if is_day else "moon"
    return "cloud"


def _wind_text(observe: dict[str, Any]) -> str:
    direction = str(observe.get("wind_direction_name") or "").strip()
    power = str(observe.get("wind_power") or "").strip()
    if direction and power:
        return f"{direction} {power}级"
    return f"{direction or ''}{power}级" if power else direction


def _unique(parts: list[str]) -> list[str]:
    result: list[str] = []
    for part in parts:
        if part and part not in result:
            result.append(part)
    return result


def _pick(data: dict[str, Any], query: str) -> dict[str, Any] | None:
    """从 city/like 的模糊结果里挑最贴近输入的一条。"""
    wanted = _strip_suffix(query)
    best_score: int | None = None
    best: tuple[str, list[str]] | None = None
    for code, label in data.items():
        parts = [part.strip() for part in str(label).replace("，", ",").split(",") if part.strip()]
        if not parts:
            continue
        score = 3
        if parts[-1] == wanted:
            score = 0
        elif len(parts) > 1 and parts[1] == wanted:
            score = 1
        elif parts[0] == wanted:
            score = 2
        elif wanted and wanted in parts:
            score = 1
        if best_score is None or score < best_score:
            best_score, best = score, (str(code), parts)
        if score == 0:
            break
    if best is None:
        return None
    code, parts = best
    return {
        "code": code,
        "province": parts[0],
        "city": parts[1] if len(parts) > 1 else parts[0],
        "district": parts[2] if len(parts) > 2 else "",
        "name": parts[-1],
        "region": "，".join(_unique(parts[:-1])),
    }


async def resolve(client: httpx.AsyncClient, name: str) -> dict[str, Any] | None:
    """把用户输入的城市名解析成 QQ 天气需要的省/市/区。"""
    queries = [name.strip()]
    shortened = _strip_suffix(name)
    if shortened and shortened != name.strip():
        queries.append(shortened)
    for query in dict.fromkeys(queries):
        response = await client.get(_CITY_URL, params={"source": "pc", "city": query})
        response.raise_for_status()
        place = _pick((response.json() or {}).get("data") or {}, query)
        if place:
            return place
    return None


def _is_daytime(clock: str, sunrise: str, sunset: str) -> bool:
    if not clock:
        return True
    if sunrise and clock < sunrise:
        return False
    if sunset and clock >= sunset:
        return False
    if sunrise or sunset:
        return True
    try:
        hour = int(clock[:2])
    except (TypeError, ValueError):
        return True
    return 6 <= hour < 18


def _rise_today(rise: Any, today_compact: str) -> tuple[str, str]:
    items = rise.values() if isinstance(rise, dict) else rise if isinstance(rise, list) else []
    for item in items:
        if isinstance(item, dict) and str(item.get("time") or "") == today_compact:
            return str(item.get("sunrise") or ""), str(item.get("sunset") or "")
    return "", ""


def _hourly(raw: Any, sunrise: str, sunset: str, now: datetime) -> list[dict[str, Any]]:
    """取当前小时起的 7 段逐小时预报（QQ 返回 48 段，键序不是时间序）。"""
    items = raw.values() if isinstance(raw, dict) else raw if isinstance(raw, list) else []
    entries: list[tuple[datetime, dict[str, Any]]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            moment = datetime.strptime(str(item.get("update_time") or ""), "%Y%m%d%H%M%S")
        except (TypeError, ValueError):
            continue
        entries.append((moment, item))
    entries.sort(key=lambda pair: pair[0])
    start = now.replace(tzinfo=None, minute=0, second=0, microsecond=0)
    upcoming = [pair for pair in entries if pair[0] >= start] or entries
    result: list[dict[str, Any]] = []
    for moment, item in upcoming[:7]:
        text = str(item.get("weather") or item.get("weather_short") or "未知").strip()
        code = str(item.get("weather_code") or "")
        result.append(
            {
                "time": moment.strftime("%H:%M"),
                "weather": text,
                "degree_c": _number(item.get("degree")),
                "icon": _icon(code, text, _is_daytime(moment.strftime("%H:%M"), sunrise, sunset)),
            }
        )
    return result


def _indices(raw: Any) -> list[dict[str, str]]:
    """整理生活指数：优先固定顺序，最多 8 条。"""
    if not isinstance(raw, dict):
        return []
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for key in _INDEX_ORDER:
        item = raw.get(key)
        if isinstance(item, dict) and item.get("name"):
            result.append(
                {
                    "key": key,
                    "name": str(item.get("name")),
                    "info": str(item.get("info") or ""),
                    "detail": str(item.get("detail") or ""),
                }
            )
            seen.add(key)
        if len(result) >= 8:
            return result
    for key, item in raw.items():
        if key in seen or not isinstance(item, dict) or not item.get("name"):
            continue
        result.append(
            {
                "key": key,
                "name": str(item.get("name")),
                "info": str(item.get("info") or ""),
                "detail": str(item.get("detail") or ""),
            }
        )
        if len(result) >= 8:
            break
    return result


async def run(client: httpx.AsyncClient, city: str, days: int = 1) -> dict[str, Any]:
    """查询 QQ 天气并整理成与 Open-Meteo 版相同的结构。"""
    place = await resolve(client, city)
    if place is None:
        raise CityNotFound(
            f"找不到城市：{city}；QQ 天气只覆盖国内城市，请用中文城市名（如 武汉、杭州）。"
        )
    params = {
        "source": "pc",
        "province": place["province"],
        "city": place["city"],
        "weather_type": _WEATHER_TYPES,
    }
    if place["district"]:
        params["country"] = place["district"]
    response = await client.get(_WEATHER_URL, params=params)
    response.raise_for_status()
    payload = response.json() or {}
    status = _integer(payload.get("status"))
    if status is not None and status != 200:
        raise ValueError(f"QQ 天气返回异常：{payload.get('message') or payload.get('status')}")

    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    observe = data.get("observe") if isinstance(data.get("observe"), dict) else {}
    if not str(observe.get("degree") or "").strip():
        raise CityNotFound(
            f"找不到城市：{city}；QQ 天气只覆盖国内城市，请用中文城市名（如 武汉、杭州）。"
        )

    now = datetime.now(_BEIJING)
    observed_at = _timestamp(observe.get("update_time")) or now.strftime("%Y-%m-%dT%H:%M")
    today = observed_at[:10]
    today_compact = today.replace("-", "")
    sunrise, sunset = _rise_today(data.get("rise"), today_compact)
    clock = observed_at[11:16]
    daytime = _is_daytime(clock, sunrise, sunset)
    weather_text = str(observe.get("weather") or observe.get("weather_short") or "未知").strip()
    code = str(observe.get("weather_code") or "")

    air = data.get("air") if isinstance(data.get("air"), dict) else {}
    daily: list[dict[str, Any]] = []
    forecast = data.get("forecast_24h")
    if isinstance(forecast, dict):
        forecast = list(forecast.values())
    elif not isinstance(forecast, list):
        forecast = []
    entries = [
        (str(item.get("time") or ""), item)
        for item in forecast
        if isinstance(item, dict) and str(item.get("time") or "")
    ]
    entries.sort(key=lambda pair: pair[0])
    upcoming = [pair for pair in entries if pair[0] >= today] or entries
    for date_text, item in upcoming[: max(1, min(int(days or 1), 7))]:
        day_weather = str(item.get("day_weather") or item.get("night_weather") or "未知").strip()
        day_code = str(item.get("day_weather_code") or "")
        daily.append(
            {
                "date": date_text,
                "weekday": _weekday(date_text),
                "weather": day_weather,
                "code": _integer(day_code),
                "icon": _icon(day_code, day_weather, True),
                "temp_max_c": _number(item.get("max_degree")),
                "temp_min_c": _number(item.get("min_degree")),
                "precip_prob_percent": None,
                "aqi": _integer(item.get("aqi")),
                "aqi_name": str(item.get("aqi_name") or ""),
            }
        )

    return {
        "city": place["name"],
        "region": place["region"],
        "timezone": "Asia/Shanghai",
        "utc_offset": "UTC+08:00",
        "source": "QQ 天气",
        "observed_at": observed_at,
        "sunrise": sunrise,
        "sunset": sunset,
        "current": {
            "weather": weather_text,
            "code": _integer(code),
            "icon": _icon(code, weather_text, daytime),
            "temperature_c": _number(observe.get("degree")),
            "apparent_c": None,
            "humidity_percent": _number(observe.get("humidity"), 0),
            "precipitation_mm": _number(observe.get("precipitation")),
            "wind_kmh": None,
            "wind_text": _wind_text(observe),
            "pressure_hpa": _number(observe.get("pressure"), 0),
            "is_day": daytime,
        },
        "air": {"aqi": _integer(air.get("aqi")), "name": str(air.get("aqi_name") or "")},
        "hourly": _hourly(data.get("forecast_1h"), sunrise, sunset, now),
        "index": _indices(data.get("index")),
        "daily": daily,
    }

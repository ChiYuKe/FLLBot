"""插件声明式配置框架测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from fllbot.plugin.settings import MASK, PluginSettings, SettingField, as_list
from fllbot.storage import Storage


def build_settings() -> PluginSettings:
    settings = PluginSettings(lambda: "demo")
    settings.define(SettingField("enabled", type="bool", default=True))
    settings.define(SettingField("limit", type="int", default=5, minimum=1, maximum=10))
    settings.define(SettingField("ratio", type="float", default=0.5, minimum=0, maximum=1))
    settings.define(SettingField("mode", type="select", options=["a", "b"], default="a"))
    settings.define(SettingField("tags", type="list", default=[]))
    settings.define(SettingField("welcome", type="text", default="hi", group_scoped=True))
    settings.define(SettingField("token", type="string", default="", secret=True))
    return settings


def test_defaults_and_schema() -> None:
    settings = build_settings()
    assert settings.get("enabled") is True
    assert settings.get("welcome") == "hi"
    assert settings.get("missing", "fallback") == "fallback"

    schema = {item["key"]: item for item in settings.schema()}
    assert schema["limit"]["min"] == 1
    assert schema["mode"]["options"] == ["a", "b"]
    assert schema["welcome"]["group_scoped"] is True
    assert schema["token"]["secret"] is True
    assert schema["enabled"]["type"] == "bool"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("true", True), ("off", False), (0, False), (1, True), ("是", True), ("关", False)],
)
def test_bool_coercion(raw: object, expected: bool) -> None:
    assert build_settings().get_field("enabled").coerce(raw) is expected  # type: ignore[union-attr]


def test_numeric_and_select_validation() -> None:
    settings = build_settings()
    assert settings.get_field("limit").coerce("8") == 8  # type: ignore[union-attr]
    assert settings.get_field("ratio").coerce("0.25") == 0.25  # type: ignore[union-attr]
    with pytest.raises(ValueError):
        settings.get_field("limit").coerce(99)  # type: ignore[union-attr]
    with pytest.raises(ValueError):
        settings.get_field("mode").coerce("c")  # type: ignore[union-attr]
    with pytest.raises(ValueError):
        settings.get_field("enabled").coerce("maybe")  # type: ignore[union-attr]


def test_list_coercion() -> None:
    assert as_list("a\nb,c") == ["a", "b", "c"]
    assert as_list(["x", " y "]) == ["x", "y"]
    assert as_list(None) == []


def test_duplicate_field_rejected() -> None:
    settings = build_settings()
    with pytest.raises(ValueError):
        settings.define(SettingField("enabled", type="bool"))


def test_invalid_field_type_rejected() -> None:
    with pytest.raises(ValueError):
        SettingField("x", type="weird")
    with pytest.raises(ValueError):
        SettingField("", type="bool")


async def test_persist_and_group_override(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "s.db")
    await storage.connect()
    try:
        settings = build_settings()
        await settings.load(storage)
        await settings.set("limit", 8)
        await settings.set("welcome", "群欢迎", group_id=20002)
        await settings.set("token", "sk-1")

        assert settings.get("limit") == 8
        assert settings.get("welcome") == "hi"  # 全局未变
        assert settings.get("welcome", group_id=20002) == "群欢迎"
        assert settings.values(20002)["welcome"] == "群欢迎"
        assert settings.values_for_api()["token"] == MASK

        again = build_settings()
        await again.load(storage)
        assert again.get("limit") == 8
        assert again.get("welcome", group_id=20002) == "群欢迎"
        assert again.values_for_api()["token"] == MASK
    finally:
        await storage.close()


async def test_update_is_atomic(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "s.db")
    await storage.connect()
    try:
        settings = build_settings()
        await settings.load(storage)
        with pytest.raises(ValueError):
            await settings.update({"limit": 3, "mode": "zzz"})
        assert settings.get("limit") == 5  # 整体回滚

        assert (await settings.update({"limit": 3, "mode": "b"}))["limit"] == 3
        assert settings.get("mode") == "b"
    finally:
        await storage.close()


async def test_unknown_key_rejected(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "s.db")
    await storage.connect()
    try:
        settings = build_settings()
        await settings.load(storage)
        with pytest.raises(ValueError):
            await settings.update({"nope": 1})
    finally:
        await storage.close()


async def test_view_binds_group(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "s.db")
    await storage.connect()
    try:
        settings = build_settings()
        await settings.load(storage)
        view = settings.view(20002)
        assert view.group_id == 20002
        await view.set("welcome", "本群专属")
        assert view.get("welcome") == "本群专属"
        assert settings.get("welcome") == "hi"
    finally:
        await storage.close()

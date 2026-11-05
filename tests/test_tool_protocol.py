"""Native tool format regressions taken from reported QQ output shapes."""

import json

import pytest

from fllbot.llm.tool_protocol import ToolProtocolError, contains_dsml, parse_dsml_tool_calls

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "github__read_repository",
            "parameters": {
                "properties": {"repository": {"type": "string"}, "paths": {"type": "array"}},
            },
        },
    }
]


def envelope(separator="|", kind="calls", name="github_read_repository"):
    return f'''<{separator}DSML{separator}{kind}>
<{separator}DSML{separator}invoke name="{name}">
<{separator}DSML{separator}parameter name="repository" string="true">https://github.com/ChiYuKe/ONI-Mods</{separator}DSML{separator}parameter>
<{separator}DSML{separator}parameter name="paths" string="false">["MadeInAbyss/Curse/AbyssTracker.cs"]</{separator}DSML{separator}parameter>
</{separator}DSML{separator}invoke>
</{separator}DSML{separator}{kind}>'''


@pytest.mark.parametrize("separator", ["|", "｜", " | | ", " ｜ ｜ "])
@pytest.mark.parametrize("kind", ["calls", "tool_calls"])
def test_reported_formats_become_authorized_calls(separator, kind):
    wire = envelope(separator, kind)
    assert contains_dsml(wire)
    text, calls = parse_dsml_tool_calls(wire, TOOLS)
    assert text == "" and len(calls) == 1
    assert calls[0]["function"]["name"] == "github__read_repository"
    assert json.loads(calls[0]["function"]["arguments"]) == {
        "repository": "https://github.com/ChiYuKe/ONI-Mods",
        "paths": ["MadeInAbyss/Curse/AbyssTracker.cs"],
    }


@pytest.mark.parametrize(
    "wire",
    [
        envelope()[:-8],
        envelope().replace('["MadeInAbyss/Curse/AbyssTracker.cs"]', "[broken"),
        envelope(name="other_tool"),
        envelope().replace("</|DSML|invoke>", "garbage</|DSML|invoke>"),
        envelope() + '<|DSML|invoke name="broken">',
    ],
)
def test_invalid_envelopes_fail_without_partial_execution(wire):
    with pytest.raises(ToolProtocolError):
        parse_dsml_tool_calls(wire, TOOLS)


def test_normal_text_is_preserved_and_no_tools_cannot_execute():
    text = "Markdown 示例：**普通文字**，`a < b`"
    assert parse_dsml_tool_calls(text, TOOLS) == (text, [])
    with pytest.raises(ToolProtocolError):
        parse_dsml_tool_calls(envelope(), [])


def test_multiple_calls_have_distinct_ids():
    _, calls = parse_dsml_tool_calls(envelope() + envelope(), TOOLS)
    assert len(calls) == 2 and calls[0]["id"] != calls[1]["id"]


@pytest.mark.parametrize("structured", [False, True])
async def test_client_normalizes_native_content_without_duplicate_calls(structured):
    import httpx

    from fllbot.config import LLMConfig
    from fllbot.llm.client import LLMClient

    def handler(request):
        message = {"content": envelope(" | | ")}
        if structured:
            message["tool_calls"] = [{"id": "native-id", "type": "function", "function": {
                "name": "github__read_repository", "arguments": '{"repository":"https://github.com/ChiYuKe/ONI-Mods"}',
            }}]
        return httpx.Response(200, json={"choices": [{"message": message, "finish_reason": "tool_calls"}]})

    config = LLMConfig(api_key="test", base_url="https://model.test", model="test", context_window=8192)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = LLMClient(lambda: config, http_client=http)
        result = await client.chat_result([{"role": "user", "content": "分析仓库"}], tools=TOOLS)
    assert result.text == "" and len(result.tool_calls) == 1
    if structured:
        assert result.tool_calls[0]["id"] == "native-id"


@pytest.mark.parametrize('calls', [
    {}, '', 0, [123], [{'function': 'invalid'}],
    [{'id': 'same', 'function': {'name': 'read'}}, {'id': 'same', 'function': {'name': 'read'}}],
])
async def test_client_rejects_malformed_native_batches(calls):
    import httpx

    from fllbot.config import LLMConfig
    from fllbot.llm.client import LLMClient, LLMError

    def handler(request):
        return httpx.Response(200, json={'choices': [{'message': {
            'content': 'candidate', 'tool_calls': calls}, 'finish_reason': 'tool_calls'}]})

    config = LLMConfig(api_key='test', base_url='https://model.test', model='test')
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = LLMClient(lambda: config, http_client=http)
        with pytest.raises(LLMError):
            await client.chat_result([{'role': 'user', 'content': 'test'}], tools=TOOLS)

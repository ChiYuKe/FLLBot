import base64
import sqlite3
from io import BytesIO
from unittest.mock import AsyncMock

import httpx
import pytest
from PIL import Image

from fllbot.app import create_app
from fllbot.history_images import MAX_IMAGE_BYTES, image_data_url, input_images, public_image_source, snapshot_images
from fllbot.onebot.models import parse_event
from fllbot.onebot.segments import MessageSegment
from fllbot.plugin.context import Context
from fllbot.storage import MIGRATIONS, Storage
from helpers import FakeConnection, group_message, make_bot


@pytest.fixture
def picture():
    buffer = BytesIO()
    Image.new('RGB', (12, 8), 'red').save(buffer, format='PNG')
    return buffer.getvalue()


async def test_snapshot_base64_and_invalid_images(picture):
    url = image_data_url(picture)
    encoded = base64.b64encode(picture).decode()
    assert await snapshot_images([
        MessageSegment.image('base64://' + encoded),
        MessageSegment.image(url),
        MessageSegment.image('data:image/svg+xml;base64,PHN2Zz4='),
        MessageSegment.image('file:///private.png'),
    ]) == [url]
    assert not image_data_url(b'not an image')
    assert not image_data_url(b'x' * (MAX_IMAGE_BYTES + 1))


async def test_snapshot_download_and_failure_fallback(picture, monkeypatch):
    original = httpx.AsyncClient
    requests = []

    def respond(request):
        requests.append(request.url.path)
        if request.url.path == '/missing':
            return httpx.Response(404)
        return httpx.Response(200, content=picture, headers={'content-type': 'image/png'})

    monkeypatch.setattr('fllbot.history_images.httpx.AsyncClient',
                        lambda **kwargs: original(transport=httpx.MockTransport(respond), **kwargs))
    monkeypatch.setattr('fllbot.history_images.public_image_source', AsyncMock(return_value=True))
    assert await snapshot_images([
        MessageSegment.image('https://images.example/missing'),
        MessageSegment.image('https://images.example/picture'),
    ]) == [image_data_url(picture)]
    assert requests == ['/missing', '/picture']


@pytest.mark.parametrize('address,allowed', [('127.0.0.1', False), ('10.0.0.1', False),
                                          ('169.254.169.254', False), ('8.8.8.8', True)])
async def test_download_rejects_local_services(address, allowed, monkeypatch):
    monkeypatch.setattr('fllbot.history_images.socket.getaddrinfo',
                        lambda *args, **kwargs: [(2, 1, 6, '', (address, 443))])
    assert await public_image_source('https://images.example/pic') is allowed
    assert not await public_image_source('https://user:password@images.example/pic')


async def test_incoming_snapshot_is_reused(picture, monkeypatch):
    from types import SimpleNamespace

    snapshot = AsyncMock(return_value=[image_data_url(picture)])
    monkeypatch.setattr('fllbot.history_images.snapshot_images', snapshot)
    ctx = SimpleNamespace(message=[], history_images=None)
    assert await input_images(ctx) == await input_images(ctx)
    snapshot.assert_awaited_once()


async def test_upgrade_preserves_text_and_defaults_images(tmp_path):
    path = tmp_path / 'old.db'
    with sqlite3.connect(path) as db:
        for version, statements in sorted(MIGRATIONS.items()):
            if version > 25:
                break
            for statement in statements:
                db.execute(statement)
        db.execute("INSERT INTO chat_messages(scene,scene_id,role,content,created_at) VALUES ('private',1,'user','旧消息',1)")
        db.execute('PRAGMA user_version=25')
    storage = Storage(path)
    await storage.connect()
    try:
        row = (await storage.recent_chat('private', 1))[0]
        assert row.content == '旧消息' and row.images == []
        assert await storage.schema_version() == 26
    finally:
        await storage.close()


async def test_incoming_and_sent_images_in_history_api_and_clear(tmp_path, picture):
    bot = await make_bot(tmp_path, llm={'enabled': True, 'api_key': 'fake'})
    await bot.permissions.save_group(20002, enabled=True, config={'policy': {'auto_group': True}})
    url = image_data_url(picture)
    event = group_message('', message_id=321)
    event['message'] = [MessageSegment.image(url).to_dict()]
    ctx = Context(bot, FakeConnection(), parse_event(event))
    try:
        await bot.chat.memory.observe(ctx)
        assert ctx.memory_message_id
        ctx.turn_input = '[图片]'
        await bot.conversations.project_input(ctx)
        private = 12345
        await bot.storage.add_chat_message('private', private, 'user', '[图片]', self_id=ctx.self_id, images=[url])
        async with bot.conversations.turn(ctx):
            assert await bot.deliveries.send_media(ctx, [MessageSegment.image(url)], '[图片]', require_ai=False)
        history = await bot.storage.recent_chat('group', ctx.group_id, self_id=ctx.self_id)
        assert [row.images for row in history] == [[url], [url]]
        assert all('base64' not in bot.chat._history_entry('group', row)['content'] for row in history)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(bot)), base_url='http://test',
                                     headers={'Authorization': 'Bearer admin-token'}) as client:
            path = f'/api/chat/group/{ctx.group_id}?self_id={ctx.self_id}'
            data = (await client.get(path)).json()
            assert data['source'] == 'memory'
            assert [row['images'] for row in data['messages']] == [[url], [url]]
            assert not (await client.get(f'/api/chat/group/{ctx.group_id}?self_id=999')).json()['messages']
            data = (await client.get(f'/api/chat/private/{private}?self_id={ctx.self_id}')).json()
            assert data['messages'][0]['images'] == [url]
            assert (await client.delete(path)).status_code == 200
            assert not (await client.get(path)).json()['messages']
            assert (await client.get(f'/api/chat/private/{private}?self_id={ctx.self_id}')).json()['messages']
    finally:
        await bot.stop()

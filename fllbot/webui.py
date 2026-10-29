"""FLLBot 自带 WebUI：单页、零外部依赖，可直接被桌面端 iframe 嵌入。

页面本身公开，但所有数据请求都带 ``Authorization: Bearer <token>``；
令牌优先取 URL 的 ``?token=``，其次 localStorage（首次带参访问后记住）。
"""

from __future__ import annotations

from . import __version__

_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>FLLBot 控制台</title>
<style>
  :root {
    color-scheme: dark;
    --bg: #14151a;
    --panel: #1c1e25;
    --line: #2c2f3a;
    --ink: #e7e9ee;
    --muted: #8b91a1;
    --accent: #7c6cf0;
    --green: #46c98b;
    --red: #f0645f;
    --yellow: #e0b552;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; padding: 20px 22px 28px;
    color: var(--ink); background: var(--bg);
    font: 13px/1.55 "Microsoft YaHei UI", "Segoe UI", system-ui, sans-serif;
  }
  header { display: flex; align-items: center; justify-content: space-between; gap: 14px; margin-bottom: 18px; }
  .brand { display: flex; align-items: baseline; gap: 10px; }
  .brand strong { font-size: 18px; letter-spacing: -.01em; }
  .brand span { color: var(--muted); font-size: 11px; }
  .status { display: inline-flex; align-items: center; gap: 6px; padding: 4px 10px; border: 1px solid var(--line); border-radius: 999px; color: var(--muted); font-size: 11px; }
  .status i { width: 7px; height: 7px; border-radius: 50%; background: var(--red); }
  .status.online i { background: var(--green); }
  .status.online { color: var(--green); border-color: color-mix(in srgb, var(--green) 40%, var(--line)); }
  .cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 10px; margin-bottom: 16px; }
  .card { padding: 12px 14px; background: var(--panel); border: 1px solid var(--line); border-radius: 10px; }
  .card span { display: block; color: var(--muted); font-size: 11px; }
  .card strong { display: block; margin-top: 5px; font-size: 19px; font-weight: 650; letter-spacing: -.01em; }
  .card small { display: block; margin-top: 3px; color: var(--muted); font-size: 10px; }
  .panel { margin-bottom: 16px; background: var(--panel); border: 1px solid var(--line); border-radius: 10px; overflow: hidden; }
  .panel-head { display: flex; align-items: center; justify-content: space-between; gap: 10px; padding: 11px 14px; border-bottom: 1px solid var(--line); }
  .panel-head h2 { margin: 0; font-size: 13px; font-weight: 650; }
  .panel-head span { color: var(--muted); font-size: 11px; }
  .list { display: grid; }
  .row { display: flex; align-items: center; gap: 12px; padding: 10px 14px; border-bottom: 1px solid var(--line); }
  .row:last-child { border-bottom: 0; }
  .row-main { min-width: 0; flex: 1; }
  .row-main strong { font-size: 12px; }
  .row-main p { margin: 2px 0 0; color: var(--muted); font-size: 11px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .tag { padding: 1px 7px; border-radius: 999px; background: color-mix(in srgb, var(--ink) 8%, transparent); color: var(--muted); font-size: 10px; }
  .tag.on { color: var(--green); background: color-mix(in srgb, var(--green) 12%, transparent); }
  .tag.off { color: var(--red); background: color-mix(in srgb, var(--red) 12%, transparent); }
  button {
    padding: 5px 11px; color: var(--ink); background: transparent;
    border: 1px solid var(--line); border-radius: 7px; font: inherit; font-size: 11px; cursor: pointer;
  }
  button:hover { border-color: var(--accent); }
  button:disabled { opacity: .5; cursor: default; }
  pre { margin: 0; padding: 12px 14px; max-height: 260px; overflow: auto; color: var(--muted); font: 11px/1.7 "Cascadia Mono", Consolas, monospace; white-space: pre-wrap; word-break: break-word; }
  #banner { display: none; margin-bottom: 14px; padding: 10px 12px; border: 1px solid color-mix(in srgb, var(--red) 45%, var(--line)); border-radius: 8px; color: var(--red); background: color-mix(in srgb, var(--red) 8%, transparent); font-size: 11px; }
  #auth { display: none; max-width: 420px; margin: 40px auto; padding: 20px; background: var(--panel); border: 1px solid var(--line); border-radius: 10px; }
  #auth h2 { margin: 0 0 6px; font-size: 15px; }
  #auth p { margin: 0 0 14px; color: var(--muted); font-size: 11px; line-height: 1.6; }
  #auth input { width: 100%; padding: 9px 11px; margin-bottom: 10px; color: var(--ink); background: var(--bg); border: 1px solid var(--line); border-radius: 7px; font: inherit; font-size: 12px; }
  .empty { padding: 16px 14px; color: var(--muted); font-size: 11px; }
</style>
</head>
<body>
<header>
  <div class="brand"><strong>FLLBot</strong><span id="version">v__VERSION__</span></div>
  <div class="status" id="status"><i></i><span>连接中…</span></div>
</header>

<div id="banner"></div>

<div id="auth">
  <h2>需要管理令牌</h2>
  <p>令牌见 FLLBot 的 <code>data/admin_token.txt</code>，或首次启动日志。它只保存在本机浏览器存储里。</p>
  <input id="auth-token" type="password" placeholder="管理令牌" autocomplete="off" spellcheck="false">
  <button id="auth-submit">保存并连接</button>
</div>

<main id="dashboard">
  <section class="cards" id="cards"></section>

  <section class="panel">
    <div class="panel-head"><h2>插件</h2><span id="plugin-summary"></span></div>
    <div class="list" id="plugins"></div>
  </section>

  <section class="panel">
    <div class="panel-head"><h2>最近日志</h2><span>内存环形缓冲</span></div>
    <pre id="logs">加载中…</pre>
  </section>
</main>

<script>
(function () {
  'use strict';
  var params = new URLSearchParams(location.search);
  var token = params.get('token') || '';
  if (token) {
    try { localStorage.setItem('fllbot_token', token); } catch (e) {}
  } else {
    try { token = localStorage.getItem('fllbot_token') || ''; } catch (e) { token = ''; }
  }

  var dashboard = document.getElementById('dashboard');
  var auth = document.getElementById('auth');
  var authInput = document.getElementById('auth-token');
  var banner = document.getElementById('banner');

  function showBanner(text) {
    banner.textContent = text;
    banner.style.display = text ? 'block' : 'none';
  }

  function h(tag, attrs) {
    var node = document.createElement(tag);
    attrs = attrs || {};
    Object.keys(attrs).forEach(function (key) {
      var value = attrs[key];
      if (value === null || value === undefined) return;
      if (key === 'class') node.className = value;
      else if (key === 'text') node.textContent = value;
      else if (key.indexOf('on') === 0 && typeof value === 'function') node.addEventListener(key.slice(2).toLowerCase(), value);
      else node.setAttribute(key, value);
    });
    for (var i = 2; i < arguments.length; i += 1) {
      var child = arguments[i];
      if (child === null || child === undefined) continue;
      node.appendChild(child instanceof Node ? child : document.createTextNode(String(child)));
    }
    return node;
  }

  function formatUptime(seconds) {
    var total = Math.max(0, Math.floor(Number(seconds) || 0));
    if (total < 60) return total + ' 秒';
    if (total < 3600) return Math.floor(total / 60) + ' 分';
    if (total < 86400) return Math.floor(total / 3600) + ' 小时';
    return Math.floor(total / 86400) + ' 天';
  }

  function formatTime(value) {
    var date = new Date(value);
    return isNaN(date.getTime()) ? String(value || '') : date.toLocaleTimeString('zh-CN', { hour12: false });
  }

  function request(path, options) {
    options = options || {};
    return fetch(path, {
      method: options.method || 'GET',
      headers: Object.assign(
        { 'Accept': 'application/json' },
        token ? { 'Authorization': 'Bearer ' + token } : {},
        options.body ? { 'Content-Type': 'application/json' } : {}
      ),
      body: options.body ? JSON.stringify(options.body) : undefined
    }).then(function (response) {
      return response.json().catch(function () { return {}; }).then(function (payload) {
        if (response.status === 401) {
          var error = new Error('管理令牌无效或缺失');
          error.unauthorized = true;
          throw error;
        }
        if (!response.ok) throw new Error(payload.detail || ('请求失败 (' + response.status + ')'));
        return payload;
      });
    });
  }

  function setStatus(online, text) {
    var node = document.getElementById('status');
    node.className = 'status' + (online ? ' online' : '');
    node.lastChild.textContent = text;
  }

  function renderCards(status) {
    var cards = document.getElementById('cards');
    cards.textContent = '';
    var connections = Array.isArray(status.connections) ? status.connections : [];
    var onlineCount = connections.filter(function (item) { return item && item.online; }).length;
    var llm = status.llm || {};
    var storage = status.storage || {};
    var dispatcher = status.dispatcher || {};
    var stats = dispatcher.stats || {};

    function card(label, value, note) {
      return h('div', { class: 'card' },
        h('span', { text: label }),
        h('strong', { text: value }),
        note ? h('small', { text: note }) : null
      );
    }

    cards.appendChild(card('运行时长', formatUptime(status.uptime_seconds), 'v' + (status.version || '?')));
    cards.appendChild(card('NapCat 连接', String(onlineCount), connections.length ? connections.length + ' 条已登记' : '等待 NapCat 连入'));
    cards.appendChild(card('插件', String((status.plugins || []).length), dispatcher.handlers ? JSON.stringify(dispatcher.handlers) : ''));
    cards.appendChild(card('消息总数', String(storage.messages || 0), 'SQLite ' + (storage.schema_version || '-')));
    cards.appendChild(card('LLM', llm.available ? '已启用' : '未启用', llm.available ? (llm.model + ' · ' + llm.requests + ' 次') : ''));
    cards.appendChild(card('分发', String(stats.handled || 0), stats.errors ? ('异常 ' + stats.errors) : '无异常'));
  }

  function renderPlugins(plugins) {
    var list = document.getElementById('plugins');
    list.textContent = '';
    document.getElementById('plugin-summary').textContent = plugins.length + ' 个，已启用 ' + plugins.filter(function (item) { return item.enabled; }).length + ' 个';
    if (!plugins.length) {
      list.appendChild(h('div', { class: 'empty', text: '还没有插件' }));
      return;
    }
    plugins.forEach(function (plugin) {
      var toggle = h('button', {
        type: 'button',
        text: plugin.enabled ? '停用' : '启用',
        onclick: function () {
          toggle.disabled = true;
          request('/api/plugins/' + encodeURIComponent(plugin.name) + '/enabled', {
            method: 'POST',
            body: { enabled: !plugin.enabled }
          }).then(refresh).catch(function (error) {
            showBanner(error.message);
          }).then(function () { toggle.disabled = false; });
        }
      });
      list.appendChild(h('div', { class: 'row' },
        h('div', { class: 'row-main' },
          h('strong', { text: plugin.name + ' ' }),
          h('span', { class: 'tag ' + (plugin.enabled ? 'on' : 'off'), text: plugin.enabled ? '已启用' : '已停用' }),
          h('p', { text: 'v' + (plugin.version || '?') + (plugin.builtin ? ' · 内置' : '') + ' · ' + (plugin.description || '无说明') })
        ),
        toggle
      ));
    });
  }

  function renderLogs(items) {
    var logs = document.getElementById('logs');
    if (!items || !items.length) {
      logs.textContent = '暂无日志';
      return;
    }
    logs.textContent = items.map(function (item) {
      return '[' + formatTime(item.ts) + '] ' + String(item.level || 'INFO').padEnd(7) + ' ' + (item.logger || '') + ': ' + (item.message || '');
    }).join('\\n');
  }

  function refresh() {
    Promise.all([
      request('/api/status'),
      request('/api/plugins'),
      request('/api/logs?limit=60').catch(function () { return { items: [] }; })
    ]).then(function (results) {
      showBanner('');
      dashboard.style.display = '';
      auth.style.display = 'none';
      setStatus(true, '运行中');
      renderCards(results[0]);
      renderPlugins(Array.isArray(results[1]) ? results[1] : []);
      renderLogs(results[2] && results[2].items);
    }).catch(function (error) {
      if (error.unauthorized) {
        dashboard.style.display = 'none';
        auth.style.display = 'block';
        setStatus(false, '需要令牌');
        return;
      }
      setStatus(false, '离线');
      showBanner(error.message || '无法连接 FLLBot');
    });
  }

  document.getElementById('auth-submit').addEventListener('click', function () {
    token = authInput.value.trim();
    try { localStorage.setItem('fllbot_token', token); } catch (e) {}
    refresh();
  });
  authInput.addEventListener('keydown', function (event) {
    if (event.key === 'Enter') document.getElementById('auth-submit').click();
  });

  if (!token) {
    dashboard.style.display = 'none';
    auth.style.display = 'block';
  } else {
    refresh();
  }
  setInterval(function () {
    if (token && document.visibilityState !== 'hidden') refresh();
  }, 5000);
})();
</script>
</body>
</html>
"""


def render_index() -> str:
    """返回控制台页面 HTML。"""
    return _HTML.replace("__VERSION__", __version__)


__all__ = ["render_index"]

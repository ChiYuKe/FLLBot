"""FLLBot 自带 WebUI：单页、零外部依赖，可直接被桌面端 iframe 嵌入。

页面本身公开，但所有数据请求都带 ``Authorization: Bearer <token>``；
令牌优先取 URL 的 ``?token=``，其次 localStorage（首次带参访问后记住）。
``?theme=light|dark`` 用于跟随桌面端主题，不传则跟随系统。
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
    --bg: #0e1014;
    --panel: #15181e;
    --panel-soft: #1a1e26;
    --line: #262b35;
    --ink: #e8eaf0;
    --muted: #8b93a1;
    --faint: #6b7280;
    --accent: #3b6fe0;
    --accent-soft: rgba(59, 111, 224, .14);
    --green: #22c55e;
    --red: #ef4444;
    --amber: #f59e0b;
  }
  :root[data-theme="light"] {
    color-scheme: light;
    --bg: #f4f5f8;
    --panel: #ffffff;
    --panel-soft: #f7f8fa;
    --line: #e6e8ee;
    --ink: #1f2430;
    --muted: #6b7280;
    --faint: #9aa1ad;
    --accent: #2f6bff;
    --accent-soft: rgba(47, 107, 255, .1);
    --green: #16a34a;
    --red: #dc2626;
    --amber: #d97706;
  }
  * { box-sizing: border-box; }
  html, body { height: 100%; }
  body {
    margin: 0;
    color: var(--ink);
    background: var(--bg);
    font: 13px/1.55 -apple-system, "Segoe UI", "Microsoft YaHei UI", system-ui, sans-serif;
    -webkit-font-smoothing: antialiased;
  }
  .topbar {
    position: sticky; top: 0; z-index: 5;
    display: flex; align-items: center; justify-content: space-between; gap: 16px;
    height: 58px; padding: 0 24px;
    background: color-mix(in srgb, var(--bg) 88%, transparent);
    border-bottom: 1px solid var(--line);
    backdrop-filter: blur(10px);
  }
  .brand { display: flex; align-items: center; gap: 10px; }
  .brand .logo {
    width: 30px; height: 30px; display: grid; place-items: center;
    color: #fff; background: var(--accent); border-radius: 9px;
    font-weight: 700; font-size: 14px;
  }
  .brand strong { display: block; font-size: 14px; letter-spacing: -.01em; }
  .brand small { display: block; color: var(--muted); font-size: 11px; }
  .topbar-right { display: flex; align-items: center; gap: 10px; }
  .status-pill {
    display: inline-flex; align-items: center; gap: 7px;
    padding: 5px 11px; color: var(--muted); font-size: 11px;
    background: var(--panel); border: 1px solid var(--line); border-radius: 999px;
  }
  .status-pill i { width: 7px; height: 7px; border-radius: 50%; background: var(--red); }
  .status-pill.online { color: var(--green); border-color: color-mix(in srgb, var(--green) 35%, var(--line)); }
  .status-pill.online i { background: var(--green); box-shadow: 0 0 0 3px color-mix(in srgb, var(--green) 16%, transparent); }

  .btn {
    display: inline-flex; align-items: center; justify-content: center; gap: 6px;
    height: 30px; padding: 0 12px; color: var(--ink); font: inherit; font-size: 11px;
    background: transparent; border: 1px solid var(--line); border-radius: 8px; cursor: pointer;
    transition: background .15s, border-color .15s, color .15s;
  }
  .btn:hover:not(:disabled) { border-color: var(--accent); color: var(--accent); }
  .btn:disabled { opacity: .55; cursor: default; }
  .btn.primary { color: #fff; background: var(--accent); border-color: var(--accent); }
  .btn.primary:hover:not(:disabled) { background: color-mix(in srgb, var(--accent) 85%, #000); color: #fff; }
  .btn.block { width: 100%; height: 36px; font-size: 12px; }

  main { width: min(1180px, 100%); margin: 0 auto; padding: 22px 24px 40px; }
  .stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(172px, 1fr)); gap: 12px; margin-bottom: 16px; }
  .stat { padding: 15px 16px 14px; background: var(--panel); border: 1px solid var(--line); border-radius: 14px; }
  .stat-head { display: flex; align-items: center; gap: 9px; color: var(--muted); font-size: 12px; }
  .stat-icon {
    width: 28px; height: 28px; display: grid; place-items: center; flex: none;
    color: var(--accent); background: var(--accent-soft); border-radius: 9px;
  }
  .stat-icon svg { width: 15px; height: 15px; }
  .stat-value { display: block; margin: 10px 0 2px; font-size: 25px; font-weight: 650; letter-spacing: -.02em; }
  .stat-note { display: block; color: var(--faint); font-size: 11px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

  .card { margin-bottom: 16px; background: var(--panel); border: 1px solid var(--line); border-radius: 16px; overflow: hidden; }
  .card-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; padding: 16px 18px 12px; }
  .card-head h2 { margin: 0; font-size: 14px; font-weight: 650; }
  .card-head p { margin: 3px 0 0; color: var(--muted); font-size: 11px; }
  .card-head .count { color: var(--muted); font-size: 11px; white-space: nowrap; }

  .list { display: grid; }
  .plugin-row { display: flex; align-items: center; gap: 14px; padding: 13px 18px; border-top: 1px solid var(--line); }
  .plugin-main { min-width: 0; flex: 1; }
  .plugin-title { display: flex; align-items: center; gap: 8px; }
  .plugin-title strong { font-size: 12.5px; }
  .tag { padding: 1px 7px; border-radius: 999px; color: var(--muted); background: var(--panel-soft); font-size: 10px; font-style: normal; }
  .tag.on { color: var(--green); background: color-mix(in srgb, var(--green) 12%, transparent); }
  .tag.off { color: var(--red); background: color-mix(in srgb, var(--red) 12%, transparent); }
  .plugin-desc { margin: 3px 0 0; color: var(--muted); font-size: 11px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .plugin-meta { display: flex; gap: 12px; margin-top: 5px; color: var(--faint); font-size: 10.5px; }

  pre { margin: 0; padding: 14px 18px 18px; max-height: 300px; overflow: auto; color: var(--muted); font: 11px/1.75 "Cascadia Mono", Consolas, monospace; white-space: pre-wrap; word-break: break-word; }

  .banner { display: none; margin: 0 0 14px; padding: 10px 13px; color: var(--red); font-size: 11px; background: color-mix(in srgb, var(--red) 8%, transparent); border: 1px solid color-mix(in srgb, var(--red) 35%, var(--line)); border-radius: 10px; }
  .empty { padding: 22px 18px; color: var(--muted); font-size: 11px; }

  #auth { display: none; width: min(400px, calc(100% - 48px)); margin: 64px auto; padding: 22px; background: var(--panel); border: 1px solid var(--line); border-radius: 16px; }
  #auth h2 { margin: 0 0 6px; font-size: 15px; }
  #auth p { margin: 0 0 14px; color: var(--muted); font-size: 11px; line-height: 1.7; }
  #auth code { padding: 1px 5px; border-radius: 5px; background: var(--panel-soft); }
  #auth input { width: 100%; height: 36px; padding: 0 11px; margin-bottom: 10px; color: var(--ink); background: var(--bg); border: 1px solid var(--line); border-radius: 8px; outline: none; font: inherit; font-size: 12px; }
  #auth input:focus { border-color: var(--accent); box-shadow: 0 0 0 3px var(--accent-soft); }
</style>
</head>
<body>
<div class="topbar">
  <div class="brand">
    <span class="logo">F</span>
    <div><strong>FLLBot</strong><small>v__VERSION__ · OneBot 11 后端</small></div>
  </div>
  <div class="topbar-right">
    <span class="status-pill" id="status"><i></i><span>连接中…</span></span>
    <button class="btn" id="refresh" type="button">刷新</button>
  </div>
</div>

<main id="dashboard">
  <div class="banner" id="banner"></div>
  <section class="stats" id="cards"></section>

  <section class="card">
    <div class="card-head"><div><h2>插件</h2><p>来自运行中的实例，可单独启用 / 停用</p></div><span class="count" id="plugin-summary"></span></div>
    <div class="list" id="plugins"></div>
  </section>

  <section class="card">
    <div class="card-head"><div><h2>最近日志</h2><p>内存环形缓冲 · 最近 60 条</p></div></div>
    <pre id="logs">加载中…</pre>
  </section>
</main>

<div id="auth">
  <h2>需要管理令牌</h2>
  <p>令牌见 FLLBot 的 <code>data/admin_token.txt</code>，或首次启动日志。它只保存在本机浏览器存储里。</p>
  <input id="auth-token" type="password" placeholder="管理令牌" autocomplete="off" spellcheck="false">
  <button class="btn primary block" id="auth-submit" type="button">保存并连接</button>
</div>

<script>
(function () {
  'use strict';
  var ICONS = {
    clock: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>',
    plug: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M9 7V3M15 7V3M7 7h10v4a5 5 0 0 1-10 0z"/><path d="M12 16v5"/></svg>',
    puzzle: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 3h3a2 2 0 0 1 2 2v2h2a2 2 0 0 1 2 2v2h-2a2 2 0 0 0 0 4h2v3a2 2 0 0 1-2 2h-3v-2a2 2 0 0 0-4 0v2H6a2 2 0 0 1-2-2v-3h2a2 2 0 0 0 0-4H4V9a2 2 0 0 1 2-2h3z"/></svg>',
    message: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12a8 8 0 0 1-8 8H7l-4 3V12a8 8 0 0 1 8-8h2a8 8 0 0 1 8 8z"/></svg>',
    spark: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3l1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8z"/></svg>',
    split: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M6 3v6a3 3 0 0 0 3 3h6"/><path d="M6 21v-6a3 3 0 0 1 3-3h6"/><path d="M15 9l3 3-3 3"/></svg>'
  };

  var params = new URLSearchParams(location.search);
  var theme = params.get('theme');
  if (theme === 'light' || theme === 'dark') document.documentElement.dataset.theme = theme;

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
      else if (key === 'html') node.innerHTML = value;
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
    node.className = 'status-pill' + (online ? ' online' : '');
    node.lastChild.textContent = text;
  }

  function stat(icon, label, value, note) {
    return h('div', { class: 'stat' },
      h('div', { class: 'stat-head' }, h('span', { class: 'stat-icon', html: ICONS[icon] }), label),
      h('strong', { class: 'stat-value', text: value }),
      h('small', { class: 'stat-note', text: note || '' })
    );
  }

  function renderCards(status) {
    var cards = document.getElementById('cards');
    cards.textContent = '';
    var connections = Array.isArray(status.connections) ? status.connections : [];
    var onlineCount = connections.filter(function (item) { return item && item.online; }).length;
    var plugins = Array.isArray(status.plugins) ? status.plugins : [];
    var enabledCount = plugins.filter(function (item) { return item && item.enabled; }).length;
    var llm = status.llm || {};
    var storage = status.storage || {};
    var stats = (status.dispatcher || {}).stats || {};

    cards.appendChild(stat('clock', '运行时长', formatUptime(status.uptime_seconds), 'v' + (status.version || '?')));
    cards.appendChild(stat('plug', 'NapCat 连接', String(onlineCount), connections.length ? connections.length + ' 条已登记' : '等待 NapCat 连入'));
    cards.appendChild(stat('puzzle', '插件', String(plugins.length), '已启用 ' + enabledCount));
    cards.appendChild(stat('message', '消息总数', String(storage.messages || 0), 'SQLite ' + (storage.schema_version || '-')));
    cards.appendChild(stat('spark', 'LLM', llm.available ? '已启用' : '未启用', llm.available ? ((llm.model || '') + ' · ' + (llm.requests || 0) + ' 次') : '未配置 API Key'));
    cards.appendChild(stat('split', '分发', String(stats.handled || 0), stats.errors ? ('异常 ' + stats.errors) : '无异常'));
  }

  function renderPlugins(plugins) {
    var list = document.getElementById('plugins');
    list.textContent = '';
    var enabled = plugins.filter(function (item) { return item.enabled; }).length;
    document.getElementById('plugin-summary').textContent = plugins.length + ' 个，已启用 ' + enabled + ' 个';
    if (!plugins.length) {
      list.appendChild(h('div', { class: 'empty', text: '还没有插件' }));
      return;
    }
    plugins.forEach(function (plugin) {
      var toggle = h('button', {
        type: 'button',
        class: plugin.enabled ? 'btn' : 'btn primary',
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
      list.appendChild(h('div', { class: 'plugin-row' },
        h('div', { class: 'plugin-main' },
          h('div', { class: 'plugin-title' },
            h('strong', { text: plugin.name }),
            h('span', { class: 'tag ' + (plugin.enabled ? 'on' : 'off'), text: plugin.enabled ? '已启用' : '已停用' }),
            plugin.builtin ? h('em', { class: 'tag', text: '内置' }) : null
          ),
          h('p', { class: 'plugin-desc', text: 'v' + (plugin.version || '?') + ' · ' + (plugin.description || '无说明') }),
          h('div', { class: 'plugin-meta' },
            h('span', { text: (plugin.settings || []).length + ' 项配置' }),
            h('span', { text: (plugin.handlers || []).length + ' 个处理器' }),
            plugin.has_readme ? h('span', { text: '有 README' }) : null
          )
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

  document.getElementById('refresh').addEventListener('click', function () {
    if (token) refresh();
  });
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


(function () {
  'use strict';
  var ICONS = {
    clock: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>',
    plug: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M9 7V3M15 7V3M7 7h10v4a5 5 0 0 1-10 0z"/><path d="M12 16v5"/></svg>',
    puzzle: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 3h3a2 2 0 0 1 2 2v2h2a2 2 0 0 1 2 2v2h-2a2 2 0 0 0 0 4h2v3a2 2 0 0 1-2 2h-3v-2a2 2 0 0 0-4 0v2H6a2 2 0 0 1-2-2v-3h2a2 2 0 0 0 0-4H4V9a2 2 0 0 1 2-2h3z"/></svg>',
    message: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12a8 8 0 0 1-8 8H7l-4 3V12a8 8 0 0 1 8-8h2a8 8 0 0 1 8 8z"/></svg>',
    spark: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3l1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8z"/></svg>',
    tool: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z"/></svg>',
    split: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M6 3v6a3 3 0 0 0 3 3h6"/><path d="M6 21v-6a3 3 0 0 1 3-3h6"/><path d="M15 9l3 3-3 3"/></svg>'
  };

  var params = new URLSearchParams(location.search);
  var paths = {
    grid: '<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/>',
    terminal: '<rect x="3" y="4" width="18" height="16" rx="3"/><path d="m7 9 3 3-3 3m6 0h4"/>',
    search: '<circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 5 5"/>',
    refresh: '<path d="M20 7v5h-5M4 17v-5h5"/><path d="M6 7a7 7 0 0 1 12-1l2 6M4 12l2 6a7 7 0 0 0 12-1"/>',
    sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2m0 16v2M2 12h2m16 0h2M5 5l1.5 1.5m11 11L19 19M5 19l1.5-1.5m11-11L19 5"/>'
  };
  Object.keys(paths).forEach(function (key) {
    ICONS[key] = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' + paths[key] + '</svg>';
  });
  document.querySelectorAll('[data-icon]').forEach(function (node) { node.innerHTML = ICONS[node.dataset.icon] || ''; });
  var theme = params.get('theme');
  if (theme !== 'light' && theme !== 'dark') {
    try { theme = localStorage.getItem('fllbot_theme'); } catch (e) {}
    if (theme !== 'light' && theme !== 'dark') theme = matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark';
  }
  if (theme === 'light' || theme === 'dark') document.documentElement.dataset.theme = theme;
  document.getElementById('theme-toggle').addEventListener('click', function () {
    theme = document.documentElement.dataset.theme === 'light' ? 'dark' : 'light';
    document.documentElement.dataset.theme = theme;
    try { localStorage.setItem('fllbot_theme', theme); } catch (e) {}
  });

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
  var allPlugins = [];
  var allLogs = [];
  var latestStatus = null;
  var pluginFilter = 'all';
  var logsPaused = false;
  var logsError = '';
  var refreshing = false;
  var needsAuth = !token;
  var busyPlugins = new Set();
  var agentSettingsSchema = [];
  var agentSettingsInitial = {};
  var settingsPlugin = null;
  var settingsSchema = [];
  var settingsInitial = {};
  var agentModelInitial = {};
  var agentModelLoaded = false;
  var agentModelLoading = false;
  var personaEditingName = '';
  var personaLoadId = 0;
  var modelCatalog = [];
  var modelFallbackWindow = 8192;
  var contextWindowConfigured = false;
  var modelCatalogSignature = '';
  var modelDiscoveryId = 0;
  var modelDiscoveryTimer;
  // One select component for static fields and schema-generated plugin settings.
  // Native selects remain the value/event source; every visible menu uses this UI.
  var selectControls = new Map();
  var activeSelectControl = null;
  var selectControlSequence = 0;

  function enhanceSelect(select) {
    if (selectControls.has(select)) return selectControls.get(select);
    var key = 'ui-select-' + (++selectControlSequence);
    var labels = Array.from(select.labels || []);
    var title = select.getAttribute('aria-label') || labels.map(function (label) { return label.textContent.trim(); }).join(' ') || '选择选项';
    var root = h('div', { class: 'ui-select' });
    var text = h('span', { class: 'ui-select-text' });
    var trigger = h('button', { id: select.id ? select.id + '-trigger' : key + '-trigger', type: 'button', class: 'ui-select-trigger', 'aria-label': title, 'aria-haspopup': 'dialog', 'aria-expanded': 'false', 'aria-controls': key + '-popup' }, text,
      h('span', { class: 'ui-select-chevron', html: '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true"><path d="m6 9 6 6 6-6"/></svg>' }));
    labels.forEach(function (label) { label.htmlFor = trigger.id; });
    var search = h('input', { type: 'text', class: 'ui-select-search-input', placeholder: '搜索' + title + '…', 'aria-label': '搜索' + title, role: 'combobox', 'aria-autocomplete': 'list', 'aria-expanded': 'false', 'aria-controls': key + '-options', autocomplete: 'off' });
    var searchRow = h('div', { class: 'ui-select-search' }, h('span', { html: ICONS.search }), search);
    var list = h('div', { id: key + '-options', class: 'ui-select-options', role: 'listbox', 'aria-label': title + '列表', tabindex: '0' });
    var count = h('span');
    var popup = h('div', { id: key + '-popup', class: 'ui-select-popup', role: 'dialog', 'aria-label': '选择' + title }, searchRow, list,
      h('div', { class: 'ui-select-footer' }, count, h('span', { text: '↑↓ 选择 · Enter 确认' })));
    popup.hidden = true;
    select.parentNode.insertBefore(root, select);
    root.appendChild(select);
    root.appendChild(trigger);
    select.hidden = true;
    select.tabIndex = -1;
    select.setAttribute('aria-hidden', 'true');
    document.body.appendChild(popup);
    var control = { select: select, root: root, trigger: trigger, popup: popup, sync: sync, close: close, position: position };
    var items = [];
    var activeIndex = 0;
    var isOpen = false;
    var searchable = false;
    var signature = '';
    selectControls.set(select, control);

    function nativeOptions() {
      return Array.from(select.options).map(function (option) {
        return { value: option.value, label: option.textContent.trim(), disabled: option.disabled || (option.parentElement.tagName === 'OPTGROUP' && option.parentElement.disabled) };
      });
    }
    function sync() {
      var options = nativeOptions();
      var next = JSON.stringify([select.value, select.disabled, options, select.id === 'agent-model' ? modelCatalog : null]);
      if (next === signature) return;
      signature = next;
      trigger.disabled = select.disabled || !options.length;
      var selected = options.find(function (option) { return option.value === select.value; });
      var caption = selected ? selected.label : '请选择' + title;
      if (text.textContent !== caption) text.textContent = caption;
      if (trigger.disabled) close(false);
      else if (isOpen) { render(); position(); }
    }
    function close(restoreFocus) {
      isOpen = false;
      popup.hidden = true;
      trigger.setAttribute('aria-expanded', 'false');
      search.setAttribute('aria-expanded', 'false');
      search.removeAttribute('aria-activedescendant');
      list.removeAttribute('aria-activedescendant');
      if (activeSelectControl === control) activeSelectControl = null;
      if (restoreFocus && !trigger.disabled && trigger.isConnected) trigger.focus({ preventScroll: true });
    }
    function position() {
      if (!isOpen) return;
      var bounds = trigger.getBoundingClientRect();
      if (bounds.bottom < 0 || bounds.top > window.innerHeight) { close(false); return; }
      var below = window.innerHeight - bounds.bottom - 14;
      var above = bounds.top - 14;
      var up = below < 240 && above > below;
      var available = up ? above : below;
      popup.style.width = Math.min(bounds.width, window.innerWidth - 16) + 'px';
      popup.style.left = Math.max(8, Math.min(bounds.left, window.innerWidth - popup.offsetWidth - 8)) + 'px';
      popup.style.setProperty('--select-menu-height', Math.max(64, Math.min(280, available - (searchable ? 88 : 40))) + 'px');
      popup.style.top = (up ? Math.max(8, bounds.top - popup.offsetHeight - 6) : bounds.bottom + 6) + 'px';
    }
    function highlight(scroll) {
      Array.from(list.querySelectorAll('[role="option"]')).forEach(function (option, index) { option.classList.toggle('is-active', index === activeIndex); });
      var node = document.getElementById(key + '-option-' + activeIndex);
      var focusTarget = searchable ? search : list;
      if (!node) { focusTarget.removeAttribute('aria-activedescendant'); return; }
      focusTarget.setAttribute('aria-activedescendant', node.id);
      if (scroll) {
        var row = node.getBoundingClientRect();
        var bounds = list.getBoundingClientRect();
        if (row.top < bounds.top) list.scrollTop -= bounds.top - row.top;
        else if (row.bottom > bounds.bottom) list.scrollTop += row.bottom - bounds.bottom;
      }
    }
    function choose(index) {
      var option = items[index];
      if (!option || option.disabled) return;
      select.value = option.value;
      sync();
      close(true);
      select.dispatchEvent(new Event('input', { bubbles: true }));
      select.dispatchEvent(new Event('change', { bubbles: true }));
    }
    function render() {
      var options = nativeOptions();
      var query = search.value.trim().toLowerCase();
      items = options.filter(function (option) { return !query || option.label.toLowerCase().includes(query) || option.value.toLowerCase().includes(query); });
      activeIndex = Math.max(0, Math.min(activeIndex, items.length - 1));
      list.textContent = '';
      items.forEach(function (option, index) {
        var model = select.id === 'agent-model' && modelCatalog && modelCatalog.find(function (entry) { return entry.id === option.value; });
        list.appendChild(h('div', { id: key + '-option-' + index, role: 'option', class: 'ui-select-option', 'aria-selected': String(option.value === select.value), 'aria-disabled': String(Boolean(option.disabled)),
          onmousedown: function (event) { event.preventDefault(); },
          onclick: function () { choose(index); },
          onmouseenter: function () { if (!option.disabled) { activeIndex = index; highlight(false); } }
        }, h('span', { class: 'ui-select-option-name', text: option.label }),
          model && model.context_window ? h('span', { class: 'ui-select-option-meta', text: model.context_window.toLocaleString('zh-CN') + ' Token' }) : null,
          h('span', { class: 'ui-select-option-check', text: option.value === select.value ? '✓' : '', 'aria-hidden': 'true' })));
      });
      if (!items.length) list.appendChild(h('div', { class: 'empty', text: '没有匹配的选项，试试其他关键词。' }));
      count.textContent = items.length + ' / ' + options.length + ' 项';
      highlight(false);
    }
    function open() {
      sync();
      if (trigger.disabled) return;
      if (activeSelectControl && activeSelectControl !== control) activeSelectControl.close(false);
      activeSelectControl = control;
      searchable = select.id === 'agent-model' || select.id === 'agent-persona' || select.id === 'agent-settings-group' || select.options.length > 8;
      searchRow.hidden = !searchable;
      search.value = '';
      activeIndex = Math.max(0, nativeOptions().findIndex(function (option) { return option.value === select.value; }));
      isOpen = true;
      popup.hidden = false;
      trigger.setAttribute('aria-expanded', 'true');
      search.setAttribute('aria-expanded', String(searchable));
      render();
      position();
      (searchable ? search : list).focus({ preventScroll: true });
      highlight(true);
    }
    function move(direction) {
      for (var step = 0; step < items.length; step += 1) {
        activeIndex = (activeIndex + direction + items.length) % items.length;
        if (!items[activeIndex].disabled) break;
      }
      highlight(true);
    }
    function keydown(event) {
      if (!isOpen && (event.key === 'ArrowDown' || event.key === 'ArrowUp')) { event.preventDefault(); open(); return; }
      if (!isOpen) return;
      if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); close(true); }
      else if (event.key === 'ArrowDown' || event.key === 'ArrowUp') { event.preventDefault(); move(event.key === 'ArrowDown' ? 1 : -1); }
      else if (event.key === 'Enter' || (event.key === ' ' && event.target === list)) { event.preventDefault(); choose(activeIndex); }
      else if (event.key === 'Tab') close(true);
      else if (!searchable && (event.key === 'Home' || event.key === 'End')) { event.preventDefault(); activeIndex = event.key === 'Home' ? 0 : items.length - 1; highlight(true); }
    }
    trigger.addEventListener('click', function () { if (isOpen) close(false); else open(); });
    root.addEventListener('keydown', keydown);
    popup.addEventListener('keydown', keydown);
    search.addEventListener('input', function () { activeIndex = 0; render(); position(); });
    select.addEventListener('change', sync);
    sync();
    return control;
  }
  function refreshSelectControls() {
    selectControls.forEach(function (control, select) {
      if (!select.isConnected) { control.close(false); control.popup.remove(); selectControls.delete(select); }
    });
    document.querySelectorAll('select').forEach(function (select) { (select.multiple && select.dataset.replyModes ? enhanceReplyModes(select) : enhanceSelect(select)).sync(); });
  }
  function syncModelPicker() {
    if (selectControls.has(document.getElementById('agent-model'))) selectControls.get(document.getElementById('agent-model')).sync();
  }
  document.addEventListener('click', function (event) {
    if (activeSelectControl && !activeSelectControl.root.contains(event.target) && !activeSelectControl.popup.contains(event.target)) activeSelectControl.close(false);
  });
  document.addEventListener('focusin', function (event) {
    if (activeSelectControl && !activeSelectControl.root.contains(event.target) && !activeSelectControl.popup.contains(event.target)) activeSelectControl.close(false);
  });
  window.addEventListener('resize', function () { if (activeSelectControl) activeSelectControl.position(); });
  document.addEventListener('scroll', function (event) {
    if (activeSelectControl && !activeSelectControl.popup.contains(event.target)) activeSelectControl.position();
  }, true);

  function modelConnectionSignature() {
    return JSON.stringify([document.getElementById('agent-base-url').value.trim(), document.getElementById('agent-api-key').value.trim()]);
  }
  function renderModelContext() {
    syncModelPicker();
    var selected = document.getElementById('agent-model').value;
    var model = modelCatalog.find(function (item) { return item.id === selected; });
    var defaultWindow = model && model.context_window ? model.context_window : modelFallbackWindow;
    var input = document.getElementById('agent-context-window');
    if (!contextWindowConfigured) input.value = defaultWindow;
    document.getElementById('agent-context-info').textContent = model && model.context_window
      ? '模型目录默认值：' + model.context_window.toLocaleString('zh-CN') + ' Token；可按服务商说明手动调整。'
      : model ? '服务商未提供窗口大小，默认保守使用 ' + modelFallbackWindow.toLocaleString('zh-CN') + ' Token；可手动调整。'
      : '默认 8,192 Token；获取模型信息后可自动填入模型目录值，也可手动修改。';
  }
  function loadAvailableModels() {
    clearTimeout(modelDiscoveryTimer);
    if (!agentModelLoaded) return;
    var base = document.getElementById('agent-base-url').value.trim();
    if (!/^https?:\/\/\S+$/i.test(base)) {
      document.getElementById('agent-models-note').textContent = '请先填写有效的模型服务地址。';
      return;
    }
    if (!document.getElementById('agent-api-key').value.trim() && !agentModelInitial.api_key && !(latestStatus && latestStatus.llm && latestStatus.llm.available)) {
      document.getElementById('agent-models-note').textContent = '填写密钥后可获取模型，也可以手动填写模型名称。';
      return;
    }
    var current = document.getElementById('agent-model').value || agentModelInitial.model;
    var signature = modelConnectionSignature();
    var id = ++modelDiscoveryId;
    var note = document.getElementById('agent-models-note');
    var select = document.getElementById('agent-model');
    select.disabled = true;
    syncModelPicker();
    document.getElementById('agent-models-refresh').disabled = true;
    note.textContent = '正在获取可用模型…';
    return request('/api/llm/models/discover', { method: 'POST', body: {
      base_url: base, api_key: document.getElementById('agent-api-key').value.trim() || null
    } }).then(function (data) {
      if (id !== modelDiscoveryId) return;
      modelCatalog = Array.isArray(data.models) ? data.models : [];
      modelFallbackWindow = Number(data.fallback_context_window) || 8192;
      modelCatalogSignature = signature;
      select.textContent = '';
      select.appendChild(h('option', { value: '', text: modelCatalog.length ? '请选择模型' : '此服务未返回可用模型' }));
      modelCatalog.forEach(function (model) { select.appendChild(h('option', { value: model.id, text: model.id })); });
      if (manualModel && document.getElementById('model-manual').value.trim()) {
        current = document.getElementById('model-manual').value.trim();
        if (!modelCatalog.some(function (model) { return model.id === current; })) select.appendChild(h('option', { value:current, text:current + '（手动填写）' }));
        select.value = current;
      }
      else if (modelCatalog.some(function (model) { return model.id === current; })) select.value = current;
      else if (modelCatalog.length === 1) select.value = modelCatalog[0].id;
      note.textContent = modelCatalog.length ? '已获取 ' + modelCatalog.length + ' 个模型，请选择要使用的模型。' : '模型列表为空，请检查服务地址与账号权限。';
      renderModelContext();
    }).catch(function (problem) {
      if (id !== modelDiscoveryId) return;
      modelCatalog = [];
      modelCatalogSignature = '';
      note.textContent = (problem.message || '模型获取失败') + '，可检查后点击“刷新模型”重试。';
      renderModelContext();
    }).finally(function () {
      if (id !== modelDiscoveryId) return;
      select.disabled = !modelCatalog.length;
      syncModelPicker();
      document.getElementById('agent-models-refresh').disabled = false;
    });
  }
  document.getElementById('agent-models-refresh').addEventListener('click', loadAvailableModels);
  document.getElementById('agent-model').addEventListener('change', function () {
    contextWindowConfigured = false;
    renderModelContext();
  });
  document.getElementById('agent-context-window').addEventListener('input', function () { contextWindowConfigured = true; });
  ['agent-base-url', 'agent-api-key'].forEach(function (id) {
    document.getElementById(id).addEventListener('input', function () {
      clearTimeout(modelDiscoveryTimer);
      modelDiscoveryId += 1;
      modelCatalog = [];
      modelCatalogSignature = '';
      document.getElementById('agent-model').disabled = true;
      syncModelPicker();
      document.getElementById('agent-models-refresh').disabled = false;
      renderModelContext();
      modelDiscoveryTimer = setTimeout(loadAvailableModels, 800);
    });
  });
  document.querySelectorAll('#agent-model-form input, #agent-model-form textarea').forEach(function (field) { field.disabled = true; });
  document.getElementById('agent-model-save').disabled = true;

  var groupDirectory = [];
  var groupDirectoryLoaded = false;
  var groupDirectoryRequest = null;

  function renderGroupScope() {
    var select = document.getElementById('agent-settings-group');
    var current = select.value;
    select.textContent = '';
    select.appendChild(h('option', { value: '', text: '全局默认配置' }));
    groupDirectory.forEach(function (group) {
      select.appendChild(h('option', { value: String(group.group_id), text: (group.group_name || '群名暂不可用') + ' · ' + group.group_id + (group.live ? '' : '（离线记录）') }));
    });
    if (current && !groupDirectory.some(function (group) { return String(group.group_id) === current; })) select.appendChild(h('option', { value: current, text: '群 ' + current + '（暂不可用）' }));
    select.value = current;
  }
  function loadGroupDirectory() {
    if (groupDirectoryRequest) return groupDirectoryRequest;
    document.getElementById('groups-refresh').disabled = true;
    groupDirectoryRequest = request('/api/groups/discover').then(function (data) {
      groupDirectory = Array.isArray(data.groups) ? data.groups : [];
      groupDirectoryLoaded = true;
      renderGroupScope(); renderManagedGroups();
    }).catch(function (problem) {
      document.getElementById('group-table-notice').textContent = '群列表读取失败：' + problem.message;
    }).finally(function () {
      groupDirectoryRequest = null;
      document.getElementById('groups-refresh').disabled = false;
    });
    return groupDirectoryRequest;
  }
  document.getElementById('groups-refresh').addEventListener('click', loadGroupDirectory);
  document.getElementById('plugin-search').addEventListener('input', function () { renderPlugins(allPlugins); });
  document.querySelectorAll('[data-filter]').forEach(function (button) {
    button.addEventListener('click', function () {
      pluginFilter = button.dataset.filter;
      document.querySelectorAll('[data-filter]').forEach(function (item) {
        item.classList.toggle('active', item === button);
        item.setAttribute('aria-pressed', String(item === button));
      });
      renderPlugins(allPlugins);
    });
  });
  ['log-level', 'log-search'].forEach(function (id) {
    document.getElementById(id).addEventListener('input', function () { renderLogs(allLogs); });
  });
  document.getElementById('logs-pause').addEventListener('click', function () {
    logsPaused = !logsPaused;
    this.textContent = logsPaused ? '继续更新' : '暂停更新';
    this.setAttribute('aria-pressed', String(logsPaused));
    if (!logsPaused) refresh();
    renderLogs(allLogs);
  });

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
      cache: 'no-store',
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
    document.getElementById('last-sync').dataset.stale = String(!online);
  }

  function stat(icon, label, value, note) {
    return h('div', { class: 'stat' },
      h('div', { class: 'stat-head' }, h('span', { class: 'stat-icon', html: ICONS[icon] }), h('span', {class:'stat-label', text:label})),
      h('strong', { class: 'stat-value', text: value }),
      h('small', { class: 'stat-note', text: note || '' })
    );
  }

  function renderCards(status) {
    var cards = document.getElementById('cards');
    cards.textContent = '';
    var connections = Array.isArray(status.connections) ? status.connections : [];
    var onlineCount = connections.filter(function (item) { return item && item.online; }).length;
    var extensions = Array.isArray(status.plugins) ? status.plugins : [];
    var plugins = extensions;
    var enabledCount = plugins.filter(function (item) { return item && item.enabled; }).length;
    var llm = status.llm || {};
    var storage = status.storage || {};
    var stats = (status.dispatcher || {}).stats || {};

    cards.appendChild(stat('plug', '在线账号', String(onlineCount), connections.length ? '共 ' + connections.length + ' 个连接' : '等待 NapCat 连入'));
    cards.appendChild(stat('message', '消息总数', String(storage.messages || 0), 'SQLite ' + (storage.schema_version || '-')));
    cards.appendChild(stat('clock', '运行时长', formatUptime(status.uptime_seconds), 'v' + (status.version || '?')));
    cards.appendChild(stat('puzzle', '已启用插件', String(enabledCount), '共 ' + plugins.length + ' 个插件'));
    var list = document.getElementById('connections');
    list.textContent = '';
    document.getElementById('connection-count').textContent = onlineCount + ' / ' + connections.length + ' 在线';
    if (!connections.length) list.appendChild(h('div', { class: 'empty', text: '尚无账号连接 · 等待 NapCat 连入' }));
    connections.forEach(function (item) {
      list.appendChild(h('div', { class: 'connection' },
        h('span', { class: 'connection-avatar', html: ICONS.plug }),
        h('div', { class: 'connection-info' }, h('strong', { text: item.self_id || '未识别账号' }), h('small', { text: (item.client_name || 'NapCat') + ' · 已接收 ' + (item.events_received || 0) + ' 个事件' })),
        h('span', { class: 'tag ' + (item.online ? 'on' : 'off'), text: item.online ? '在线' : '离线' })
      ));
    });
    var badge = document.getElementById('llm-badge');
    badge.textContent = llm.available ? '已就绪' : '未就绪';
    badge.className = 'tag ' + (llm.available ? 'on' : 'off');
    var details = document.getElementById('ai-details');
    details.textContent = '';
    [['当前模型', llm.model || '尚未配置'], ['当前人格', llm.persona || '默认'], ['累计请求', (llm.requests || 0) + ' 次'], ['最近响应', llm.requests ? (llm.last_latency_ms || 0) + ' ms' : '等待首次请求'], ['消息分发', (stats.handled || 0) + ' 次 · 异常 ' + (stats.errors || 0)]].forEach(function (row) {
      details.appendChild(h('div', {}, h('dt', { text: row[0] }), h('dd', { text: row[1] })));
    });
  }

  function renderPlugins(plugins) {
    var list = document.getElementById('plugins');
    list.textContent = '';
    var enabled = plugins.filter(function (item) { return item.enabled; }).length;
    document.getElementById('plugin-summary').textContent = plugins.length + ' 个，已启用 ' + enabled + ' 个';
    var query = document.getElementById('plugin-search').value.trim().toLowerCase();
    var filtered = plugins.filter(function (plugin) {
      return (pluginFilter === 'all' || Boolean(plugin.enabled) === (pluginFilter === 'enabled')) &&
        (String(plugin.name || '') + ' ' + (plugin.description || '')).toLowerCase().includes(query);
    });
    if (!filtered.length) {
      list.appendChild(h('div', { class: 'empty', text: plugins.length ? '没有匹配的插件，试试其他关键词或状态' : '还没有插件 · 加载后将在这里显示' }));
      return;
    }
    filtered.forEach(function (plugin) {
      var toggle = h('button', {
        type: 'button',
        class: 'switch',
        role: 'switch',
        'aria-checked': String(Boolean(plugin.enabled)),
        'aria-label': (plugin.enabled ? '停用 ' : '启用 ') + plugin.name,
        onclick: function () {
          busyPlugins.add(plugin.name);
          toggle.disabled = true;
          request('/api/plugins/' + encodeURIComponent(plugin.name) + '/enabled', {
            method: 'POST',
            body: { enabled: !plugin.enabled }
          }).then(function () {
            plugin.enabled = !plugin.enabled;
            return refresh();
          }).catch(function (error) {
            showBanner(error.message);
          }).finally(function () { busyPlugins.delete(plugin.name); renderPlugins(allPlugins); });
        }
      });
      toggle.disabled = busyPlugins.has(plugin.name);
      list.appendChild(h('div', { class: 'plugin-row' },
        h('div', { class: 'plugin-icon', text: String(plugin.name || 'P').slice(0, 1).toUpperCase(), 'aria-hidden': 'true' }),
        h('div', { class: 'plugin-main' },
          h('div', { class: 'plugin-title' },
            h('strong', { text: plugin.name }),
            h('span', { class: 'tag ' + (plugin.enabled ? 'on' : 'off'), text: plugin.enabled ? '已启用' : '已停用' }),
            plugin.builtin ? h('em', { class: 'tag', text: '内置' }) : null
          ),
          h('p', { class: 'plugin-desc', text: 'v' + (plugin.version || '?') + ' · ' + (plugin.description || '无说明') }),
          h('div', { class: 'plugin-meta' },
            h('span', { text: plugin.name === 'agent' ? '独立配置页' : (plugin.settings || []).length + ' 项配置' }),
            h('span', { text: (plugin.handlers || []).length + ' 个处理器' }),
            (plugin.ai_tools || []).length ? h('span', { text: (plugin.ai_tools || []).length + ' 个工具' }) : null,
            plugin.has_readme ? h('span', { text: '有 README' }) : null
          ),
        h('div', { class: 'plugin-actions' },
          (plugin.settings || []).length && plugin.name !== 'agent' ? h('button', {
            type: 'button',
            class: 'btn',
            text: '配置',
            onclick: function () { openPluginSettings(plugin); }
          }) : null,
          plugin.has_readme ? h('button', {
            type: 'button',
            class: 'btn',
            text: '说明',
            onclick: function () { openReadme(plugin); }
          }) : null
        )),
        toggle
      ));
    });
  }

  function toolCatalog() {
    var tools = [];
    (allPlugins || []).forEach(function (plugin) {
      if (plugin.name === 'agent') return;
      (plugin.ai_tools || []).forEach(function (tool) {
        tools.push({
          id: tool.name,
          title: tool.title || tool.name,
          description: tool.description || '',
          plugin: plugin.name,
          pluginEnabled: Boolean(plugin.enabled)
        });
      });
    });
    return tools;
  }

  function toolTitle(id) {
    var tool = toolCatalog().find(function (item) { return item.id === id; });
    return tool ? tool.title : String(id);
  }

  function renderToolPicker(node, selected, disabled, onToggle) {
    var tools = toolCatalog();
    node.textContent = '';
    tools.forEach(function (tool) {
      var check = h('input', { type: 'checkbox' });
      check.checked = selected.indexOf(tool.id) >= 0;
      check.disabled = Boolean(disabled);
      check.addEventListener('change', function () { onToggle(tool.id, check.checked); });
      node.appendChild(h('label', { class: 'tool-choice', title: tool.description || '', 'data-tool': tool.id },
        check,
        h('span', {},
          h('strong', { text: tool.title }),
          !tool.pluginEnabled ? h('small', { text: '工具未启用' }) : null
        )
      ));
    });
    selected.filter(function (id) {
      return !tools.some(function (tool) { return tool.id === id; });
    }).forEach(function (id) {
      node.appendChild(h('p', { class: 'settings-note', text: id + '：工具未加载或已移除，已保留原设置。' }));
    });
    if (!tools.length) {
      node.appendChild(h('p', { class: 'settings-note', text: '暂无可用工具；启用提供 Agent 工具的插件后会显示可选项。' }));
    }
    return tools;
  }

  function escapeHtml(text) {
    return String(text)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  function inlineMd(text) {
    var value = escapeHtml(text);
    value = value.replace(/`([^`]+)`/g, function (_, code) { return '<code>' + code + '</code>'; });
    value = value.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
    value = value.replace(/(^|[^*])\*([^*]+)\*/g, '$1<em>$2</em>');
    value = value.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, function (match, label, href) {
      if (!/^https?:\/\//i.test(href)) return match;
      return '<a href="' + href + '" target="_blank" rel="noreferrer noopener">' + label + '</a>';
    });
    return value;
  }

  function renderMarkdown(source) {
    var text = String(source || '').replace(/\r\n?/g, '\n').trim();
    if (!text) return '<p class="empty">这个插件还没有说明</p>';
    var blocks = [];
    var stash = function (html) {
      blocks.push(html);
      return '@@BLOCK' + (blocks.length - 1) + '@@';
    };
    text = text.replace(/```[^\n]*\n([\s\S]*?)```/g, function (_, code) {
      return stash('<pre><code>' + escapeHtml(code.replace(/\n$/, '')) + '</code></pre>');
    });
    text = text.replace(/(?:^\|.*\|[ \t]*\n?)+/gm, function (block) {
      var rows = block.trim().split('\n').map(function (row) { return row.trim(); }).filter(Boolean);
      if (rows.length < 2 || !/^\|[\s:|-]+\|$/.test(rows[1])) return block;
      var cells = function (row) { return row.replace(/^\|/, '').replace(/\|$/, '').split('|').map(function (c) { return c.trim(); }); };
      var head = cells(rows[0]).map(function (c) { return '<th>' + inlineMd(c) + '</th>'; }).join('');
      var body = rows.slice(2).map(function (row) {
        return '<tr>' + cells(row).map(function (c) { return '<td>' + inlineMd(c) + '</td>'; }).join('') + '</tr>';
      }).join('');
      return stash('<table><thead><tr>' + head + '</tr></thead><tbody>' + body + '</tbody></table>') + '\n';
    });

    var out = [];
    var list = '';
    var paragraph = [];
    var quote = [];
    var flush = function () {
      if (paragraph.length) { out.push('<p>' + inlineMd(paragraph.join(' ')) + '</p>'); paragraph = []; }
      if (quote.length) { out.push('<blockquote>' + inlineMd(quote.join(' ')) + '</blockquote>'); quote = []; }
      if (list) { out.push('</' + list + '>'); list = ''; }
    };

    text.split('\n').forEach(function (raw) {
      var line = raw.trimEnd();
      var block = line.match(/^@@BLOCK(\d+)@@$/);
      if (block) {
        flush();
        out.push(blocks[Number(block[1])]);
        return;
      }
      if (!line.trim()) { flush(); return; }
      var heading = line.match(/^(#{1,6})\s+(.*)$/);
      if (heading) {
        flush();
        var level = Math.min(heading[1].length, 4);
        out.push('<h' + level + '>' + inlineMd(heading[2]) + '</h' + level + '>');
        return;
      }
      if (/^(-{3,}|\*{3,}|_{3,})$/.test(line.trim())) { flush(); out.push('<hr />'); return; }
      if (line.indexOf('>') === 0) { flush(); quote.push(line.replace(/^>\s?/, '')); return; }
      var bullet = line.match(/^[-*+]\s+(.*)$/);
      var ordered = line.match(/^\d+[.)]\s+(.*)$/);
      if (bullet || ordered) {
        if (quote.length || paragraph.length) flush();
        var kind = bullet ? 'ul' : 'ol';
        if (list !== kind) {
          if (list) out.push('</' + list + '>');
          out.push('<' + kind + '>');
          list = kind;
        }
        out.push('<li>' + inlineMd((bullet || ordered)[1]) + '</li>');
        return;
      }
      if (quote.length) flush();
      paragraph.push(line.trim());
    });
    flush();
    return out.join('\n');
  }

  function closePluginSettings() {
    document.getElementById('plugin-settings').classList.remove('open');
    settingsPlugin = null;
    if (settingsTrigger && settingsTrigger.isConnected) settingsTrigger.focus();
    else document.getElementById('plugin-search').focus();
  }

  var settingsTrigger = null;
  function settingsGroupId() {
    var value = document.getElementById('settings-group').value.trim();
    if (!value) return null;
    if (!/^\d+$/.test(value) || Number(value) <= 0) throw new Error('群号必须是正整数');
    return Number(value);
  }

  function renderPluginSettings(data, formId) {
    var schema = Array.isArray(data.schema) ? data.schema : (formId ? agentSettingsSchema : settingsSchema);
    var initial = Object.assign({}, data.values || {});
    if (formId) { agentSettingsSchema = schema; agentSettingsInitial = initial; }
    else { settingsSchema = schema; settingsInitial = initial; }
    var groupId = data.group_id;
    var form = document.getElementById(formId || 'settings-form');
    form.textContent = '';
    document.getElementById('settings-note').textContent = groupId
      ? '当前显示该群的生效值；仅可按群设置的字段可在此覆盖。'
      : '这是全局默认配置；群级覆盖可在群号框中填写群号后编辑。';
    schema.forEach(function (field) {
      var wrap = h('div', { class: 'setting-field' + (field.type === 'bool' ? ' is-check' : '') });
      var id = (formId ? 'agent-setting-' : 'setting-') + String(field.key).replace(/[^A-Za-z0-9_-]/g, '-');
      var value = initial[field.key];
      var control;
      if (field.type === 'bool') {
        control = h('input', { id: id, type: 'checkbox', 'data-key': field.key, 'data-type': field.type });
        control.checked = Boolean(value);
      } else if (field.type === 'select') {
        control = h('select', { id: id, 'data-key': field.key, 'data-type': field.type });
        (field.options || []).forEach(function (option) {
          var caption = field.key === 'trigger_mode' ? ({at:'仅被 @ 时回复',prefix:'听到唤醒词时回复',all:'自动参与群聊'})[option] || String(option) : String(option);
          control.appendChild(h('option', { value: String(option), text: caption }));
        });
        control.value = String(value == null ? '' : value);
      } else if (field.type === 'list' || field.type === 'multiselect' || field.type === 'text') {
        control = h('textarea', { id: id, 'data-key': field.key, 'data-type': field.type });
        control.value = Array.isArray(value) ? value.join('\n') : String(value == null ? '' : value);
      } else {
        var inputType = field.secret ? 'password' : (field.type === 'int' || field.type === 'float' ? 'number' : 'text');
        control = h('input', {
          id: id,
          type: inputType,
          'data-key': field.key,
          'data-type': field.type,
          min: field.min,
          max: field.max,
          step: field.type === 'float' ? 'any' : (field.type === 'int' ? '1' : null),
          placeholder: field.secret ? '留空表示保持当前密钥' : null
        });
        control.value = field.secret && value === '******' ? '' : String(value == null ? '' : value);
      }
      if (groupId && !field.group_scoped) control.disabled = true;
      if (field.type === 'bool') {
        wrap.appendChild(control);
        wrap.appendChild(h('label', { for: id, text: field.label || field.key }));
      } else {
        wrap.appendChild(h('label', { for: id, text: field.label || field.key }));
        wrap.appendChild(control);
      }
      if (field.description) wrap.appendChild(h('small', { text: field.description }));
      form.appendChild(wrap);
    });
  }

  function agentGroupId() {
    var value = document.getElementById('agent-settings-group').value.trim();
    if (!value) return null;
    if (!/^\d+$/.test(value) || Number(value) <= 0) throw new Error('群号必须是正整数');
    return Number(value);
  }

  function renderAgentSettings(data) {
    renderPluginSettings(data, 'agent-settings-form');
    var form = document.getElementById('agent-settings-form');
    var source = document.createElement('div');
    while (form.firstChild) source.appendChild(form.firstChild);
    var groups = [
      { title: '群聊响应', description: '控制智能体何时处理本群消息。', keys: ['auto_group', 'trigger_mode', 'trigger_prefix', 'cooldown_seconds'] },
      { title: '工具', description: '选择模型可以调用的工具。工具由插件提供，执行时仍检查插件开关与用户权限。', keys: ['allowed_tools', 'max_tool_rounds'] }
    ];
    var fields = {};
    while (source.firstChild) {
      var node = source.firstChild;
      source.removeChild(node);
      var control = node.querySelector('[data-key]');
      if (control) fields[control.dataset.key] = node;
      else form.appendChild(node);
    }
    groups.forEach(function (group) {
      var items = group.keys.map(function (key) { return fields[key]; }).filter(Boolean);
      if (!items.length) return;
      var section = h('section', { class: 'agent-setting-group' },
        h('h3', { text: group.title }),
        h('p', { text: group.description })
      );
      items.forEach(function (item) { section.appendChild(item); });
      form.appendChild(section);
    });
    Object.keys(fields).forEach(function (key) {
      if (!groups.some(function (group) { return group.keys.includes(key); })) form.appendChild(fields[key]);
    });
    var groupId = data.group_id;
    document.getElementById('agent-settings-note').textContent = groupId
      ? '正在编辑该群的生效配置；只有标记为可按群设置的参数可在此覆盖。'
      : '群聊自然语言响应默认关闭。启用后，该群普通消息及启用后的会话历史会发送到已配置的模型服务。';
    if (data.group_policy) document.getElementById('agent-settings-note').textContent += ' 该群有独立设置，部分字段会被覆盖；请到“配置 → 群聊设置”查看。';
    renderToolChoices();
    renderAgentStatus();
  }

  function renderAgentStatus() {
    var badge = document.getElementById('agent-status');
    var entry = allPlugins.find(function (item) { return item.name === 'agent'; });
    var llmReady = latestStatus && latestStatus.llm && latestStatus.llm.available;
    var enabled = agentSettingsInitial.auto_group === true;
    if (!entry || !entry.enabled) {
      badge.textContent = '插件已停用';
      badge.className = 'tag off';
    } else if (!llmReady) {
      badge.textContent = '模型未就绪';
      badge.className = 'tag off';
    } else {
      badge.textContent = enabled ? '群聊响应已开启' : '群聊响应已关闭';
      badge.className = 'tag ' + (enabled ? 'on' : 'off');
    }
  }

  function renderAgentModel(data) {
    var root = data && data.config;
    var llm = (root && root.llm) || (data && data.llm) || (root && root.config && root.config.llm);
    if (!llm || typeof llm !== 'object') throw new Error('配置接口没有返回 llm 模型设置');
    agentModelInitial = {
      enabled: Boolean(llm.enabled),
      base_url: String(llm.base_url || ''),
      api_key: String(llm.api_key || ''),
      model: String(llm.model || ''),
      context_window: Number(llm.context_window || 0),
      context_mode: llm.context_mode || 'custom',
      max_tokens: Number(llm.max_tokens || 800),
      temperature: Number(llm.temperature == null ? 0.8 : llm.temperature),
      persona: String(llm.persona || 'fllbot'),
      system_extra: String(llm.system_extra || '')
    };
    document.getElementById('agent-llm-enabled').checked = agentModelInitial.enabled;
    document.getElementById('agent-base-url').value = agentModelInitial.base_url;
    document.getElementById('agent-api-key').value = '';
    document.getElementById('agent-api-key-state').textContent = agentModelInitial.api_key
      ? '当前密钥已隐藏；留空保持不变。环境变量密钥优先级更高。'
      : '未在配置文件中填写密钥；也可通过环境变量提供。';
    var modelSelect = document.getElementById('agent-model');
    modelSelect.textContent = '';
    modelSelect.appendChild(h('option', { value: agentModelInitial.model, text: (agentModelInitial.model || '尚未选择模型') + '（等待获取）' }));
    syncModelPicker();
    document.getElementById('agent-max-tokens').value = agentModelInitial.max_tokens;
    contextWindowConfigured = agentModelInitial.context_mode === 'custom' && agentModelInitial.context_window > 0;
    document.getElementById('agent-context-window').value = agentModelInitial.context_window || modelFallbackWindow;
    document.getElementById('agent-temperature').value = agentModelInitial.temperature;
    document.getElementById('agent-system-extra').value = agentModelInitial.system_extra;
    document.getElementById('model-manual').value = agentModelInitial.model;
  }

  function loadPersonaDetail(name) {
    if (!name) return Promise.resolve();
    var requestId = ++personaLoadId;
    document.getElementById('agent-persona-error').textContent = '正在读取人格…';
    return request('/api/personas/' + encodeURIComponent(name)).then(function (persona) {
      if (requestId !== personaLoadId) return;
      personaEditingName = persona.editable ? persona.name : '';
      document.getElementById('agent-persona-name').value = persona.name || '';
      document.getElementById('agent-persona-name').readOnly = Boolean(persona.editable);
      document.getElementById('agent-persona-title').value = persona.title || '';
      document.getElementById('agent-persona-description').value = persona.description || '';
      document.getElementById('agent-persona-content').value = persona.content || '';
      document.getElementById('agent-persona-delete').disabled = !persona.editable || persona.name === agentModelInitial.persona;
      document.getElementById('agent-persona-error').textContent = persona.editable
        ? '正在编辑自定义人格。'
        : '这是内置人格；修改人格标识后保存，可另存为自定义人格。';
    }).catch(function (problem) {
      if (requestId === personaLoadId) document.getElementById('agent-persona-error').textContent = problem.message || '读取人格失败';
    });
  }

  function loadPersonaCatalog(selectedName) {
    return request('/api/personas').then(function (personas) {
      var select = document.getElementById('agent-persona');
      select.textContent = '';
      (Array.isArray(personas) ? personas : []).forEach(function (persona) {
        select.appendChild(h('option', { value: persona.name, text: (persona.title || persona.name) + ' · ' + persona.name }));
      });
      if (selectedName && !Array.from(select.options).some(function (option) { return option.value === selectedName; })) {
        select.appendChild(h('option', { value: selectedName, text: selectedName + '（未找到，将回退默认人格）' }));
      }
      if (selectedName) select.value = selectedName;
      if (!select.value && select.options.length) select.selectedIndex = 0;
      return loadPersonaDetail(select.value);
    }).catch(function (problem) {
      document.getElementById('agent-persona-error').textContent = problem.message || '读取人格列表失败';
    });
  }

  document.getElementById('agent-persona').addEventListener('change', function () {
    loadPersonaDetail(this.value);
  });
  document.getElementById('agent-persona-new').addEventListener('click', function () {
    personaLoadId += 1;
    personaEditingName = '';
    document.getElementById('agent-persona-name').readOnly = false;
    document.getElementById('agent-persona-name').value = '';
    document.getElementById('agent-persona-title').value = '';
    document.getElementById('agent-persona-description').value = '';
    document.getElementById('agent-persona-content').value = '';
    document.getElementById('agent-persona-delete').disabled = true;
    document.getElementById('agent-persona-error').textContent = '填写人格标识和提示词后保存。';
    document.getElementById('agent-persona-name').focus();
  });
  document.getElementById('agent-persona-save').addEventListener('click', function () {
    var name = document.getElementById('agent-persona-name').value.trim();
    var payload = {
      title: document.getElementById('agent-persona-title').value.trim(),
      description: document.getElementById('agent-persona-description').value.trim(),
      content: document.getElementById('agent-persona-content').value.trim()
    };
    if (!/^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/.test(name)) {
      document.getElementById('agent-persona-error').textContent = '人格标识格式不正确。';
      return;
    }
    if (!payload.content) {
      document.getElementById('agent-persona-error').textContent = '人格提示词不能为空。';
      return;
    }
    if (personaEditingName && personaEditingName !== name) {
      document.getElementById('agent-persona-error').textContent = '已保存的人格标识不可直接修改，请点击“新建人格”另存。';
      return;
    }
    var button = this;
    var selectedDefault = document.getElementById('agent-persona').value;
    var creating = !personaEditingName;
    button.disabled = true;
    document.getElementById('agent-persona-error').textContent = '正在保存人格…';
    request('/api/personas/' + encodeURIComponent(name), { method: 'PUT', body: payload })
      .then(function () {
        return loadPersonaCatalog(creating ? name : selectedDefault);
      })
      .then(function () { document.getElementById('agent-persona-error').textContent = creating
        ? '人格已保存并选中；点击“设为默认人格”后生效。'
        : '人格已保存。'; })
      .catch(function (problem) { document.getElementById('agent-persona-error').textContent = problem.message || '保存人格失败'; })
      .finally(function () { button.disabled = false; });
  });
  document.getElementById('agent-persona-delete').addEventListener('click', function () {
    var name = personaEditingName;
    if (!name || name === agentModelInitial.persona) return;
    if (!window.confirm('确定删除人格“' + name + '”？此操作不可撤销。')) return;
    var button = this;
    button.disabled = true;
    request('/api/personas/' + encodeURIComponent(name), { method: 'DELETE' })
      .then(function () { return loadPersonaCatalog(agentModelInitial.persona); })
      .catch(function (problem) { document.getElementById('agent-persona-error').textContent = problem.message || '删除人格失败'; })
      .finally(function () { button.disabled = !personaEditingName || personaEditingName === agentModelInitial.persona; });
  });

  function loadAgentModel() {
    if (agentModelLoading) return;
    agentModelLoading = true;
    agentModelLoaded = false;
    document.getElementById('agent-model').disabled = true;
    syncModelPicker();
    document.getElementById('agent-model-save').disabled = true;
    document.querySelectorAll('#agent-model-form input, #agent-model-form textarea').forEach(function (field) { field.disabled = true; });
    document.getElementById('agent-model-error').textContent = '正在读取模型配置…';
    return request('/api/config')
      .then(function (data) {
        renderAgentModel(data);
        agentModelLoaded = true;
        document.getElementById('agent-model-error').textContent = '';
        loadAvailableModels();
        if (!dirtyPages.has('personas')) loadPersonaCatalog(agentModelInitial.persona);
      })
      .catch(function (problem) {
        agentModelLoaded = false;
        document.getElementById('agent-model-error').textContent = problem.message || '读取模型配置失败';
      })
      .finally(function () {
        agentModelLoading = false;
        document.querySelectorAll('#agent-model-form input, #agent-model-form textarea').forEach(function (field) { field.disabled = !agentModelLoaded; });
        document.getElementById('agent-model-reload').disabled = false;
        document.getElementById('agent-model-save').disabled = !agentModelLoaded;
      });
  }

  document.getElementById('agent-model-reload').addEventListener('click', function () {
    loadAgentModel().then(function () { if (agentModelLoaded) markSaved('models'); });
  });
  document.getElementById('agent-model-save').addEventListener('click', function () {
    if (!agentModelLoaded) {
      document.getElementById('agent-model-error').textContent = '模型配置尚未成功读取，请重新读取后再保存。';
      return;
    }
    var current = {
      enabled: document.getElementById('agent-llm-enabled').checked,
      base_url: document.getElementById('agent-base-url').value.trim(),
      model: document.getElementById('agent-model').value.trim(),
      context_mode: contextWindowConfigured ? 'custom' : 'model',
      context_window: Number(document.getElementById('agent-context-window').value),
      max_tokens: Number(document.getElementById('agent-max-tokens').value),
      temperature: Number(document.getElementById('agent-temperature').value),
      persona: agentModelInitial.persona,
      system_extra: document.getElementById('agent-system-extra').value
    };
    var values = {};
    Object.keys(current).forEach(function (key) {
      if (JSON.stringify(current[key]) !== JSON.stringify(agentModelInitial[key])) values[key] = current[key];
    });
    var apiKey = document.getElementById('agent-api-key').value.trim();
    if (apiKey) values.api_key = apiKey;
    if (!/^https?:\/\/\S+$/i.test(current.base_url)) {
      document.getElementById('agent-model-error').textContent = '服务地址必须以 http:// 或 https:// 开头。';
      return;
    }
    if (!current.model) {
      document.getElementById('agent-model-error').textContent = '请先选择模型，或点击“手动填写”输入模型名称。';
      return;
    }
    if (!Number.isInteger(current.max_tokens) || current.max_tokens < 1 || current.max_tokens > 32768) {
      document.getElementById('agent-model-error').textContent = '最大回复 Token 必须是正整数。';
      return;
    }
    if (!Number.isInteger(current.context_window) || current.context_window < 1 || current.context_window > 10000000) {
      document.getElementById('agent-model-error').textContent = '上下文长度必须是 1 到 10,000,000 之间的正整数。';
      return;
    }
    if (!Number.isFinite(current.temperature) || current.temperature < 0 || current.temperature > 2) {
      document.getElementById('agent-model-error').textContent = 'Temperature 需在 0 到 2 之间。';
      return;
    }
    if (!Object.keys(values).length) { document.getElementById('agent-model-error').textContent = '没有需要保存的改动。'; return; }
    var save = document.getElementById('agent-model-save');
    save.disabled = true;
    document.getElementById('agent-model-error').textContent = '正在保存…';
    request('/api/config', { method: 'PATCH', body: { llm: values } })
      .then(function () { markSaved('models'); return loadAgentModel(); })
      .then(function () { toast('模型设置已保存'); return refresh(); })
      .catch(function (problem) { document.getElementById('agent-model-error').textContent = problem.message || '保存模型配置失败'; })
      .finally(function () { save.disabled = false; });
  });

  function loadAgentSettings() {
    var error = document.getElementById('agent-settings-error');
    var groupId;
    try { groupId = agentGroupId(); }
    catch (problem) { error.textContent = problem.message; return; }
    error.textContent = '正在读取配置…';
    var suffix = groupId === null ? '' : '?group_id=' + encodeURIComponent(groupId);
    request('/api/plugins/agent/settings' + suffix)
      .then(function (data) {
        error.textContent = '';
        renderAgentSettings(data);
        loadedPages.add('agent');
        markSaved('agent');
      })
      .catch(function (problem) { error.textContent = problem.message || '读取智能体配置失败'; });
  }

  document.getElementById('agent-settings-group').addEventListener('change', loadAgentSettings);
  document.getElementById('agent-settings-reload').addEventListener('click', loadAgentSettings);
  document.getElementById('agent-settings-save').addEventListener('click', function () {
    var values = {};
    document.querySelectorAll('#agent-settings-form [data-key]').forEach(function (control) {
      if (control.disabled) return;
      var key = control.dataset.key;
      var type = control.dataset.type;
      var value;
      if (type === 'bool') value = control.checked;
      else if (type === 'list' || type === 'multiselect') value = control.value.split(/[,\n]/).map(function (part) { return part.trim(); }).filter(Boolean);
      else if (type === 'int' || type === 'float') value = control.value === '' ? null : Number(control.value);
      else value = control.value;
      var field = agentSettingsSchema.find(function (item) { return item.key === key; });
      if (field && field.secret && value === '') return;
      if (JSON.stringify(value) !== JSON.stringify(agentSettingsInitial[key])) values[key] = value;
    });
    if (!Object.keys(values).length) { document.getElementById('agent-settings-error').textContent = '没有需要保存的改动。'; return; }
    var body = { values: values };
    try {
      var groupId = agentGroupId();
      if (groupId !== null) body.group_id = groupId;
    } catch (problem) { document.getElementById('agent-settings-error').textContent = problem.message; return; }
    var save = document.getElementById('agent-settings-save');
    save.disabled = true;
    document.getElementById('agent-settings-error').textContent = '正在保存…';
    request('/api/plugins/agent/settings', { method: 'PATCH', body: body })
      .then(function (data) {
        document.getElementById('agent-settings-error').textContent = '';
        renderAgentSettings(data);
        return refresh();
      })
      .catch(function (problem) { document.getElementById('agent-settings-error').textContent = problem.message || '保存失败'; })
      .finally(function () { save.disabled = false; });
  });

  function loadPluginSettings() {
    var plugin = settingsPlugin;
    if (!plugin) return;
    var error = document.getElementById('settings-error');
    var groupId;
    try { groupId = settingsGroupId(); }
    catch (problem) { error.textContent = problem.message; return; }
    error.textContent = '正在读取配置…';
    var suffix = groupId === null ? '' : '?group_id=' + encodeURIComponent(groupId);
    request('/api/plugins/' + encodeURIComponent(plugin.name) + '/settings' + suffix)
      .then(function (data) {
        if (settingsPlugin !== plugin) return;
        error.textContent = '';
        renderPluginSettings(data);
      })
      .catch(function (problem) { if (settingsPlugin === plugin) error.textContent = problem.message || '读取配置失败'; });
  }

  function openPluginSettings(plugin) {
    settingsTrigger = document.activeElement;
    settingsPlugin = plugin;
    document.querySelector("#plugin-settings .eyebrow").textContent="插件配置";
    document.getElementById('settings-title').textContent = plugin.name + ' 插件配置';
    document.getElementById('settings-sub').textContent = 'v' + (plugin.version || '?') + ' · ' + (plugin.description || '');
    document.getElementById('settings-group').value = '';
    document.getElementById('settings-error').textContent = '';
    document.getElementById('settings-form').textContent = '';
    document.getElementById('plugin-settings').classList.add('open');
    document.getElementById('settings-close').focus();
    loadPluginSettings();
  }

  document.getElementById('settings-group').addEventListener('change', loadPluginSettings);
  document.getElementById('settings-close').addEventListener('click', closePluginSettings);
  document.getElementById('settings-cancel').addEventListener('click', closePluginSettings);
  document.getElementById('plugin-settings').addEventListener('click', function (event) {
    if (event.target === event.currentTarget) closePluginSettings();
  });
  document.getElementById('plugin-settings').addEventListener('keydown', function (event) {
    if (event.key === 'Escape') { closePluginSettings(); return; }
    if (event.key !== 'Tab') return;
    var focusable = this.querySelectorAll('button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled)');
    var first = focusable[0];
    var last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
  });
  document.getElementById('settings-save').addEventListener('click', function () {
    if (!settingsPlugin) return;
    var values = {};
    var controls = document.querySelectorAll('#settings-form [data-key]');
    controls.forEach(function (control) {
      if (control.disabled) return;
      var key = control.dataset.key;
      var type = control.dataset.type;
      var value;
      if (type === 'bool') value = control.checked;
      else if (type === 'list' || type === 'multiselect') value = control.value.split(/[,\n]/).map(function (part) { return part.trim(); }).filter(Boolean);
      else if (type === 'int' || type === 'float') value = control.value === '' ? null : Number(control.value);
      else value = control.value;
      var field = settingsSchema.find(function (item) { return item.key === key; });
      if (field && field.secret && value === '') return;
      if (JSON.stringify(value) !== JSON.stringify(settingsInitial[key])) values[key] = value;
    });
    if (!Object.keys(values).length) { document.getElementById('settings-error').textContent = '没有需要保存的改动。'; return; }
    var body = { values: values };
    try {
      var groupId = settingsGroupId();
      if (groupId !== null) body.group_id = groupId;
    } catch (problem) { document.getElementById('settings-error').textContent = problem.message; return; }
    var save = document.getElementById('settings-save');
    save.disabled = true;
    document.getElementById('settings-error').textContent = '正在保存…';
    request('/api/plugins/' + encodeURIComponent(settingsPlugin.name) + '/settings', { method: 'PATCH', body: body })
      .then(function (data) {
        if (settingsPlugin) renderPluginSettings(data);
        closePluginSettings();
        return refresh();
      })
      .catch(function (problem) { document.getElementById('settings-error').textContent = problem.message || '保存失败'; })
      .finally(function () { save.disabled = false; });
  });

  var readmeTrigger = null;
  var readmeRequest = 0;
  function closeReadme() {
    document.getElementById('readme').classList.remove('open');
    readmeRequest += 1;
    if (readmeTrigger && readmeTrigger.isConnected) readmeTrigger.focus();
    else document.getElementById('plugin-search').focus();
  }

  function openReadme(plugin) {
    readmeTrigger = document.activeElement;
    document.querySelector('#readme .eyebrow').textContent='插件说明';
    var requestId = ++readmeRequest;
    var backdrop = document.getElementById('readme');
    var body = document.getElementById('readme-body');
    document.getElementById('readme-title').textContent = plugin.name;
    document.getElementById('readme-sub').textContent = 'v' + (plugin.version || '?') + ' · ' + (plugin.description || '');
    body.textContent = '正在读取 README…';
    backdrop.classList.add('open');
    document.getElementById('readme-close').focus();
    request('/api/plugins/' + encodeURIComponent(plugin.name) + '/readme')
      .then(function (data) { if (requestId === readmeRequest) body.innerHTML = renderMarkdown(data && data.content); })
      .catch(function (error) { if (requestId === readmeRequest) body.textContent = error.message || '读取 README 失败'; });
  }

  document.getElementById('readme-close').addEventListener('click', closeReadme);
  document.getElementById('readme').addEventListener('click', function (event) {
    if (event.target === event.currentTarget) closeReadme();
  });
  document.addEventListener('keydown', function (event) {
    var modal = document.getElementById('readme');
    if (!modal.classList.contains('open')) return;
    if (event.key === 'Escape') closeReadme();
    if (event.key === 'Tab') {
      var focusable = modal.querySelectorAll('button, a[href]');
      var first = focusable[0];
      var last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    }
  });

  function renderLogs(items) {
    var logs = document.getElementById('logs');
    var nearBottom = logs.scrollHeight - logs.scrollTop - logs.clientHeight < 32;
    var previousScroll = logs.scrollTop;
    var level = document.getElementById('log-level').value;
    var query = document.getElementById('log-search').value.trim().toLowerCase();
    var filtered = (items || []).filter(function (item) {
      var current = String(item.level || 'INFO').toUpperCase();
      return (level === 'all' || current === level || (level === 'ERROR' && current === 'CRITICAL')) &&
        ((item.logger || '') + ' ' + (item.message || '')).toLowerCase().includes(query);
    });
    document.getElementById('log-summary').textContent = logsError ? '日志获取失败 · 下次同步时重试' : (logsPaused ? '已暂停更新' : '自动更新') + ' · 显示 ' + filtered.length + ' / ' + (items || []).length + ' 条';
    logs.textContent = '';
    if (!filtered.length) {
      logs.appendChild(h('div', { class: 'empty', text: logsError || (items && items.length ? '没有匹配的日志' : '暂无日志 · 新的运行记录将在这里显示') }));
      return;
    }
    filtered.forEach(function (item) {
      var current = String(item.level || 'INFO').toUpperCase();
      var kind = current === 'ERROR' || current === 'CRITICAL' ? 'error' : current === 'WARNING' ? 'warning' : '';
      logs.appendChild(h('div', { class: 'log-line ' + kind }, h('span', { class: 'log-time', text: formatTime(item.ts) }), h('span', { class: 'log-level', text: current }), h('span', { class: 'log-message', text: (item.logger ? item.logger + ': ' : '') + (item.message || '') })));
    });
    logs.scrollTop = nearBottom ? logs.scrollHeight : previousScroll;
  }

  function refresh() {
    if (refreshing) return Promise.resolve();
    refreshing = true;
    document.getElementById('refresh').disabled = true;
    document.getElementById('auth-submit').disabled = true;
    return Promise.all([
      request('/api/status'),
      request('/api/plugins'),
      logsPaused ? Promise.resolve(null) : request('/api/logs?limit=60').catch(function (error) {
        if (error.unauthorized) throw error;
        return { error: error.message };
      })
    ]).then(function (results) {
      showBanner('');
      dashboard.style.display = '';
      auth.style.display = 'none';
      needsAuth = false;
      document.getElementById('auth-error').textContent = '';
      setStatus(true, results[0].started ? '服务运行中' : '服务已连接');
      document.getElementById('last-sync').textContent = formatTime(new Date()) + ' 已同步';
      document.getElementById('footer-sync').textContent = document.getElementById('last-sync').textContent;
      latestStatus = results[0];
      renderCards(results[0]);
      renderChecklist(results[0]);
      if (!consoleReady) { consoleReady = true; navigate(location.hash.slice(1) || 'overview'); }
      allPlugins = Array.isArray(results[1]) ? results[1] : [];
      renderAgentStatus();
      // Do not replace a focused switch or README button during background polling.
      if (!document.getElementById('plugins').contains(document.activeElement) &&
          !document.getElementById('readme').classList.contains('open') &&
          !document.getElementById('plugin-settings').classList.contains('open')) renderPlugins(allPlugins);
      if (document.body.dataset.view === 'agent') updateDirtyState();
      if (results[2]) {
        logsError = results[2].error || '';
        if (!logsError) allLogs = results[2].items || [];
      }
      renderLogs(allLogs);
    }).catch(function (error) {
      if (error.unauthorized) {
        dashboard.style.display = 'none';
        auth.style.display = 'block';
        needsAuth = true;
        document.getElementById('auth-error').textContent = '令牌无效或已过期，请重新输入。';
        setStatus(false, '需要令牌');
        return;
      }
      setStatus(false, '离线');
      document.getElementById('last-sync').textContent = '同步失败 · 等待重连';
      document.getElementById('footer-sync').textContent = '同步失败 · 等待重连';
      if (needsAuth) document.getElementById('auth-error').textContent = error.message || '无法连接 FLLBot';
      showBanner(error.message || '无法连接 FLLBot');
    }).finally(function () {
      refreshing = false;
      document.getElementById('refresh').disabled = false;
      document.getElementById('auth-submit').disabled = false;
    });
  }

  document.getElementById('refresh').addEventListener('click', function () {
    if (token && !needsAuth) refresh();
  });
  document.getElementById('auth-submit').addEventListener('click', function () {
    token = authInput.value.trim();
    if (!token) { document.getElementById('auth-error').textContent = '请先输入管理令牌。'; return; }
    try { localStorage.setItem('fllbot_token', token); } catch (e) {}
    consoleReady = false;
    refresh();
  });
  authInput.addEventListener('keydown', function (event) {
    if (event.key === 'Enter') document.getElementById('auth-submit').click();
  });

  /* CONSOLE_FEATURES */

  refreshSelectControls();
  var selectObserver = new MutationObserver(refreshSelectControls);
  selectObserver.observe(document.body, { childList: true, subtree: true, attributes: true, attributeFilter: ['disabled', 'selected', 'value', 'label'] });

  if (!token) {
    dashboard.style.display = 'none';
    auth.style.display = 'block';
  } else {
    refresh();
  }
  setInterval(function () {
    if (token && !needsAuth && document.visibilityState !== 'hidden') refresh();
  }, 5000);
})();

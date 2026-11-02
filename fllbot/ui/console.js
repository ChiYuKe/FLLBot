// This module is composed inside app.js so existing plugin editors share the API client.
var consoleReady = false;
var dirtyPages = new Set();
var loadedPages = new Set();
var historyRooms = [];
var activeRoom = null;
var historyRequestId = 0;
var managedRequestId = 0;
var managedBotId = null;
var managedRevision = null;
var systemInitial = null;
var systemRevision = null;
var toastTimer;
var manualModel = false;
var setupGuideRequestId=0,setupGuideUpdated=0;
function loadSetupGuide(force) {
  if(!force&&Date.now()-setupGuideUpdated<5000)return;
  var group=document.getElementById('setup-guide-group').value.trim(),bot=document.getElementById('setup-guide-bot').value;
  var note=document.getElementById('setup-guide-note');
  if(group&&(!/^\d+$/.test(group)||!Number.isSafeInteger(Number(group))||Number(group)<=0)){note.textContent='请填写有效的正整数群号。';return;}
  var id=++setupGuideRequestId,query=[];setupGuideUpdated=Date.now();
  if(group)query.push('group_id='+group);if(bot)query.push('self_id='+bot);
  request('/api/console/setup'+(query.length?'?'+query.join('&'):'')).then(function(data){
    if(id!==setupGuideRequestId)return;
    fillOptions('setup-guide-bot',data.accounts,'自动选择机器人','self_id',function(account){return (account.nickname||account.self_id)+(account.online?' · 在线':' · 离线');});
    document.getElementById('setup-guide-bot').value=bot;
    document.getElementById('setup-guide-progress').textContent=data.completed_checks+' / '+data.total_checks+' 项必要配置检查通过'+(data.configuration_ready?' · 等待真实群验证':'');
    var node=document.getElementById('setup-guide-steps');node.textContent='';
    data.steps.forEach(function(step){node.appendChild(h('button',{type:'button',class:'check-item',onclick:function(){
      navigate(step.target);
      if(step.target==='groups'&&data.group_id)loadGroupDirectory().then(function(){document.getElementById('group-list-view').value='all';document.getElementById('group-table-search').value=String(data.group_id);loadGroupTable();});
    }},h('span',{class:'check-dot'+(step.done?'':' pending'),text:step.done?'✓':step.optional?'选填':'↗'}),h('strong',{text:step.title}),h('small',{text:step.detail})));});
    document.getElementById('setup-guide-verification').textContent=data.verification.title+'：'+data.verification.detail;
    note.textContent=data.note;refreshSelectControls();
  }).catch(function(error){if(id===setupGuideRequestId)note.textContent='读取引导失败：'+error.message;setupGuideUpdated=0;});
}
document.getElementById('setup-guide-refresh').addEventListener('click',function(){loadSetupGuide(true);});
document.getElementById('setup-guide-bot').addEventListener('change',function(){loadSetupGuide(true);});
document.getElementById('setup-guide-group').addEventListener('change',function(){loadSetupGuide(true);});
var viewInfo = {
  bots: ['聊天默认参数', '管理聊天输入、唤醒与回复输出。'],
  accounts: ['账号默认设置', '按机器人账号维护回复方式和资源；群单独配置优先。'],
  overview: ['概览', '查看机器人连接、消息和运行状态。'],
  groups: ['群聊设置', '选一个群，设置它如何回复。'],
  tools: ['工具', '选择 Agent 可以调用的具体能力。'],
  plugins: ['插件', '查看已安装的功能、配置和使用说明。'],
  models: ['模型接入', '维护可供默认行为和群策略选择的模型服务。'],
  personas: ['人格库', '人格全部由你创建，可重复使用并在群策略中选择。'],
  agent: ['默认回复规则', '设置群聊使用的默认回复方式，独立群策略优先。'],
  playground: ['对话测试', '发送之前，先在这里试一试。'],
  history: ['会话记录', '查看和管理机器人保存的对话上下文。'],
  usage: ['用量统计', '了解消息量、模型请求与 Token 消耗。'],
  logs: ['运行日志', '筛选运行记录，定位连接和回复问题。'],
  traces: ['消息诊断', '按账号和会话查看消息处理的阶段、耗时与原因。'],
  system: ['系统设置', '管理连接方式、机器人行为和访问权限。'],
  services: ['外部服务', '查看插件声明的服务状态，按需检查健康接口。'],
  maintenance: ['备份与恢复', '创建、校验和下载数据库备份，查看停机恢复指引。'],
  retention: ['数据保留与清理', '先预览范围与数量，再确认清理；自动保留默认关闭。'],
  tasks: ['任务中心', '查看插件任务与运行记录；执行前重新检查连接和权限。']
};
ICONS.settings = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"><path d="M4 6h16M4 12h16M4 18h16"/><circle cx="8" cy="6" r="2" fill="var(--panel)"/><circle cx="16" cy="12" r="2" fill="var(--panel)"/><circle cx="10" cy="18" r="2" fill="var(--panel)"/></svg>';
document.querySelectorAll('[data-icon="settings"]').forEach(function (node) { node.innerHTML = ICONS.settings; });

function toast(message) {
  var node = document.getElementById('toast');
  node.textContent = message;
  node.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(function () { node.hidden = true; }, 3500);
}
function updateDirtyState() {
  var view = document.body.dataset.view;
  var heading = viewInfo[view];
  document.getElementById('page-description').textContent = heading[1] + (dirtyPages.has(view) ? ' · 有未保存的更改' : '');
  if(typeof renderAdminPageContext==='function')renderAdminPageContext();
}
function markSaved(view) { dirtyPages.delete(view); updateDirtyState(); }
document.getElementById('dashboard').addEventListener('input', function (event) {
  if (event.target.closest('.resource-replacement')) return;
  if (!event.target.closest('form,.persona-editor')) return;
  if (['bots', 'accounts', 'models', 'personas', 'agent', 'groups', 'system'].includes(document.body.dataset.view)) {
    dirtyPages.add(document.body.dataset.view); updateDirtyState();
  }
});
// Keep a configuration draft when changing pages; warn before leaving the browser.
window.addEventListener('beforeunload', function (event) {
  if (dirtyPages.size) { event.preventDefault(); event.returnValue = ''; }
});
var baseRequest = request;
request = function (path, options) {
  return baseRequest(path, options).then(function (data) {
    if (options && options.method && options.method !== 'GET') {
      if (path.indexOf('/api/personas/') === 0) markSaved('personas');
      else if (path === '/api/plugins/agent/settings') { markSaved('agent'); toast('默认行为设置已保存'); }
      else if (path.indexOf('/api/console/groups/') === 0) markSaved('groups');
    }
    return data;
  });
};

function renderChecklist(status) {
  loadSetupGuide(false);
  loadOverviewTrend();
  loadOverviewUsage();
  var node = document.getElementById('setup-checklist');
  var connected = (status.connections || []).some(function (item) { return item.online; });
  var agent = (status.plugins || []).find(function (item) { return item.name === 'agent'; });
  var failed = Object.keys(status.failed_plugins || {});
  document.getElementById('plugin-failures').hidden = !failed.length;
  var failureList = document.getElementById('plugin-failure-list'); failureList.textContent = '';
  failed.forEach(function (name) { failureList.appendChild(h('div', { class:'setting-field' }, h('strong', { text:name }), h('pre', { text:String(status.failed_plugins[name]) }))); });
  var checks = [
    ['账号连接', connected ? 'NapCat 已连接' : '连接 NapCat 后接收消息', connected, 'system'],
    ['模型服务', status.llm && status.llm.available ? '模型配置已就绪' : '填写服务地址和密钥', status.llm && status.llm.available, 'models'],
    ['群聊与回复', agent && agent.enabled ? '检查群范围和响应开关' : '请先启用 agent 插件', false, 'groups'],
    ['插件状态', failed.length ? failed.length + ' 个插件加载失败' : '已加载插件运行正常', !failed.length, 'plugins']
  ];
  node.textContent = '';
  checks.forEach(function (item) {
    node.appendChild(h('button', { type:'button', class:'check-item', onclick:function () { navigate(item[3]); } },
      h('span', { class:'check-dot' + (item[2] ? '' : ' pending'), text:item[2] ? '✓' : '↗' }),
      h('strong', { text:item[0] }), h('small', { text:item[1] })));
  });
  // More detailed response checks include per-group overrides and global switches.
  if (!loadedPages.has('readiness')) {
    loadedPages.add('readiness');
    request('/api/console/readiness').then(function (data) {
      var entry = node.children[2];
      if (entry) {
        var count = (data.ready_groups || []).length;
        var configuredCount = (data.responding_groups || []).length;
        var ready = count > 0;
        entry.querySelector('small').textContent = ready ? count + ' 个已登记群通过连接与配置检查' : configuredCount ? '群配置已就绪，请检查账号连接' : '群聊响应尚未就绪，点击配置';
        entry.querySelector('.check-dot').classList.toggle('pending', !ready);
        entry.querySelector('.check-dot').textContent = ready ? '✓' : '↗';
      }
    }).catch(function () {}).finally(function () { setTimeout(function () { loadedPages.delete('readiness'); }, 4500); });
  }
}

var overviewTrendUpdated = 0;
var overviewTrendSeries = [];
var overviewTrendFrame = 0;
function renderOverviewTrend() {
  var node = document.getElementById('overview-trend');
  if (!node || !overviewTrendSeries.length || !node.clientWidth) return;
  var series = overviewTrendSeries;
  var width = node.clientWidth, height = node.clientHeight || 200;
  var maximum = Math.max.apply(null, series.map(function (item) { return item.count; }).concat([4]));
  var scale = Math.ceil(maximum / 4) * 4;
  var left = Math.max(34, String(scale).length * 7 + 14), right = 22, top = 14, bottom = 30;
  var plotWidth = Math.max(1, width - left - right), plotHeight = Math.max(1, height - top - bottom);
  function svgNode(tag, attrs, text) {
    var element = document.createElementNS('http://www.w3.org/2000/svg', tag);
    Object.keys(attrs).forEach(function (key) { element.setAttribute(key, attrs[key]); });
    if (text != null) element.textContent = text;
    return element;
  }
  // Match SVG units to CSS pixels instead of stretching a fixed-width drawing.
  var svg = svgNode('svg', { viewBox: '0 0 ' + width + ' ' + height, width: width, height: height, preserveAspectRatio: 'xMidYMid meet', 'aria-hidden': 'true' });
  for (var tick = 0; tick <= 4; tick++) {
    var y = top + plotHeight * tick / 4;
    svg.appendChild(svgNode('line', { x1: left, x2: width - right, y1: y, y2: y, stroke: 'var(--line)', 'stroke-width': 1 }));
    svg.appendChild(svgNode('text', { x: left - 10, y: y + 4, fill: 'var(--faint)', 'font-size': 11, 'text-anchor': 'end' }, scale * (4 - tick) / 4));
  }
  var points = series.map(function (item, index) { return [left + plotWidth * index / (series.length - 1), top + plotHeight * (1 - item.count / scale)]; });
  svg.appendChild(svgNode('polyline', { points: points.map(function (point) { return point.join(','); }).join(' '), fill: 'none', stroke: '#4c9cd2', 'stroke-width': 1.6, 'vector-effect': 'non-scaling-stroke' }));
  var labelStep = Math.max(1, Math.ceil((series.length - 1) / Math.max(1, Math.floor(plotWidth / 72))));
  series.forEach(function (item, index) {
    var point = points[index];
    var dot = svgNode('circle', { cx: point[0], cy: point[1], r: 3, fill: '#4c9cd2' });
    dot.appendChild(svgNode('title', {}, item.day + '：' + item.count + ' 条'));
    svg.appendChild(dot);
    var last = series.length - 1;
    if (index === last || index % labelStep === 0 && (last - index) * plotWidth / last >= 58) {
      svg.appendChild(svgNode('text', { x: point[0], y: height - 5, fill: 'var(--faint)', 'font-size': 11, 'text-anchor': 'middle' }, item.day.slice(5)));
    }
  });
  node.replaceChildren(svg);
}
function scheduleOverviewTrend() {
  cancelAnimationFrame(overviewTrendFrame);
  overviewTrendFrame = requestAnimationFrame(renderOverviewTrend);
}
if (typeof ResizeObserver !== 'undefined') {
  var overviewTrendObserver = new ResizeObserver(scheduleOverviewTrend);
  overviewTrendObserver.observe(document.getElementById('overview-trend'));
} else {
  window.addEventListener('resize', scheduleOverviewTrend);
}
function loadOverviewTrend() {
  if (document.body.dataset.view !== 'overview' || Date.now() - overviewTrendUpdated < 30000) return;
  overviewTrendUpdated = Date.now();
  request('/api/stats?days=14').then(function (data) {
    var byDay = {};
    (data.daily || []).forEach(function (item) { byDay[item.day] = Number(item.count) || 0; });
    var series = [];
    for (var offset=13; offset>=0; offset--) {
      var date = new Date(); date.setDate(date.getDate()-offset);
      var day = date.getFullYear()+'-'+String(date.getMonth()+1).padStart(2,'0')+'-'+String(date.getDate()).padStart(2,'0');
      series.push({ day:day, count:byDay[day] || 0 });
    }
    overviewTrendSeries = series;
    renderOverviewTrend();
    var total=series.reduce(function(sum,item){return sum+item.count;},0);
    var summary=document.getElementById('overview-trend-summary');summary.textContent='';summary.appendChild(h('h3',{text:'消息统计'}));
    [['近 14 天',total,''],['活跃日期',series.filter(function(item){return item.count>0;}).length,'green'],['单日最高',Math.max.apply(null,series.map(function(item){return item.count;})),'purple']].forEach(function(row){summary.appendChild(h('div',{},h('span',{},h('i',{class:'trend-dot '+row[2]}),row[0]),h('strong',{text:row[1]})));});
    summary.appendChild(h('small',{text:'消息按服务器日期统计'}));
  }).catch(function(error){document.getElementById('overview-trend-summary').textContent=error.message;overviewTrendUpdated=0;});
}

function loadProviders() {
  return Promise.all([request('/api/llm/providers'), request('/api/config')]).then(function (data) {
    var items = data[0], config = data[1].config.llm, configRevision=data[1].revision;
    var node = document.getElementById('provider-list'); node.textContent = '';
    items.forEach(function (item) {
      var details = h('details', {class:'advanced'});
      details.appendChild(h('summary', {text:item.name + ' · ' + item.model + (item.is_default ? ' · 默认' : '')}));
      details.appendChild(h('p', {class:'settings-note', text:item.base_url + ' · ' + (item.configured ? '已配置密钥' : '缺少密钥')}));
      var references=h('details',{class:'advanced'}),referenceList=h('div',{class:'resource-references'});
      references.appendChild(h('summary',{text:'引用位置'}));references.appendChild(referenceList);
      references.addEventListener('toggle',function(){if(references.open)loadResourceReferences(referenceList,'provider',item.name).catch(function(){});});
      details.appendChild(references);
      var form = h('form', {class:'settings-form'}), grid = h('div', {class:'form-grid'}), fields = {};
      [['context_window','上下文窗口（留空自动获取）',1,10000000], ['max_tokens','回复长度上限',1,1000000],
       ['image_token_budget','每张图片预留 Token',1,1000000], ['max_concurrency','同时请求上限',1,100],
       ['input_price','输入单价／百万 Token',0,null], ['output_price','输出单价／百万 Token',0,null]].forEach(function (spec) {
        var input = h('input', {type:'number', min:spec[2], max:spec[3], step:spec[0].indexOf('price') >= 0 ? 'any' : '1', 'aria-label':spec[1]});
        input.value = item[spec[0]] == null ? '' : item[spec[0]];
        fields[spec[0]] = input;
        grid.appendChild(h('label', {class:'setting-field'}, h('span', {text:spec[1]}), input));
      });
      [['supports_tools','支持工具调用'], ['supports_images','支持识图'], ['supports_temperature','发送随机性参数']].forEach(function (spec) {
        var input = h('input', {type:'checkbox'}); input.checked = item[spec[0]];
        fields[spec[0]] = input;
        grid.appendChild(h('label', {class:'setting-field is-check'}, input, h('span', {text:spec[1]})));
      });
      var parameter = h('select', {'aria-label':'回复长度参数'}, h('option', {value:'max_tokens', text:'标准回复长度参数'}), h('option', {value:'max_completion_tokens', text:'推理模型回复长度参数'}));
      parameter.value = item.token_parameter; fields.token_parameter = parameter;
      grid.appendChild(h('label', {class:'setting-field'}, h('span', {text:'回复长度参数'}), parameter));
      var save = h('button', {type:'submit',class:'btn primary',text:'保存此模型能力'});
      var probe = h('button', {type:'button',class:'btn',text:'诊断已保存的模型'});
      var note = h('div', {class:'settings-note',role:'status'});
      form.appendChild(grid);
      form.appendChild(h('p', {class:'settings-note',text:'图片预算是预留量，可按服务商说明调整。诊断会分别验证对话、工具和识图，会产生用量；不会执行插件或向 QQ 发消息。'}));
      form.appendChild(h('div', {class:'settings-footer'}, save, probe)); form.appendChild(note);
      form.addEventListener('submit', function (event) {
        event.preventDefault(); if (!form.reportValidity()) return;
        var values = {};
        Object.keys(fields).forEach(function (key) {
          var input = fields[key];
          var value = input.type === 'checkbox' ? input.checked : key === 'token_parameter' ? input.value : input.value === '' && key === 'context_window' ? null : Number(input.value);
          if (value !== item[key]) values[key] = value;
        });
        if (!Object.keys(values).length) { note.textContent = '没有需要保存的改动'; return; }
        var patch = values;
        if (Object.keys(config.providers || {}).length) { patch = {providers:{}}; patch.providers[item.name] = values; }
        save.disabled = true;
        request('/api/config', {method:'PATCH',headers:configRevision?{'If-Match':configRevision}:{},body:{llm:patch}}).then(function (result) {
          configRevision=result.revision;
          Object.assign(item, values); markSaved('models'); note.textContent = '模型能力已保存'; toast('模型能力已保存');
          return loadAgentModel();
        }).catch(function (error) { note.textContent = error.message; }).finally(function () { save.disabled = false; });
      });
      probe.addEventListener('click', function () {
        probe.disabled = true; note.textContent = '正在逐项诊断已保存的模型…';
        request('/api/llm/diagnose', {method:'POST',body:{provider:item.name}}).then(function (result) {
          var names = {chat:'普通对话',tools:'工具调用',images:'图片识别'}, states = {passed:'通过',failed:'失败',skipped:'跳过'};
          note.textContent = '';
          result.checks.forEach(function (check) { note.appendChild(h('p', {text:names[check.kind] + ' · ' + states[check.status] + ' · ' + check.latency_ms + ' ms · ' + check.detail})); });
        }).catch(function (error) { note.textContent = error.message; }).finally(function () { probe.disabled = false; });
      });
      details.appendChild(form); node.appendChild(details);
    });
    var form = h('form', {class:'settings-form'}), grid = h('div', {class:'form-grid'});
    var token = h('input', {type:'number', min:0, step:1, 'aria-label':'每群每日 Token 上限'}); token.value = config.group_daily_token_limit || 0;
    var cost = h('input', {type:'number', min:0, step:'any', 'aria-label':'每群每日费用上限'}); cost.value = config.group_daily_cost_limit || 0;
    var fallback = h('input', {type:'text', 'aria-label':'备用模型服务', placeholder:'按顺序填写服务名称，用逗号分隔'}); fallback.value = (config.fallback_providers || []).join(', ');
    grid.appendChild(h('label', {class:'setting-field'}, h('span', {text:'每群每日 Token 上限（0 为不限）'}), token));
    grid.appendChild(h('label', {class:'setting-field'}, h('span', {text:'每群每日费用上限（0 为不限）'}), cost));
    grid.appendChild(h('label', {class:'setting-field'}, h('span', {text:'备用模型服务'}), fallback));
    var save = h('button', {type:'submit',class:'btn primary',text:'保存额度与备用服务'}), note = h('p', {class:'settings-note',role:'status'});
    form.appendChild(h('h3', {text:'额度与故障恢复'})); form.appendChild(grid);
    form.appendChild(h('p', {class:'settings-note',text:'额度对每个群分别累计，按服务器日期重置，并预留本轮回复预算。费用使用配置单价估算，需为主模型和备用模型填写单价。遇到临时服务故障才尝试备用模型。'}));
    form.appendChild(save); form.appendChild(note);
    form.addEventListener('submit', function (event) {
      event.preventDefault(); if (!form.reportValidity()) return;
      var names = fallback.value.split(/[,，]/).map(function (value) {return value.trim();}).filter(Boolean);
      if (names.some(function (name) {return !items.some(function (item) {return item.name === name;});})) { note.textContent = '备用服务名称必须是上面已有的模型服务'; return; }
      save.disabled = true;
      request('/api/config', {method:'PATCH',headers:configRevision?{'If-Match':configRevision}:{},body:{llm:{group_daily_token_limit:Number(token.value),group_daily_cost_limit:Number(cost.value),fallback_providers:names}}}).then(function (result) {
        configRevision=result.revision;
        markSaved('models'); note.textContent = '额度与备用服务已保存'; toast('设置已保存');
      }).catch(function (error) {note.textContent = error.message;}).finally(function () {save.disabled = false;});
    });
    node.appendChild(form);
  }).catch(function (error) { document.getElementById('provider-list').textContent = error.message; });
}

function loadResourceReferences(node,kind,name) {
  var requestId=String(Number(node.dataset.requestId||0)+1);node.dataset.requestId=requestId;
  node.textContent=name?'正在读取引用…':'保存资源后可查看引用。';
  if(!name)return Promise.resolve(null);
  return request('/api/console/resources/'+kind+'/'+encodeURIComponent(name)+'/references').then(function(data){
    if(node.dataset.requestId!==requestId)return null;
    node.textContent='';
    node.appendChild(h('p',{class:'settings-note',text:data.count?'共 '+data.count+' 处直接引用；继承此资源的群不重复计数。':'暂无直接引用。'}));
    data.items.forEach(function(item){
      node.appendChild(h('div',{class:'resource-reference-row'},h('span',{text:item.label}),h('button',{type:'button',class:'btn',text:'前往修改',onclick:function(){
        if(item.scope==='bot'){showAccountDefaults(item.object_id);return;}
        navigate(item.target);
        if(item.scope==='group')loadGroupDirectory().then(function(){
          document.getElementById('group-list-view').value='all';
          document.getElementById('group-table-search').value=String(item.object_id);
          return loadGroupTable();
        }).then(function(){
          var field=document.getElementById('table-'+item.object_id+'-'+kind+'-trigger');
          if(field)field.focus();
          else toast('未能定位该群，请刷新群列表后重试');
        });
      }})));
    });
    appendResourceReplacement(node,kind,name,data);
    return data;
  }).catch(function(error){if(node.dataset.requestId===requestId)node.textContent='读取引用失败：'+error.message;throw error;});
}
document.getElementById('persona-references-refresh').addEventListener('click',function(){loadResourceReferences(document.getElementById('persona-references'),'persona',personaEditingName).catch(function(){});});
document.getElementById('model-test').addEventListener('click', function () {
  var button = this; button.disabled = true;
  document.getElementById('model-save-note').textContent = '正在请求已保存的模型…';
  request('/api/llm/test', { method:'POST', body:{ prompt:'请简短回复：连接成功。' } }).then(function (data) {
    document.getElementById('model-save-note').textContent = '连接成功 · ' + (data.text || '') + ' · ' + (data.latency_ms || 0) + ' ms';
    toast('模型连接测试成功');
  }).catch(function (error) { document.getElementById('model-save-note').textContent = error.message; }).finally(function () { button.disabled = false; });
});
document.getElementById('model-manual-toggle').addEventListener('click', function () {
  manualModel = !manualModel;
  var field = document.getElementById('model-manual-field'); field.hidden = !manualModel;
  this.textContent = manualModel ? '收起手填' : '手动填写';
  if (manualModel) { document.getElementById('model-manual').value = document.getElementById('agent-model').value; document.getElementById('model-manual').focus(); }
});
document.getElementById('model-manual').addEventListener('input', function () {
  var select = document.getElementById('agent-model'); var value = this.value.trim();
  if (value && !Array.from(select.options).some(function (option) { return option.value === value; })) select.appendChild(h('option', { value:value, text:value + '（手动填写）' }));
  select.value = value; renderModelContext();
});
document.getElementById('persona-default-save').addEventListener('click', function () {
  var persona = document.getElementById('agent-persona').value;
  if (!persona) return;
  var button = this; button.disabled = true;
  request('/api/config', { method:'PATCH', headers:agentModelRevision?{'If-Match':agentModelRevision}:{}, body:{ llm:{ persona:persona } } }).then(function (result) {
    agentModelRevision=result.revision;
    agentModelInitial.persona = persona;
    document.getElementById('agent-persona-delete').disabled = !personaEditingName;
    renderPersonaList();
    document.getElementById('persona-default-note').textContent = '默认人格已更新：' + persona;
    toast('默认人格已更新'); return refresh();
  }).catch(function (error) { document.getElementById('persona-default-note').textContent = error.message; }).finally(function () { button.disabled = false; });
});
document.getElementById('plugins-reload').addEventListener('click', function () {
  var button = this; button.disabled = true;
  request('/api/plugins/reload', { method:'POST', body:{} }).then(function (data) {
    loadedPages.delete('agent');
    var failures = Object.keys(data.failed || {});
    toast(failures.length ? '扫描完成，请查看加载失败的插件' : '插件扫描完成'); return refresh();
  }).catch(function (error) { showBanner(error.message); }).finally(function () { button.disabled = false; });
});

function fillOptions(id, items, firstLabel, key, caption) {
  var select = document.getElementById(id); var value = select.value;
  select.textContent = ''; select.appendChild(h('option', { value:'', text:firstLabel }));
  items.forEach(function (item) { select.appendChild(h('option', { value:item[key], text:caption(item) })); });
  if (Array.from(select.options).some(function (item) { return item.value === value; })) select.value = value;
}
function loadTestOptions() {
  Promise.all([request('/api/llm/providers'), request('/api/personas')]).then(function (data) {
    fillOptions('test-provider', data[0], '默认路由', 'name', function (item) { return item.name + ' · ' + item.model; });
    fillOptions('test-persona', data[1], '默认人格', 'name', function (item) { return item.title || item.name; });
  }).catch(function (error) { document.getElementById('test-note').textContent = error.message; });
}
function bubble(message, role) {
  return h('div', { class:'bubble ' + role }, h('small', { text:role === 'user' ? '你' : role === 'assistant' ? 'FLLBot' : role }), h('span', { text:message }));
}
document.getElementById('test-form').addEventListener('submit', function (event) {
  event.preventDefault();
  var input = document.getElementById('test-message'); var message = input.value.trim();
  var send = document.getElementById('test-send');
  if (!message || send.disabled) return;
  var node = document.getElementById('test-messages');
  if (node.querySelector('.empty')) node.textContent = '';
  node.appendChild(bubble(message, 'user')); node.scrollTop = node.scrollHeight;
  send.disabled = true; send.textContent = '正在回答…'; document.getElementById('test-clear').disabled = true;
  document.getElementById('test-note').textContent = '等待模型回复…';
  request('/api/llm/test', { method:'POST', body:{ prompt:message, persona:document.getElementById('test-persona').value || null, provider:document.getElementById('test-provider').value || null } }).then(function (data) {
    node.appendChild(bubble(data.text || '模型返回了空回复。', 'assistant'));
    input.value = '';
    document.getElementById('test-note').textContent = (data.model || '') + ' · ' + (data.latency_ms || 0) + ' ms · ' + ((data.prompt_tokens || 0) + (data.completion_tokens || 0)) + ' Token';
  }).catch(function (error) { document.getElementById('test-note').textContent = '测试失败：' + error.message + '。输入已保留，可重试。'; }).finally(function () {
    send.disabled = false; send.textContent = '发送测试 ↗'; document.getElementById('test-clear').disabled = false; node.scrollTop = node.scrollHeight;
  });
});
document.getElementById('test-message').addEventListener('keydown', function (event) { if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) { event.preventDefault(); document.getElementById('test-form').requestSubmit(); } });
document.getElementById('test-clear').addEventListener('click', function () { document.getElementById('test-messages').textContent = ''; document.getElementById('test-note').textContent = '测试记录已清空'; });

function renderRooms() {
  var node = document.getElementById('history-rooms'); node.textContent = '';
  var query = document.getElementById('history-search').value.trim();
  var rooms = historyRooms.filter(function (room) { return String(room.scene_id).includes(query); });
  if (!rooms.length) node.appendChild(h('div', { class:'empty', text:'暂无匹配的会话；机器人产生 AI 对话后会在这里显示。' }));
  rooms.forEach(function (room) {
    node.appendChild(h('button', { type:'button', class:'room' + (activeRoom && room.scene === activeRoom.scene && room.scene_id === activeRoom.scene_id ? ' active' : ''), onclick:function () { selectRoom(room); } },
      h('strong', { text:(room.scene === 'group' ? '群聊 ' : '私聊 ') + room.scene_id }),
      h('small', { text:room.messages + ' 条 · ' + new Date(room.last_at * 1000).toLocaleString('zh-CN') })));
  });
}
function loadHistory() {
  document.getElementById('history-refresh').disabled = true;
  return request('/api/console/sessions').then(function (data) { historyRooms = data.items; renderRooms(); }).catch(function (error) {
    document.getElementById('history-note').textContent = error.message;
  }).finally(function () { document.getElementById('history-refresh').disabled = false; });
}
function selectRoom(room) {
  activeRoom = room; renderRooms();
  var requestId = ++historyRequestId;
  document.getElementById('history-title').textContent = (room.scene === 'group' ? '群聊 ' : '私聊 ') + room.scene_id;
  document.getElementById('history-delete').disabled = true;
  document.getElementById('history-messages').textContent = '正在读取…';
  document.getElementById('history-note').textContent = '';
  request('/api/chat/' + room.scene + '/' + room.scene_id + '?limit=100').then(function (data) {
    if (requestId !== historyRequestId) return;
    var node = document.getElementById('history-messages'); node.textContent = '';
    data.messages.forEach(function (message) { node.appendChild(bubble(message.content, message.role)); });
    if (!data.messages.length) node.appendChild(h('div', { class:'empty', text:'该会话暂无记录。' }));
    node.scrollTop = node.scrollHeight; document.getElementById('history-delete').disabled = false;
  }).catch(function (error) { if (requestId === historyRequestId) document.getElementById('history-messages').textContent = error.message; });
}
document.getElementById('history-refresh').addEventListener('click', loadHistory);
document.getElementById('history-search').addEventListener('input', renderRooms);
document.getElementById('history-delete').addEventListener('click', function () {
  if (!activeRoom || !window.confirm('清空此会话的全部 AI 上下文？清空后无法恢复。')) return;
  var room = activeRoom; var button = this; button.disabled = true;
  request('/api/chat/' + room.scene + '/' + room.scene_id, { method:'DELETE' }).then(function () {
    ++historyRequestId; activeRoom = null;
    document.getElementById('history-title').textContent = '选择一个会话';
    document.getElementById('history-messages').textContent = '会话已清空。'; toast('会话已清空'); return loadHistory();
  }).catch(function (error) { document.getElementById('history-note').textContent = error.message; button.disabled = false; });
});

var usageRequestId = 0;
var overviewUsageUpdated = 0;
var overviewUsageRequestId = 0;
function usageNumber(value) { return Number(value || 0).toLocaleString('zh-CN'); }
function loadOverviewUsage(force) {
  if (document.body.dataset.view !== 'overview' || (!force && Date.now() - overviewUsageUpdated < 10000)) return;
  overviewUsageUpdated = Date.now();
  var requestId = ++overviewUsageRequestId;
  var range = document.getElementById('overview-usage-range').value;
  return request('/api/llm/usage?range=' + range).then(function (usage) {
    if (requestId !== overviewUsageRequestId) return;
    var node = document.getElementById('overview-usage-metrics'); node.textContent = '';
    var success = usage.requests ? ((usage.requests - usage.failures) / usage.requests * 100).toFixed(1) + '%' : '—';
    [['调用次数', usageNumber(usage.requests), '失败 ' + usageNumber(usage.failures) + ' 次 · 成功率 ' + success],
     ['输入 Token', usageNumber(usage.prompt_tokens), '提示词、历史与工具结果'],
     ['输出 Token', usageNumber(usage.completion_tokens), '模型生成内容'],
     ['总 Token', usageNumber(usage.total_tokens), '平均耗时 ' + (usage.avg_latency_ms / 1000).toFixed(2) + ' s']].forEach(function (item) {
      node.appendChild(h('div', {class:'usage-metric'}, h('span', {text:item[0]}), h('strong', {text:item[1]}), h('small', {text:item[2]})));
    });
    document.getElementById('overview-usage-note').textContent = '重启后保留 · 服务商未返回 Token 时按文本估算。' + (usage.estimated_requests ? ' 其中 ' + usage.estimated_requests + ' 次含估算或旧记录，非精确账单。' : ' 当前范围内均为服务商实报用量。');
  }).catch(function (error) { if (requestId === overviewUsageRequestId) { overviewUsageUpdated = 0; document.getElementById('overview-usage-note').textContent = '用量读取失败：' + error.message; } });
}
document.getElementById('overview-usage-range').addEventListener('change', function () { loadOverviewUsage(true); });
document.getElementById('overview-usage-detail').addEventListener('click', function () {
  document.getElementById('usage-range').value = document.getElementById('overview-usage-range').value;
  navigate('usage');
});
function renderModelUsageTable(usage, node) {
  node.textContent = '';
  if (!usage.by_model.length) { node.appendChild(h('div', {class:'empty', text:'这个时间范围内还没有模型调用。'})); return; }
  var head = h('tr');
  ['模型 / 服务','调用','失败','输入 Token','输出 Token','总 Token','平均耗时','估算费用'].forEach(function (label) { head.appendChild(h('th', {scope:'col', text:label})); });
  var body = h('tbody');
  usage.by_model.forEach(function (item) {
      var row = h('tr', {}, h('td', {}, h('strong', {text:item.model || '未知模型'}), h('small', {text:(item.provider || '默认服务') + (item.estimated_requests ? ' · 含估算 ' + item.estimated_requests + ' 次' : '')})));
    [usageNumber(item.requests), usageNumber(item.failures), usageNumber(item.prompt_tokens), usageNumber(item.completion_tokens), usageNumber(item.tokens), (Number(item.avg_latency_ms || 0) / 1000).toFixed(2) + ' s', Number(item.cost || 0) ? Number(item.cost).toFixed(6) : '—'].forEach(function (text) { row.appendChild(h('td', {text:text})); });
    body.appendChild(row);
  });
  node.appendChild(h('div', {class:'usage-table-wrap'}, h('table', {class:'usage-model-table', 'aria-label':'按模型统计用量'}, h('thead', {}, head), body)));
}
function loadUsage() {
  var requestId = ++usageRequestId;
  document.getElementById('usage-refresh').disabled = true;
  return Promise.all([request('/api/llm/usage?range=' + document.getElementById('usage-range').value), request('/api/stats?days=7')]).then(function (data) {
    if (requestId !== usageRequestId) return;
    var usage = data[0]; var cards = document.getElementById('usage-cards'); cards.textContent = '';
    cards.appendChild(stat('spark', '模型请求', usage.requests, '失败 ' + usage.failures + ' 次'));
    cards.appendChild(stat('message', 'Token 总量', usage.total_tokens.toLocaleString('zh-CN'), '输入 ' + usage.prompt_tokens + ' / 输出 ' + usage.completion_tokens));
    cards.appendChild(stat('clock', '平均响应', (usage.avg_latency_ms / 1000).toFixed(2) + ' s', '模型调用耗时'));
    cards.appendChild(stat('grid', '调用成功率', usage.requests ? ((usage.requests - usage.failures) / usage.requests * 100).toFixed(1) + '%' : '—', '成功 ' + usageNumber(usage.requests - usage.failures) + ' 次'));
    renderModelUsageTable(usage, document.getElementById('usage-models'));
    var daily = data[1].daily; var chart = document.getElementById('message-chart'); chart.textContent = '';
    var maximum = Math.max.apply(null, daily.map(function (item) { return item.count; }).concat([1]));
    if (!daily.length) chart.appendChild(h('div', { class:'empty', text:'暂无消息数据，连接机器人后将开始记录。' }));
    daily.forEach(function (item) { var bar = h('i', { 'aria-hidden':'true' }); bar.style.height = Math.max(2, item.count / maximum * 130) + 'px'; chart.appendChild(h('div', { class:'bar-column', title:item.day + ' · ' + item.count + ' 条' }, h('span', { text:item.count }), bar, h('span', { text:item.day.slice(5) }))); });
    document.getElementById('usage-note').textContent = 'Token 优先使用服务商实报，缺失时按文本估算；旧记录无法区分时也标为含估算。费用按调用时配置的每百万 Token 单价累计，单位与配置一致；“—”表示无已记录费用，不代表免费。' + (usage.cost ? ' 合计估算：' + usage.cost : '');
  }).catch(function (error) { if (requestId === usageRequestId) document.getElementById('usage-note').textContent = error.message; }).finally(function () { if (requestId === usageRequestId) document.getElementById('usage-refresh').disabled = false; });
}
document.getElementById('usage-range').addEventListener('change', loadUsage);
document.getElementById('usage-refresh').addEventListener('click', loadUsage);

function renderToolChoices() {
  var field = document.querySelector('#agent-settings-form [data-key="allowed_tools"]');
  if (!field) return;
  var selected = field.value.split(/[,\n]/).map(function (item) { return item.trim(); }).filter(Boolean);
  var node = h('div', { class: 'tool-choices' });
  renderToolPicker(node, selected, field.disabled, function (id, checked) {
    var values = new Set(field.value.split(/[,\n]/).map(function (item) { return item.trim(); }).filter(Boolean));
    if (checked) values.add(id); else values.delete(id);
    field.value = Array.from(values).join('\n');
    field.dispatchEvent(new Event('input', { bubbles: true }));
  });
  field.hidden = true;
  field.insertAdjacentElement('afterend', node);
}

var toolsCatalog = [], toolDefaultBusy = false;
function loadTools() {
  return request('/api/tools').then(function (data) {
    toolsCatalog = Array.isArray(data.items) ? data.items : [];
    renderTools();
  }).catch(function (error) {
    var list = document.getElementById('tools-list');
    list.textContent = '';
    list.appendChild(h('div', { class: 'empty', text: '读取工具失败：' + error.message }));
  });
}
function renderTools() {
  var list = document.getElementById('tools-list');
  var query = (document.getElementById('tool-search').value || '').trim().toLowerCase();
  var filtered = toolsCatalog.filter(function (tool) {
    return [tool.title, tool.id, tool.plugin, tool.description].join(' ').toLowerCase().includes(query);
  });
  var openCount = toolsCatalog.filter(function (tool) { return tool.globally_allowed; }).length;
  document.getElementById('tool-summary').textContent = toolsCatalog.length + ' 个工具 · 默认开放 ' + openCount + ' 个';
  list.textContent = '';
  if (!filtered.length) {
    list.appendChild(h('div', { class: 'empty', text: toolsCatalog.length ? '没有匹配的工具，试试其他关键词' : '还没有可用的 Agent 工具；启用提供工具的插件后会显示在这里。' }));
    return;
  }
  filtered.forEach(function (tool) {
    var tags = h('span', { class: 'tool-tags' });
    tags.appendChild(h('span', { class: 'tag ' + (tool.plugin_enabled ? 'on' : 'off'), text: tool.plugin_enabled ? '可用' : '未启用' }));
    tags.appendChild(h('span', { class: 'tag' + (tool.globally_allowed ? ' on' : ''), text: tool.globally_allowed ? '默认开放' : '默认未开放' }));
    if (tool.group_count) tags.appendChild(h('span', { class: 'tag', text: tool.group_count + ' 个群单独设置' }));
    tags.appendChild(h('span', { class: 'tag', text: '来自插件 ' + tool.plugin }));
    list.appendChild(h('div', { class: 'tool-row' },
      h('div', { class: 'plugin-icon', text: String(tool.title || tool.id || 'T').slice(0, 1), 'aria-hidden': 'true' }),
      h('div', { class: 'plugin-main' },
        h('div', { class: 'plugin-title' }, h('strong', { text: tool.title || tool.id }), tags),
        h('p', { class: 'plugin-desc', title:tool.description, text: tool.description || '没有说明' }),
        tool.permission ? h('div', { class: 'plugin-meta' }, h('span', { text: '权限 ' + tool.permission })) : null
      ),
      h('div', { class: 'plugin-actions' }, makeToolDefaultSwitch(tool))
    ));
  });
}
function makeToolDefaultSwitch(tool) {
  var toggle=h('button',{type:'button',class:'switch',role:'switch','aria-checked':String(tool.globally_allowed),'aria-label':'默认允许 '+tool.title,onclick:function(){
    if(toolDefaultBusy)return;toolDefaultBusy=true;renderTools();
    request('/api/tools/'+encodeURIComponent(tool.id)+'/enabled',{method:'PATCH',body:{enabled:!tool.globally_allowed}}).then(function(){
      if(!dirtyPages.has('agent'))loadedPages.delete('agent');return loadTools();
    }).catch(function(error){toast(error.message);}).finally(function(){toolDefaultBusy=false;renderTools();});
  }});toggle.disabled=toolDefaultBusy;
  return h('div',{class:'tool-default-action'},h('small',{text:'默认允许 Agent 调用'}),toggle);
}
document.getElementById('tools-refresh').addEventListener('click', loadTools);
document.getElementById('tool-search').addEventListener('input', renderTools);

function renderManagedGroups() {
  fillOptions('managed-group', groupDirectory, '请选择群聊', 'group_id', function (group) { return (group.group_name || '群聊') + ' · ' + group.group_id; });
  document.getElementById('managed-group').disabled=!groupDirectory.length;
  if(document.body.dataset.view==='groups')loadGroupTable();
  if(!groupDirectory.length)document.getElementById('group-editor-empty').textContent='暂无可设置的群。连接机器人后，点击“刷新群列表”。';
}
function loadManagedGroup() {
  var groupId = document.getElementById('managed-group').value; var requestId = ++managedRequestId;
  document.getElementById('managed-save').disabled = true;
  managedRevision=null;document.getElementById('managed-reload').hidden=true;
  document.getElementById('group-changes').textContent='正在读取记录…';
  document.getElementById('group-detail-form').hidden=true;
  document.getElementById('group-editor-empty').hidden=false;
  document.getElementById('group-editor-empty').textContent=groupId?'正在读取这个群的设置…':'选择群聊后，在这里调整回复方式。';
  ['managed-enabled', 'managed-llm', 'managed-persona','policy-mode'].forEach(function (id) { document.getElementById(id).disabled = true; });
  document.getElementById('policy-editor').hidden=true;
  if (!groupId) { document.getElementById('managed-note').textContent = '选择群聊后编辑。'; document.getElementById('policy-effective').textContent='选择群聊后显示。'; return; }
  document.getElementById('managed-note').textContent = '正在读取…';
  Promise.all([request('/api/console/groups/' + groupId + (managedBotId ? '?self_id=' + managedBotId : '')), request('/api/personas'), request('/api/llm/providers')]).then(function (data) {
    if (requestId !== managedRequestId) return;
    var group = data[0];
    managedRevision=group.revision;
    loadGroupChanges(groupId,requestId);
    fillOptions('managed-persona', data[1], '跟随默认人格', 'name', function (item) { return item.title || item.name; });
    document.getElementById('managed-enabled').checked = group.enabled;
    document.getElementById('managed-llm').value = group.llm_enabled == null ? 'inherit' : group.llm_enabled ? 'on' : 'off';
    document.getElementById('managed-persona').value = group.persona;
    renderPolicy(group,data[2]);
    var diagnosis=group.effective_policy.diagnostics;
    var failedChecks=diagnosis?(diagnosis.checks||[]).filter(function(check){return !check.passed;}):[];
    document.getElementById('managed-note').textContent = failedChecks.length
      ? failedChecks[0].message+'；可展开生效详情查看诊断。'
      : '已保存配置就绪；实际回复取决于消息触发与调用结果。';
    ['managed-enabled', 'managed-llm', 'managed-persona', 'managed-save','policy-mode'].forEach(function (id) { document.getElementById(id).disabled = false; });
  }).catch(function (error) { if (requestId === managedRequestId) document.getElementById('group-editor-empty').textContent = error.message; });
}
document.getElementById('managed-group').addEventListener('change', loadManagedGroup);
document.getElementById('managed-reload').addEventListener('click',function(){
  if(window.confirm('重新读取会放弃当前未保存的修改，继续吗？'))loadManagedGroup();
});

function loadGroupChanges(groupId,requestId,before) {
  var node=document.getElementById('group-changes');
  return request('/api/console/groups/'+groupId+'/changes'+(before?'?before='+before:'')).then(function(data){
    if(requestId!==managedRequestId)return;
    if(!before)node.textContent='';
    var more=node.querySelector('button');if(more)more.remove();
    if(!data.items.length&&!before)node.appendChild(h('p',{class:'settings-note',text:'暂无记录。仅记录启用本功能后的群设置与全局配置变化。'}));
    var labels={enabled:'群总开关','config.policy':'群策略','config.llm':'群 AI / 人格','config._console_rule':'单群配置','config._inherit_enabled':'开关继承',config:'群配置',system:'全局配置（可能影响本群）'};
    data.items.forEach(function(item){node.appendChild(h('p',{class:'settings-note',text:new Date(item.created_at*1000).toLocaleString('zh-CN')+' · 实例操作 · '+({created:'群登记',updated:'设置更新',deleted:'群记录删除',global_updated:'全局设置更新',resource_replaced:'共享资源引用替换',plugin_settings_updated:'插件设置更新',bot_profile_updated:'账号资料更新',startup_overrides_archived:'旧启动覆盖归档',plugin_state_updated:'插件开关更新'}[item.action]||item.action)+' · '+item.fields.map(function(field){return labels[field]||field;}).join('、')}));});
    if(data.next_cursor)node.appendChild(h('button',{type:'button',class:'btn',text:'更早记录',onclick:function(){this.disabled=true;loadGroupChanges(groupId,requestId,data.next_cursor);}}));
  }).catch(function(error){if(requestId===managedRequestId){node.appendChild(h('p',{class:'settings-note',text:'读取变更记录失败：'+error.message}));var more=node.querySelector('button');if(more)more.disabled=false;}});
}
document.getElementById('group-detail-form').addEventListener('submit', function (event) {
  event.preventDefault(); var button = document.getElementById('managed-save'); if (button.disabled) return;
  var policy;
  try { policy=collectPolicy(); } catch(error) { document.getElementById('managed-note').textContent=error.message; return; }
  button.disabled = true; groupDetailSaving = true; var choice = document.getElementById('managed-llm').value;
  document.getElementById('managed-note').textContent='正在保存…';
  request('/api/console/groups/' + document.getElementById('managed-group').value+groupContextQuery(), { method:'PATCH', headers:managedRevision?{'If-Match':managedRevision}:{}, body:{ enabled:document.getElementById('managed-enabled').checked, llm_enabled:choice === 'inherit' ? null : choice === 'on', persona:document.getElementById('managed-persona').value,policy:policy } }).then(function () {
    groupDetailSaving = false; toast('这个群的设置已保存'); closeGroupDetails();loadGroupTable();loadedPages.delete('readiness');
  }).catch(function (error) { groupDetailSaving = false; document.getElementById('managed-note').textContent = error.message; button.disabled = error.status===409; document.getElementById('managed-reload').hidden=error.status!==409; });
});
var systemFields = [
  ['bot.superusers', '超级管理员 QQ', 'ids', '填写 QQ 号，多个用逗号或换行分隔。'],
  ['bot.command_prefix', '命令前缀', 'list', '多个用逗号分隔，例如 /,!。'],
  ['bot.allow_bare_command', '@ 时允许省略命令前缀', 'bool'],
  ['permission.allow_private', '允许私聊使用机器人', 'bool'],
  ['permission.default_group_enabled', '新群默认启用机器人', 'bool'],
  ['permission.group_admin_permission', '允许群管理员使用管理命令', 'bool'],
  ['permission.deny_groups', '群聊黑名单', 'ids', '黑名单优先于白名单，多个群号用逗号分隔。'],
  ['llm.enable_group', '允许群聊 AI 对话', 'bool'],
  ['llm.enable_private', '允许私聊 AI 对话', 'bool'],
  ['llm.cooldown_seconds', 'AI 对话冷却（秒）', 'number', '自然语言智能体的冷却在“配置 → 默认行为”中设置，单群策略可独立覆盖。'],
  ['llm.max_context_messages', '最多携带的上下文消息数', 'number'],
  ['logging.level', '日志详细程度', 'select', '', ['DEBUG', 'INFO', 'WARNING', 'ERROR']],
  ['bot.error_reply', '命令异常时的回复', 'text', '留空则只记录日志。']
];
function getNested(object, path) { return path.split('.').reduce(function (value, key) { return value[key]; }, object); }
function loadSystem() {
  document.getElementById('system-save').disabled = true;
  return request('/api/config').then(function (data) {
    renderStartupConfig(data.startup);
    systemInitial = data.config; systemRevision=data.revision; var node = document.getElementById('system-fields'); node.textContent = '';
    systemFields.forEach(function (field) {
      var value = getNested(systemInitial, field[0]); var id = 'system-' + field[0].replace('.', '-'); var control;
      if (field[2] === 'select') { control = h('select', { id:id }); field[4].forEach(function (choice) { control.appendChild(h('option', { value:choice, text:choice })); }); control.value = value; }
      else if (field[2] === 'bool') { control = h('input', { id:id, type:'checkbox' }); control.checked = value; }
      else { control = h('input', { id:id, type:field[2] === 'number' ? 'number' : 'text', min:field[0] === 'llm.max_context_messages' ? '1' : '0', step:'1' }); control.value = Array.isArray(value) ? value.join(', ') : value; }
      control.dataset.path = field[0]; control.dataset.kind = field[2];
      var wrap = h('div', { class:'setting-field' + (field[2] === 'bool' ? ' is-check' : '') });
      if (field[2] === 'bool') wrap.appendChild(control);
      wrap.appendChild(h('label', { for:id, text:field[1] }));
      if (field[2] !== 'bool') wrap.appendChild(control);
      if (field[3]) wrap.appendChild(h('small', { text:field[3] }));
      node.appendChild(wrap);
    });
    var host = location.hostname || '127.0.0.1'; if (host.includes(':')) host = '[' + host + ']';
    document.getElementById('connection-url').textContent = (location.protocol === 'https:' ? 'wss://' : 'ws://') + host + (location.port ? ':' + location.port : '') + systemInitial.server.ws_path;
    document.getElementById('connection-note').textContent = '消息格式选择 array，开启心跳。' + (systemInitial.server.access_token ? '连接令牌已设置，请使用配置文件中的 server.access_token。' : '当前未设置连接令牌。');
    document.getElementById('system-note').textContent = '已读取当前生效配置'; document.getElementById('system-save').disabled = false;
  }).catch(function (error) { document.getElementById('system-note').textContent = error.message; });
}
document.getElementById('system-reload').addEventListener('click', function () { loadSystem().then(function () { markSaved('system'); }); });
document.getElementById('system-form').addEventListener('submit', function (event) {
  event.preventDefault(); var button = document.getElementById('system-save'); if (button.disabled) return;
  var patch = {};
  try {
    document.querySelectorAll('#system-fields [data-path]').forEach(function (control) {
      var path = control.dataset.path.split('.'); var kind = control.dataset.kind; var value = control.value;
      if (kind === 'bool') value = control.checked;
      if (kind === 'ids' || kind === 'list') {
        value = value.split(/[,，\s]+/).filter(Boolean);
        if (kind === 'ids') { if (value.some(function (item) { return !/^\d+$/.test(item) || !Number.isSafeInteger(Number(item)) || Number(item) <= 0; })) throw new Error('QQ 号和群号必须是正整数。'); value = value.map(Number); }
      }
      if (kind === 'number') { value = Number(value); if (control.value.trim() === '' || !Number.isInteger(value) || value < Number(control.min)) throw new Error('数值超出允许范围，请检查。'); }
      if (JSON.stringify(value) === JSON.stringify(getNested(systemInitial, control.dataset.path))) return;
      if (!patch[path[0]]) patch[path[0]] = {}; patch[path[0]][path[1]] = value;
    });
  } catch (error) { document.getElementById('system-note').textContent = error.message; return; }
  if (!Object.keys(patch).length) { toast('没有需要保存的更改'); return; }
  button.disabled = true;
  request('/api/config', { method:'PATCH', headers:systemRevision?{'If-Match':systemRevision}:{}, body:patch }).then(function (data) {
    systemInitial = data.config; systemRevision=data.revision; markSaved('system');
    // LLM switches changed here should be reread next time the model page opens.
    if (!dirtyPages.has('models')) agentModelLoaded = false;
    document.getElementById('system-note').textContent = '设置已保存并生效'; toast('系统设置已保存'); return refresh();
  }).catch(function (error) { document.getElementById('system-note').textContent = error.message; }).finally(function () { button.disabled = false; });
});
document.getElementById('copy-connection').addEventListener('click', function () {
  var value = document.getElementById('connection-url').textContent;
  if (!navigator.clipboard) { toast('请选中上面的地址复制'); return; }
  navigator.clipboard.writeText(value).then(function () { toast('连接地址已复制'); }).catch(function () { toast('请选中上面的地址复制'); });
});
// Forms use explicit save controls; Enter in a field must not reload the console.
['agent-model-form','agent-settings-form','settings-form'].forEach(function (id) { document.getElementById(id).addEventListener('submit', function (event) { event.preventDefault(); }); });

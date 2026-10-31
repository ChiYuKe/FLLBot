var groupTableSelected = new Set(), groupTableVisibleIds = [];
var groupTableData = new Map(), groupTableProviders = [], groupTablePersonas = [];
var groupTableLoadId = 0, groupTableBusy = false, groupDetailSaving = false, groupDetailFocus = null;

function enhanceReplyModes(select) {
  if(selectControls.has(select))return selectControls.get(select);
  var title=select.getAttribute('aria-label')||'回复方式',id=select.id+'-multi',draft=[],opened=false,signature='';
  var root=h('div',{class:'ui-select reply-multiselect'}),caption=h('span',{class:'ui-select-text'});
  var trigger=h('button',{id:select.id+'-trigger',type:'button',class:'ui-select-trigger','aria-label':title,'aria-haspopup':'dialog','aria-expanded':'false','aria-controls':id},caption,h('span',{class:'ui-select-chevron',text:'⌄'}));
  var choices=h('div',{class:'reply-mode-choices'}),apply=h('button',{type:'button',class:'btn primary',text:'应用'}),cancel=h('button',{type:'button',class:'btn',text:'取消'});
  var popup=h('div',{id:id,class:'ui-select-popup reply-modes-popup',role:'dialog','aria-label':'选择'+title},h('p',{class:'reply-modes-hint',text:'可多选，满足任一条件即可响应'}),choices,h('div',{class:'reply-modes-actions'},cancel,apply));
  popup.hidden=true;select.parentNode.insertBefore(root,select);root.appendChild(select);root.appendChild(trigger);
  select.hidden=true;select.tabIndex=-1;select.setAttribute('aria-hidden','true');document.body.appendChild(popup);
  var control={select:select,root:root,trigger:trigger,popup:popup,sync:sync,close:close,position:position};
  selectControls.set(select,control);
  function values(){return Array.from(select.selectedOptions).map(function(option){return option.value;});}
  function sync(){
    var selected=values(),next=JSON.stringify([selected,select.disabled]);if(next===signature)return;signature=next;
    var labels=Array.from(select.options).filter(function(option){return option.selected;}).map(function(option){return option.textContent;});
    caption.textContent=selected.length>2?'已选 '+selected.length+' 项':selected.length>1?selected.map(function(value){return {at:'@',prefix:'唤醒词',all:'自动'}[value]||value;}).join(' + '):labels.join('')||'跟随默认';
    trigger.title=labels.join(' + ');trigger.disabled=select.disabled;if(select.disabled)close(false);
  }
  function close(restore){opened=false;popup.hidden=true;trigger.setAttribute('aria-expanded','false');if(activeSelectControl===control)activeSelectControl=null;if(restore&&trigger.isConnected&&!trigger.disabled)trigger.focus();}
  function position(){
    if(!opened)return;var bounds=trigger.getBoundingClientRect();
    if(bounds.bottom<0||bounds.top>innerHeight){close(false);return;}
    popup.style.width=Math.min(240,innerWidth-16)+'px';
    popup.style.left=Math.max(8,Math.min(bounds.left,innerWidth-popup.offsetWidth-8))+'px';
    var below=innerHeight-bounds.bottom-8,up=below<popup.offsetHeight&&bounds.top>below;
    popup.style.top=(up?Math.max(8,bounds.top-popup.offsetHeight-6):Math.min(innerHeight-popup.offsetHeight-8,bounds.bottom+6))+'px';
  }
  function open(){
    if(select.disabled)return;if(activeSelectControl)activeSelectControl.close(false);
    draft=values();if(!draft.length)draft=['inherit'];choices.textContent='';
    Array.from(select.options).forEach(function(option){
      var input=h('input',{type:'checkbox',value:option.value});input.checked=draft.includes(option.value);
      input.addEventListener('change',function(){
        if(input.checked){
          if(['inherit','off'].includes(input.value))draft=[input.value];
          else {draft=draft.filter(function(value){return !['inherit','off'].includes(value);});if(!draft.includes(input.value))draft.push(input.value);}
        }else draft=draft.filter(function(value){return value!==input.value;});
        if(!draft.length)draft=['inherit'];
        choices.querySelectorAll('input').forEach(function(check){check.checked=draft.includes(check.value);});
      });
      choices.appendChild(h('label',{class:'reply-mode-choice'},input,h('span',{text:option.textContent})));
    });
    popup.hidden=false;opened=true;activeSelectControl=control;trigger.setAttribute('aria-expanded','true');position();
    var focus=choices.querySelector('input:checked')||choices.querySelector('input');if(focus)focus.focus();
  }
  trigger.addEventListener('click',function(){if(opened)close(true);else open();});
  trigger.addEventListener('keydown',function(event){if(event.key==='ArrowDown'||event.key==='ArrowUp'){event.preventDefault();open();}});
  cancel.addEventListener('click',function(){close(true);});
  apply.addEventListener('click',function(){
    var before=values();Array.from(select.options).forEach(function(option){option.selected=draft.includes(option.value);});
    var changed=JSON.stringify(before)!==JSON.stringify(values());sync();close(true);
    if(changed){select.dispatchEvent(new Event('input',{bubbles:true}));select.dispatchEvent(new Event('change',{bubbles:true}));}
  });
  popup.addEventListener('keydown',function(event){
    if(event.key==='Escape'){event.preventDefault();event.stopPropagation();close(true);}
    if(event.key==='Tab'){
      var nodes=Array.from(popup.querySelectorAll('input,button')),first=nodes[0],last=nodes[nodes.length-1];
      if(event.shiftKey&&document.activeElement===first){event.preventDefault();last.focus();}
      else if(!event.shiftKey&&document.activeElement===last){event.preventDefault();first.focus();}
    }
  });
  sync();return control;
}

function groupReplyValue(group) {
  var policy = group.policy || {}, effective = group.effective_policy || {};
  if (group.llm_enabled === false || policy.auto_group === false) return ['off'];
  if (policy.trigger_modes && policy.trigger_modes.length) return policy.trigger_modes.slice();
  if (policy.auto_group === true || policy.trigger_mode && effective.auto_group) return [effective.trigger_mode || 'at'];
  return ['inherit'];
}
function groupTableSelect(id, label, value, options, save) {
  var select = h('select', {id:id, 'aria-label':label});
  if (Array.isArray(value)) { select.multiple=true; select.dataset.replyModes='true'; }
  options.forEach(function (option) { select.appendChild(h('option', {value:option[0], text:option[1]})); });
  if (!Array.isArray(value) && !options.some(function (option) { return option[0] === value; })) select.appendChild(h('option', {value:value, text:value + '（不可用）'}));
  if (Array.isArray(value)) Array.from(select.options).forEach(function(option){option.selected=value.includes(option.value);});
  else select.value = value;
  select.disabled = groupTableBusy;
  select.addEventListener('change', function () { save(select.multiple?Array.from(select.selectedOptions).map(function(option){return option.value;}):select.value); });
  return select;
}
function renderGroupTableRows() {
  var body = document.getElementById('group-table-body'), query = document.getElementById('group-table-search').value.trim().toLowerCase();
  body.textContent = '';
  var configured = groupDirectory.filter(function (group) { var detail=groupTableData.get(group.group_id); return detail && detail.has_override; });
  var groups = configured.filter(function (group) { return !query || (String(group.group_id) + ' ' + (group.group_name || '')).toLowerCase().includes(query); });
  document.getElementById('group-table-count').textContent = '共 ' + configured.length + ' 条配置' + (query ? ' · 筛选到 ' + groups.length + ' 个' : '');
  groupTableVisibleIds=groups.map(function(group){return group.group_id;});
  groups.forEach(function (group) {
    var data = groupTableData.get(group.group_id), title = group.group_name || '未获取群名', label = title + ' ' + group.group_id;
    var row = h('tr', {'data-group-id':group.group_id});
    var identity = h('td', {class:'group-table-name',title:title+' · '+group.group_id}, h('strong', {text:title}), h('small', {text:String(group.group_id)}));
    var selection=h('input',{type:'checkbox',class:'group-row-select','aria-label':label+' 选择配置'});
    selection.checked=groupTableSelected.has(group.group_id);selection.disabled=groupTableBusy;
    selection.addEventListener('change',function(){if(selection.checked)groupTableSelected.add(group.group_id);else groupTableSelected.delete(group.group_id);renderGroupBatchToolbar();});
    identity.insertBefore(selection,identity.firstChild);
    row.appendChild(identity);
    if (!data) { row.appendChild(h('td', {colspan:6, text:'配置暂未读取，请刷新重试'})); body.appendChild(row); return; }
    if (data.denied || !data.allowed) identity.appendChild(h('span', {class:'group-table-warning', title:'受访问范围限制，配置不会绕过白名单或黑名单', text:' · 受限'}));
    var enabled = h('input', {id:'table-' + group.group_id + '-enabled',type:'checkbox', 'aria-label':label + ' 启用'});
    enabled.checked = data.enabled; enabled.disabled = groupTableBusy;
    enabled.addEventListener('change', function () { saveGroupTableField(group.group_id, 'enabled', enabled.checked); });
    row.appendChild(h('td', {}, enabled));
    var fields = [
      ['access', '访问', data.access || 'inherit', [['inherit',document.getElementById('group-default-access').value==='all' ? '默认：允许' : '默认：禁止'],['allow','允许'],['deny','禁止']]],
      ['reply', '回复方式', groupReplyValue(data), [['inherit','跟随默认'],['at','仅被 @'],['prefix','唤醒词'],['all','自动参与'],['off','关闭 AI']]],
      ['provider', '模型', (data.policy || {}).provider || '', [['','跟随默认']].concat(groupTableProviders.map(function (p) { return [p.name,p.name + ' · ' + p.model]; }))],
      ['persona', '人格', data.persona || '', [['','跟随默认']].concat(groupTablePersonas.map(function (p) { return [p.name,p.title || p.name]; }))]
    ];
    fields.forEach(function (field) {
      row.appendChild(h('td', {}, groupTableSelect('table-' + group.group_id + '-' + field[0], label + ' ' + field[1], field[2], field[3], function (value) { saveGroupTableField(group.group_id,field[0],value); })));
    });
    row.appendChild(h('td', {class:'group-row-actions'}, h('button', {type:'button', class:'btn group-edit-button', 'data-edit-group':group.group_id, text:'编辑', onclick:function () { openGroupDetails(group.group_id); }}), h('button',{type:'button',class:'btn group-remove-button',text:'移除',title:'移除配置后恢复默认行为', 'aria-label':label+' 移除配置',onclick:function(){ removeGroupRule(group.group_id); }})));
    if (groupTableBusy) row.querySelectorAll('button').forEach(function(button){button.disabled=true;});
    body.appendChild(row);
  });
  document.getElementById('group-table-head').hidden=!configured.length;
  renderGroupRuleChoices();renderGroupBatchToolbar();
  if (!groups.length) body.appendChild(h('tr', {}, h('td', {colspan:7, class:'group-table-empty', text:query ? '没有匹配的群聊' : '暂无单群配置，各群按默认访问规则和默认行为运行。点击“新增配置”添加。'})));
}
function loadGroupTable() {
  if (groupTableBusy) return Promise.resolve();
  var loadId = ++groupTableLoadId;
  document.getElementById('group-table-notice').textContent = '正在读取配置…';
  var batches = [];
  for (var start = 0; start < groupDirectory.length; start += 100) {
    var query = groupDirectory.slice(start,start + 100).map(function (group) { return 'group_ids=' + group.group_id; }).join('&');
    batches.push(request('/api/console/groups?' + query));
  }
  return Promise.all([request('/api/llm/providers'),request('/api/personas'),Promise.all(batches),request('/api/console/access')]).then(function (data) {
    if (loadId !== groupTableLoadId || groupTableBusy) return;
    document.getElementById('group-default-access').value=data[3].mode;
    groupTableProviders = data[0]; groupTablePersonas = data[1]; groupTableData.clear();
    data[2].forEach(function (batch) { batch.items.forEach(function (item) { groupTableData.set(item.group_id,item); }); });
    document.getElementById('group-table-notice').textContent = '';
    renderGroupTableRows();
  }).catch(function (error) { if (loadId === groupTableLoadId) { document.getElementById('group-table-notice').textContent = error.message; renderGroupTableRows(); } });
}
function saveGroupTableField(groupId, key, value) {
  if (groupTableBusy) return;
  var original = groupTableData.get(groupId), policy = Object.assign({},original.policy || {});
  var payload = {enabled:original.enabled, persona:original.persona || '', llm_enabled:original.llm_enabled == null ? null : original.llm_enabled};
  if (key === 'enabled') payload.enabled = value;
  if (key === 'persona') payload.persona = value;
  if (key === 'provider') { if (value) policy.provider = value; else delete policy.provider; payload.policy = policy; }
  if (key === 'reply') {
    var modes=Array.isArray(value)?value:[value],inherit=modes.includes('inherit'),off=modes.includes('off');
    payload.llm_enabled = inherit ? null : !off;
    delete policy.trigger_mode;delete policy.trigger_modes;
    if (inherit) delete policy.auto_group;
    else { policy.auto_group = !off; if (!off) policy.trigger_modes = modes; }
    payload.policy = policy;
  }
  if (payload.policy && !Object.keys(payload.policy).length) payload.policy = null;
  groupTableBusy = true; ++groupTableLoadId; renderGroupRuleChoices();renderGroupBatchToolbar();
  document.getElementById('group-table-notice').textContent = '正在保存…';
  document.querySelectorAll('#group-table-body input,#group-table-body select,#group-table-body button').forEach(function (node) { node.disabled = true; });
  document.getElementById('groups-refresh').disabled = true;
  request('/api/console/groups/' + groupId + (key==='access'?'/access':''), {method:'PATCH',body:key==='access'?{access:value}:payload}).then(function (result) {
    groupTableData.set(groupId,result);
    document.getElementById('group-table-notice').textContent = '已保存'; loadedPages.delete('readiness');
  }).catch(function (error) { document.getElementById('group-table-notice').textContent = '保存失败：' + error.message; }).finally(function () {
    groupTableBusy = false; document.getElementById('groups-refresh').disabled = false;
    renderGroupTableRows(); refreshSelectControls();
    if (document.body.dataset.view === 'groups') {
      var focus = document.getElementById('table-' + groupId + '-' + key + (key === 'enabled' ? '' : '-trigger'));
      if (focus) focus.focus();
    }
  });
}
function openGroupDetails(groupId) {
  if (groupTableBusy) return;
  groupDetailFocus = document.activeElement;
  var group = groupDirectory.find(function (item) { return item.group_id === Number(groupId); });
  document.getElementById('managed-group').value = String(groupId);
  document.getElementById('group-editor-title').textContent = (group && group.group_name || '群聊') + ' · 详细设置';
  document.getElementById('group-editor-modal').classList.add('open');
  document.getElementById('dashboard').inert = true; document.querySelector('.sidebar').inert = true;
  document.body.classList.add('group-dialog-open');
  document.getElementById('group-editor-close').focus(); loadManagedGroup();
}
function closeGroupDetails() {
  if (groupDetailSaving) { toast('正在保存，请稍候'); return; }
  ++managedRequestId;
  if (activeSelectControl) activeSelectControl.close(false);
  document.getElementById('group-editor-modal').classList.remove('open');
  document.getElementById('dashboard').inert = false; document.querySelector('.sidebar').inert = false;
  document.body.classList.remove('group-dialog-open'); markSaved('groups');
  if (groupDetailFocus && groupDetailFocus.isConnected) groupDetailFocus.focus();
}
document.getElementById('group-table-search').addEventListener('input',renderGroupTableRows);
document.getElementById('group-editor-close').addEventListener('click',closeGroupDetails);
document.getElementById('group-editor-cancel').addEventListener('click',closeGroupDetails);
document.getElementById('group-editor-modal').addEventListener('keydown',function (event) {
  if (event.key === 'Escape') {
    if (activeSelectControl) return;
    event.preventDefault(); closeGroupDetails();
  }
  if (event.key === 'Tab') {
    var nodes = Array.from(this.querySelectorAll('button,input,textarea,summary,[tabindex="0"]')).filter(function (node) { return !node.disabled && node.getClientRects().length; });
    var first = nodes[0], last = nodes[nodes.length - 1];
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
  }
});

function renderGroupRuleChoices() {
  var available=groupDirectory.filter(function(group){var data=groupTableData.get(group.group_id);return data&&!data.has_override;});
  fillOptions('group-rule-target',available,'请选择群聊','group_id',function(group){return (group.group_name||'群聊')+' · '+group.group_id;});
  document.getElementById('group-rule-add').disabled=groupTableBusy||!available.length;
  document.getElementById('group-default-access').disabled=groupTableBusy;
  document.getElementById('group-rule-target').disabled=groupTableBusy;
  document.getElementById('group-rule-confirm').disabled=groupTableBusy||!document.getElementById('group-rule-target').value;
}
function changeGroupRule(groupId,method) {
  if(groupTableBusy)return;
  groupTableBusy=true;++groupTableLoadId;renderGroupTableRows();
  document.getElementById('groups-refresh').disabled=true;
  document.getElementById('group-table-notice').textContent=method==='POST'?'正在新增…':'正在恢复默认行为…';
  request('/api/console/groups/'+groupId+'/rule',{method:method}).then(function(result){
    groupTableData.set(Number(groupId),result);
    document.getElementById('group-rule-create').hidden=true;
    document.getElementById('group-table-search').value='';
    document.getElementById('group-table-notice').textContent=method==='POST'?'已新增，可直接修改本行配置。':'已移除，该群恢复使用默认行为。';
    loadedPages.delete('readiness');
  }).catch(function(error){document.getElementById('group-table-notice').textContent=error.message;}).finally(function(){
    groupTableBusy=false;document.getElementById('groups-refresh').disabled=false;renderGroupTableRows();
  });
}
function removeGroupRule(groupId){changeGroupRule(groupId,'DELETE');}
document.getElementById('group-rule-add').addEventListener('click',function(){
  renderGroupRuleChoices();document.getElementById('group-rule-create').hidden=false;
  refreshSelectControls();var trigger=document.getElementById('group-rule-target-trigger');if(trigger)trigger.focus();
});
document.getElementById('group-rule-target').addEventListener('change',renderGroupRuleChoices);
document.getElementById('group-rule-confirm').addEventListener('click',function(){var id=document.getElementById('group-rule-target').value;if(id)changeGroupRule(id,'POST');});
document.getElementById('group-rule-cancel').addEventListener('click',function(){document.getElementById('group-rule-create').hidden=true;});

document.getElementById('group-default-access').addEventListener('change',function(){
  if(groupTableBusy)return;
  var mode=this.value;groupTableBusy=true;++groupTableLoadId;renderGroupTableRows();
  document.getElementById('groups-refresh').disabled=true;
  document.getElementById('group-table-notice').textContent='正在保存默认访问规则…';
  request('/api/console/access',{method:'PATCH',body:{mode:mode}}).then(function(){
    document.getElementById('group-table-notice').textContent='默认访问规则已保存';
  }).catch(function(error){toast('保存默认访问规则失败：'+error.message);}).finally(function(){
    groupTableBusy=false;document.getElementById('groups-refresh').disabled=false;loadGroupTable();
  });
});

function renderGroupBatchToolbar(){
  groupTableSelected.forEach(function(id){var data=groupTableData.get(id);if(!data||!data.has_override)groupTableSelected.delete(id);});
  var count=groupTableSelected.size,select=document.getElementById('group-table-select-visible');
  document.getElementById('group-table-batch').hidden=!count;
  document.getElementById('group-batch-hint').hidden=!!count;
  document.getElementById('group-batch-count').textContent='已选 '+count+' 项';
  var visibleSelected=groupTableVisibleIds.filter(function(id){return groupTableSelected.has(id);}).length;
  select.checked=!!groupTableVisibleIds.length&&visibleSelected===groupTableVisibleIds.length;
  select.indeterminate=visibleSelected>0&&!select.checked;select.disabled=groupTableBusy||!groupTableVisibleIds.length;
  document.querySelectorAll('#group-table-batch button').forEach(function(button){button.disabled=groupTableBusy;});
}
document.getElementById('group-table-select-visible').addEventListener('change',function(){
  var checked=this.checked;groupTableVisibleIds.forEach(function(id){if(checked)groupTableSelected.add(id);else groupTableSelected.delete(id);});renderGroupTableRows();
});
document.getElementById('group-batch-clear').addEventListener('click',function(){groupTableSelected.clear();renderGroupTableRows();});
document.querySelectorAll('[data-batch-access]').forEach(function(button){button.addEventListener('click',function(){
  if(groupTableBusy||!groupTableSelected.size)return;
  groupTableBusy=true;++groupTableLoadId;renderGroupTableRows();document.getElementById('groups-refresh').disabled=true;
  var ids=Array.from(groupTableSelected);
  document.getElementById('group-table-notice').textContent='正在保存所选 '+ids.length+' 项…';
  request('/api/console/groups/batch-access',{method:'PATCH',body:{group_ids:ids,access:button.dataset.batchAccess}}).then(function(result){
    result.items.forEach(function(item){groupTableData.set(item.group_id,item);});groupTableSelected.clear();
    document.getElementById('group-table-notice').textContent='已更新所选群的访问规则';
  }).catch(function(error){document.getElementById('group-table-notice').textContent=error.message;}).finally(function(){groupTableBusy=false;document.getElementById('groups-refresh').disabled=false;renderGroupTableRows();});
});});

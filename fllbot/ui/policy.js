var policyToolValues=[], policyInheritValues=[], policyTriggerModes=null;
function policyField(key){return document.getElementById('policy-'+key);}
function renderPolicyTools(){
  var inherit=policyField('tools-inherit').checked,values=inherit?policyInheritValues:policyToolValues,node=policyField('tools');
  renderToolPicker(node, values, inherit, function(id, checked){
    var selected=new Set(policyToolValues);if(checked)selected.add(id);else selected.delete(id);policyToolValues=Array.from(selected);
  });
}
function renderPolicy(group,providers){
  var policy=group.policy||{},effective=group.effective_policy||{};
  policyTriggerModes=policy.trigger_modes?policy.trigger_modes.slice():null;
  policyField('mode').value=group.policy?'custom':'inherit';policyField('editor').hidden=false;
  document.getElementById('group-detail-form').hidden=false;
  document.getElementById('group-editor-empty').hidden=true;
  document.getElementById('group-bot-context').textContent=(group.self_id?'当前查看 '+(group.bot_nickname||group.self_id)+' 的生效设置。':'')+'本群覆盖对群内所有 Bot 生效，未覆盖的项目各自继承 Bot 默认设置。';
  policyField('name').value=policy.name||'';
  fillOptions('policy-provider',providers,'跟随 Bot 默认模型','name',function(p){return p.name+' · '+p.model;});
  var provider=policyField('provider');
  if(policy.provider&&!Array.from(provider.options).some(function(o){return o.value===policy.provider;}))provider.appendChild(h('option',{value:policy.provider,text:policy.provider+'（当前不可用）'}));
  provider.value=policy.provider||'';
  policyField('response').value=policy.auto_group==null?'inherit':policy.auto_group?'on':'off';
  policyField('trigger').value=policy.trigger_mode||'';
  [['prefix','trigger_prefix'],['cooldown','cooldown_seconds'],['rounds','max_tool_rounds'],['extra','system_extra']].forEach(function(pair){policyField(pair[0]).value=policy[pair[1]]==null?'':policy[pair[1]];});
  policyField('tools-inherit').checked=policy.allowed_tools==null;
  policyToolValues=(policy.allowed_tools||[]).slice();policyInheritValues=((group.inherited_policy||{}).allowed_tools||[]).slice();renderPolicyTools();
  var node=policyField('effective');node.textContent='';
  [['策略',effective.name],['模型',effective.provider+' · '+effective.model],['人格',effective.persona],['回复方式',effective.auto_group?(effective.trigger_modes||[effective.trigger_mode]).map(function(mode){return {at:'被 @',prefix:'唤醒词',all:'自动参与'}[mode];}).join(' / '):'关闭群聊响应'],['用户冷却',effective.cooldown_seconds+' 秒'],['可用工具',(effective.tools||[]).map(function(tool){return tool.title;}).join('、')||'不开放工具']].forEach(function(row){node.appendChild(h('div',{},h('span',{text:row[0]}),h('strong',{text:row[1]||'—'})));});
  if((effective.unavailable_tools||[]).length)node.appendChild(h('p',{class:'policy-state blocked',text:'以下工具当前不可用（插件未加载或已停用）：'+effective.unavailable_tools.join('、')}));
  var blocked=effective.blocked_by||[];node.appendChild(h('p',{class:'policy-state'+(blocked.length?' blocked':''),text:blocked.length?'未就绪：'+blocked.join('；'):'配置就绪；保存后的修改立即生效。'}));
  var reply = 'inherit';
  if(group.llm_enabled===false || policy.auto_group===false)reply='off';
  else if(policy.auto_group===true || policy.trigger_mode && effective.auto_group)reply=effective.trigger_mode || 'at';
  document.getElementById('group-reply-mode').value=reply;
  document.getElementById('group-prefix-row').hidden=reply!=='prefix';
}
function collectPolicy(){
  var response=policyField('response').value;
  function integer(key,min,max){var value=policyField(key).value.trim();if(!value)return null;var n=Number(value);if(!Number.isInteger(n)||n<min||n>max)throw new Error('冷却需在 0–120 秒之间，工具调用轮数需在 1–5 之间。');return n;}
  var policy={name:policyField('name').value.trim(),provider:policyField('provider').value,auto_group:response==='inherit'?null:response==='on',trigger_mode:policyField('trigger').value||null,trigger_modes:policyTriggerModes,cooldown_seconds:integer('cooldown',0,120),max_tool_rounds:integer('rounds',1,5),allowed_tools:policyField('tools-inherit').checked?null:policyToolValues,system_extra:policyField('extra').value.trim()};
  return Object.keys(policy).some(function(key){return policy[key]!==null && policy[key]!=='';})?policy:null;
}
policyField('tools-inherit').addEventListener('change',renderPolicyTools);
document.getElementById('group-reply-mode').addEventListener('change',function(){
  var mode=this.value;policyTriggerModes=null;
  document.getElementById('managed-llm').value=mode==='inherit'?'inherit':mode==='off'?'off':'on';
  policyField('response').value=mode==='inherit'?'inherit':mode==='off'?'off':'on';
  policyField('trigger').value=mode==='inherit'||mode==='off'?'':mode;
  if(mode==='inherit')policyField('prefix').value='';
  document.getElementById('group-prefix-row').hidden=mode!=='prefix';
});

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
  [['prefix','trigger_prefix'],['cooldown','cooldown_seconds'],['auto-reply-probability','auto_reply_probability'],['participation-threshold','participation_threshold'],['extra','system_extra']].forEach(function(pair){policyField(pair[0]).value=policy[pair[1]]==null?'':policy[pair[1]];});
  [['sandbox-write','sandbox_write'],['sandbox-network','sandbox_network'],['sandbox-private-network','sandbox_private_network']].forEach(function(pair){policyField(pair[0]).value=policy[pair[1]]==null?'':policy[pair[1]]?'on':'off';});
  policyField('tools-inherit').checked=policy.allowed_tools==null;
  policyToolValues=(policy.allowed_tools||[]).slice();policyInheritValues=((group.inherited_policy||{}).allowed_tools||[]).slice();policyField('tools').toolPickerState=null;renderPolicyTools();
  var node=policyField('effective');node.textContent='';
  var sourceNames={system:'系统默认',bot:'机器人默认',group:'本群覆盖',legacy_group:'旧版群插件配置',nickname:'机器人昵称',unresolved:'尚未选择机器人'};
  var sources=effective.sources||{};
  var sandbox=effective.sandbox||{};
  [['策略',effective.name],['模型',effective.provider+' · '+effective.model,'provider'],['人格',effective.persona||'兜底提示词','persona'],['群聊响应',effective.auto_group?'开启':'关闭','auto_group'],['回复方式',(effective.trigger_modes||[effective.trigger_mode]).map(function(mode){return {at:'被 @',prefix:'唤醒词',all:'自动参与'}[mode]||mode;}).join(' / '),'trigger_modes'],['唤醒词',effective.trigger_prefix,'trigger_prefix'],['自动参与概率',effective.auto_reply_probability+'%','auto_reply_probability'],['自动参与阈值',effective.participation_threshold,'participation_threshold'],['沙盒',(sandbox.read?'读 ':'')+(sandbox.write?'写 ':'')+(sandbox.network?'联网':'不联网'),'sandbox_write'],['用户冷却',effective.cooldown_seconds+' 秒','cooldown_seconds'],['可用工具',(effective.tools||[]).map(function(tool){return tool.title;}).join('、')||'不开放工具','allowed_tools']].forEach(function(row){
    var value=h('strong',{text:row[1]||'—'});
    if(row[2])value.appendChild(h('small',{class:'policy-source',text:'来源：'+(sourceNames[sources[row[2]]]||'系统默认')}));
    node.appendChild(h('div',{},h('span',{text:row[0]}),value));
  });
  if((effective.unavailable_tools||[]).length)node.appendChild(h('p',{class:'policy-state blocked',text:'以下工具当前不可用（插件未加载或已停用）：'+effective.unavailable_tools.join('、')}));
  var blocked=effective.blocked_by||[],diagnostics=effective.diagnostics;
  node.appendChild(h('p',{class:'policy-state'+(blocked.length?' blocked':''),text:blocked.length?'配置未就绪：'+blocked.join('；'):'配置已就绪；实际连通性以调用结果为准。'}));
  if(diagnostics){
    node.appendChild(h('h4',{text:'不回复诊断（已保存设置）'}));
    (diagnostics.checks||[]).forEach(function(check){
      var row=h('div',{class:'policy-diagnostic'},h('span',{text:(check.passed?'✓ ':'待处理 · ')+check.title}),h('span',{text:check.passed?'检查通过':check.message}));
      if(!check.passed)row.appendChild(h('button',{type:'button',class:'btn',text:'查看设置',onclick:function(){closeGroupDetails();navigate(check.target);}}));
      node.appendChild(row);
    });
    node.appendChild(h('p',{class:'settings-note',text:diagnostics.note}));
  }
  var reply = 'inherit';
  if(group.llm_enabled===false || policy.auto_group===false)reply='off';
  else if(policy.auto_group===true || policy.trigger_mode && effective.auto_group)reply=effective.trigger_mode || 'at';
  document.getElementById('group-reply-mode').value=reply;
  document.getElementById('group-prefix-row').hidden=reply!=='prefix';
}
function collectPolicy(){
  var response=policyField('response').value;
  function integer(key,min,max,message){var value=policyField(key).value.trim();if(!value)return null;var n=Number(value);if(!Number.isInteger(n)||n<min||n>max)throw new Error(message);return n;}
  function tri(key){var value=policyField(key).value;return value===''?null:value==='on';}
  var policy={name:policyField('name').value.trim(),provider:policyField('provider').value,auto_group:response==='inherit'?null:response==='on',trigger_mode:policyField('trigger').value||null,trigger_modes:policyTriggerModes,cooldown_seconds:integer('cooldown',0,120,'冷却需在 0–120 秒之间。'),auto_reply_probability:integer('auto-reply-probability',0,100,'自动参与概率需在 0–100% 之间。'),participation_threshold:integer('participation-threshold',-100,100,'自动参与阈值需在 -100–100 之间。'),sandbox_write:tri('sandbox-write'),sandbox_network:tri('sandbox-network'),sandbox_private_network:tri('sandbox-private-network'),allowed_tools:policyField('tools-inherit').checked?null:policyToolValues,system_extra:policyField('extra').value.trim()};
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

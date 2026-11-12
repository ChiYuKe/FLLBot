var traceCursor=null,traceListRequest=0,traceDetailRequest=0,traceFilters=[];
var traceOutcomes={sent:'已发送',warning:'已发送但有异常',failed:'失败',skipped:'未触发 / 跳过',processed:'处理结束，未观察到发送',cancelled:'取消',dropped:'队列丢弃'};
var traceStages={receive:'接收',queue:'排队',dispatch:'派发',access:'会话准入',plugin:'插件',rule:'规则匹配',permission:'用户权限',cooldown:'冷却',handler:'处理器',trigger:'智能体触发',model:'模型',tool:'工具',send:'QQ 发送',protocol_send:'协议发送',memory:'群记忆',dedup:'消息去重'};
var traceCodes={global_plugin_disabled:'插件已在全局停用，群单独开启无法绕过',context_cleared:'会话数据已清理，旧结果不再保存或发送',not_agent_message:'当前消息不由群聊智能体处理',command_or_empty:'命令或空消息不触发自然语言回复',received:'已接收消息',queued:'已进入队列',queue_overflow:'队列已满，丢弃本消息',shutdown_rejected:'关闭中，不再接收新消息',shutdown_dropped:'关闭超时，丢弃排队消息',duplicate_message:'重复消息，已跳过',scene_denied:'群访问/群开关或私聊开关不允许处理',scene_allowed:'会话允许处理',plugin_not_loaded:'插件未加载',plugin_disabled:'插件已停用',no_handlers:'没有消息处理器',at_required:'此处理器要求被 @',rule_not_matched:'规则未匹配',rule_failed:'规则执行失败',user_not_allowed:'用户权限不足',handler_cooldown:'处理器冷却中',handler_timeout:'处理器执行超时',handler_failed:'处理器执行失败',response_disabled:'群聊响应关闭',addressed_elsewhere:'开头 @ 的是别人，不参与',trigger_not_matched:'未匹配回复方式',probability_skipped:'随机参与概率未命中',participation_below_threshold:'自动参与评分低于阈值，未请求模型',wake_word_ignored:'仅唤醒消息被配置为忽略',fixed_reply_missing:'未配置固定回复',agent_cooldown:'智能体回复冷却中',scene_ai_disabled:'模型或该场景 AI 未开启',trigger_matched:'已匹配回复条件',model_silent:'模型选择不回复',tool_unavailable:'工具不可用',tool_plugin_disabled:'工具所属插件停用',tool_permission_denied:'用户无权调用工具',tool_blocked:'工具被沙盒策略拒绝，未执行',tool_failed:'工具执行失败',provider_selected:'选择模型服务',response_received:'收到模型响应',retry_scheduled:'等待后重试模型请求',fallback_selected:'尝试备用模型服务',completed:'完成',operation_failed:'执行失败；查看对应阶段与运行日志',cancelled:'任务取消',started:'开始'};

function loadTraces(older){
  var params=traceFilters.slice(),id=++traceListRequest,note=document.getElementById('traces-note');
  if(older&&traceCursor)params.push('before='+traceCursor);
  document.getElementById('traces-more').disabled=true;note.textContent='正在读取…';
  return request('/api/console/traces'+(params.length?'?'+params.join('&'):'')).then(function(data){
    if(id!==traceListRequest)return;
    var list=document.getElementById('traces-list');if(!older)list.textContent='';
    data.items.forEach(function(item){list.appendChild(h('button',{type:'button',class:'room-item trace-list-item','data-trace-id':item.trace_id,onclick:function(){loadTraceDetail(item.trace_id);}},
      h('strong',{text:traceOutcomes[item.outcome]||item.outcome}),h('span',{text:(item.scene==='group'?'群 ':'私聊 ')+item.scene_id+' · Bot '+item.self_id}),
      h('small',{text:new Date(item.started_at*1000).toLocaleString('zh-CN')+' · '+item.duration_ms+' ms'})));});
    if(!data.items.length&&!older)list.appendChild(h('p',{class:'settings-note readme-body',text:'暂无符合条件的记录；刚处理的消息请稍后刷新。'}));
    traceCursor=data.next_cursor;document.getElementById('traces-more').hidden=!traceCursor;
    var state=data.recorder;note.textContent='待写入 '+state.pending+' 条 · 诊断队列丢弃 '+state.dropped+' 条 · 写入失败 '+state.write_failures+' 条（本次运行）。记录失败不会阻断机器人回复。';
  }).catch(function(error){if(id===traceListRequest)note.textContent='读取失败：'+error.message;}).finally(function(){if(id===traceListRequest)document.getElementById('traces-more').disabled=false;});
}
function loadTraceDetail(traceId){
  document.querySelectorAll('.trace-list-item').forEach(function(button){var active=button.dataset.traceId===traceId;button.classList.toggle('active',active);if(active)button.setAttribute('aria-current','true');else button.removeAttribute('aria-current');});
  var id=++traceDetailRequest,node=document.getElementById('trace-detail');node.textContent='正在读取…';
  return request('/api/console/traces/'+traceId).then(function(item){
    if(id!==traceDetailRequest)return;node.textContent='';
    node.appendChild(h('p',{text:(traceOutcomes[item.outcome]||item.outcome)+' · '+item.duration_ms+' ms'}));
    var failures=Array.from(new Set(item.steps.filter(function(step){return step.status==='failed';}).map(function(step){return traceStages[step.stage]||step.stage;})));
    if(failures.length)node.appendChild(h('p',{class:'trace-summary-error',text:'异常阶段：'+failures.join('、')}));
    var skipped=item.steps.filter(function(step){return step.status==='skipped'&&['access','trigger','queue','dedup'].includes(step.stage);});
    if(!failures.length&&skipped.length){var reason=skipped[skipped.length-1];node.appendChild(h('p',{text:'处理说明：'+(traceCodes[reason.code]||reason.code)}));}
    node.appendChild(h('p',{class:'settings-note',text:'诊断 ID：'+item.trace_id+' · 消息 ID：'+item.message_id}));
    node.appendChild(h('button',{type:'button',class:'btn',text:'在近期运行日志中查找',onclick:function(){navigate('logs');document.getElementById('log-search').value=item.trace_id;renderLogs(allLogs);}}));
    node.appendChild(h('p',{class:'settings-note',text:'发送成功表示观察到调用成功，不保证最终送达或已读；处理结束也可能由插件直接执行其他动作。'}));
    if(item.truncated)node.appendChild(h('p',{class:'settings-note',text:'阶段记录超过 100 项，后续明细已截断；总结果仍汇总全部阶段。'}));
    var list=h('ol',{class:'trace-steps'});
    item.steps.forEach(function(step){list.appendChild(h('li',{class:'trace-step trace-'+step.status},
      h('strong',{text:(traceStages[step.stage]||step.stage)+' · '+({passed:'通过',failed:'失败',skipped:'跳过',cancelled:'取消',running:'执行中'}[step.status]||step.status)}),
      h('p',{text:traceCodes[step.code]||step.code}),h('small',{text:(step.component?step.component+' · ':'')+'开始于 '+step.elapsed_ms+' ms'+(step.duration_ms==null?'':' · 耗时 '+step.duration_ms+' ms')+' · '+step.code})));});
    node.appendChild(list);
  }).catch(function(error){if(id===traceDetailRequest)node.textContent=error.message;});
}
document.getElementById('traces-filter').addEventListener('submit',function(event){
  event.preventDefault();var params=[];
  try{
    [['traces-bot','self_id'],['traces-scene-id','scene_id'],['traces-message-id','message_id']].forEach(function(pair){var value=document.getElementById(pair[0]).value.trim();if(value){if(!/^\d+$/.test(value)||!Number.isSafeInteger(Number(value))||Number(value)<=0)throw new Error('账号、会话和消息 ID 必须是正整数。');params.push(pair[1]+'='+value);}});
    [['traces-scene','scene'],['traces-outcome','outcome']].forEach(function(pair){var value=document.getElementById(pair[0]).value;if(value)params.push(pair[1]+'='+encodeURIComponent(value));});
  }catch(error){document.getElementById('traces-note').textContent=error.message;return;}
  traceFilters=params;loadTraces(false);
});
document.getElementById('traces-refresh').addEventListener('click',function(){loadTraces(false);});
document.getElementById('traces-more').addEventListener('click',function(){loadTraces(true);});

var turnListRequest=0,turnDetailRequest=0,turnCursor=null,turnFilters=[],turnItems=[];
var turnStates={queued:'排队中',running:'执行中',reply_ready:'回复已生成',delivering:'发送中',delivered:'已送达',ignored:'已忽略',silent:'静默结束',failed:'失败',cancelled:'已取消',expired:'已过期',delivery_uncertain:'送达不确定',superseded:'已改用替代发送方式',pending:'待发送',sending:'发送中',sent:'发送成功',uncertain:'不确定',succeeded:'工具成功'};
var turnReasons={global_queue_full:'全局会话容量已满',conversation_queue_full:'当前会话排队已满',queue_expired:'排队等待已过期',admin_debug_completed:'管理调试完成，未发送 QQ',model_failed:'模型请求失败',execution_failed:'执行失败',empty_response:'模型返回空白',explicit_silence:'模型决定静默',tool_completed:'工具已完成对外动作',tool_limit:'工具轮数耗尽',partial_delivery:'部分内容未送达',delivery_failed:'发送失败',delivery_uncertain:'无法确认送达',deadline_exceeded:'超过处理时限',admin_cancelled:'管理员取消',user_cancelled:'发起者取消',account_disabled:'账号已停用',context_cleared:'会话数据已清理',process_restart:'进程重启中断',delivery_cancelled:'发送已取消',no_reply:'无文字回复'};
function loadTurns(more){
  var id=++turnListRequest,params=turnFilters.slice(),note=document.getElementById('turns-note');
  if(more&&turnCursor)params.push('before='+turnCursor);
  if(!more){turnItems=[];turnCursor=null;++turnDetailRequest;document.getElementById('turn-detail').textContent='选择一个轮次查看详情。';}
  note.textContent='正在读取轮次…';
  return request('/api/console/turns?'+params.join('&')).then(function(data){
    if(id!==turnListRequest)return;
    turnItems=more?turnItems.concat(data.items):data.items;turnCursor=data.next_cursor;
    var list=document.getElementById('turns-list');list.textContent='';
    turnItems.forEach(function(item){list.appendChild(h('button',{type:'button',class:'room-item turn-list-item',onclick:function(){showTurn(item.turn_id);}},h('strong',{text:(turnStates[item.status]||item.status)+' · '+item.scene_id}),h('small',{text:'Bot '+item.self_id+' · '+new Date(item.created_at*1000).toLocaleString()})));});
    if(!turnItems.length)list.appendChild(h('p',{class:'empty',text:'没有符合筛选的轮次。'}));
    document.getElementById('turns-more').hidden=!turnCursor;
    var metrics=data.delivery_metrics||{},parts=metrics.parts||{};
    note.textContent='当前筛选的全部轮次：成功 '+(parts.sent||0)+' 段 · 失败 '+(parts.failed||0)+' 段 · 送达不确定 '+(parts.uncertain||0)+' 段 · 工具不确定 '+(metrics.uncertain_tools||0)+' 项'+(metrics.confirmed_delivery_rate===null||metrics.confirmed_delivery_rate===undefined?'':' · 确认送达率 '+Math.round(metrics.confirmed_delivery_rate*100)+'%（成功/成功、失败与不确定总段数）');
  }).catch(function(error){if(id===turnListRequest)note.textContent=error.message;});
}
function showTurn(turnId){
  var id=++turnDetailRequest,node=document.getElementById('turn-detail');node.textContent='正在读取详情…';
  return request('/api/console/turns/'+encodeURIComponent(turnId)).then(function(item){
    if(id!==turnDetailRequest)return;node.textContent='';
    node.appendChild(h('h3',{text:turnStates[item.status]||item.status}));
    node.appendChild(h('p',{text:turnReasons[item.reason]||item.reason||'没有异常原因'}));
    node.appendChild(h('p',{class:'settings-note',text:'轮次 '+item.turn_id+' · Bot '+item.self_id+' · '+(item.entrypoint==='admin_debug'?'管理调试':'QQ')+' · '+(item.scene==='group'?'群聊':'私聊')+' '+item.scene_id}));
    var phases={step:'任务步骤',model:'请求模型',tool:'执行工具',compaction:'整理上下文',repeat_reminder:'重复调用提醒',steering:'补充要求'};
    var progressStates={running:'进行中',succeeded:'完成',failed:'失败',cancelled:'取消',interrupted:'中断',skipped:'跳过',superseded:'已按补充要求调整',queued:'等待应用'};
    if((item.progress||[]).length){
      node.appendChild(h('h4',{text:'任务进度'}));
      item.progress.forEach(function(event){node.appendChild(h('p',{text:'第 '+event.step+' 步 · '+(phases[event.phase]||event.phase)+' · '+(progressStates[event.status]||event.status)+(event.component?' · '+event.component:'')}));});
      if(item.can_cancel)node.appendChild(h('button',{type:'button',class:'btn',text:'刷新任务进度',onclick:function(){showTurn(turnId);}}));
    }
    node.appendChild(h('h4',{text:'模型请求'}));
    var timings=item.trace_metrics||{},labels={queue_wait_ms:'排队',first_model_response_ms:'首次模型响应',first_delivery_ms:'首次送达'};
    Object.keys(labels).forEach(function(key){if(timings[key]!==undefined&&timings[key]!==null)node.appendChild(h('p',{class:'settings-note',text:labels[key]+' '+Math.round(timings[key])+' ms'}));});
    var context=item.context_metadata||{};
    if(context.request_version){
      node.appendChild(h('p',{class:'settings-note',text:'上下文版本 '+context.request_version.slice(0,12)+' · 历史截止 '+new Date(context.history_cutoff*1000).toLocaleString()+' · 工具 '+(context.tool_names||[]).length+' 项'}));
      ((context.budget||{}).providers||[]).forEach(function(budget){node.appendChild(h('p',{class:'settings-note',text:budget.provider+' / '+budget.model+' · 输入估算 '+budget.input_estimate+' · 输出预留 '+budget.output_reserve+' · 窗口 '+budget.window}));});
    }
    if(!(item.model_requests||[]).length)node.appendChild(h('p',{text:'没有模型请求记录。'}));
    (item.model_requests||[]).forEach(function(model){
      var cached=model.cached_prompt_tokens,cacheText=cached==null?' · 缓存未报告':' · 输入缓存 '+cached+' Token'+(model.prompt_tokens?'（'+(cached/model.prompt_tokens*100).toFixed(1)+'%）':'');
      node.appendChild(h('p',{text:model.provider+' / '+model.model+' · '+(model.ok?'生成成功':'请求失败')+' · '+((model.prompt_tokens||0)+(model.completion_tokens||0))+' Token'+(model.usage_estimated?'（估算）':'')+cacheText+' · '+Math.round(model.latency_ms||0)+' ms'}));
    });
    node.appendChild(h('h4',{text:'分段回执'}));
    if(!item.deliveries.length)node.appendChild(h('p',{text:'尚无回复发送记录。'}));
    if(item.deliveries.some(function(part){return part.bodies_cleared;}))node.appendChild(h('p',{class:'settings-note',text:'回复正文已清理，发送回执仍保留。'}));
    item.deliveries.forEach(function(part){node.appendChild(h('p',{text:'第 '+(part.part_index+1)+' 段 · '+(turnStates[part.status]||part.status)+(part.message_id?' · 消息 '+part.message_id:'')+(part.error?' · '+part.error:'')}));});
    node.appendChild(h('h4',{text:'工具执行'}));
    if(!item.tools.length)node.appendChild(h('p',{text:'没有工具执行记录。'}));
    item.tools.forEach(function(tool){node.appendChild(h('p',{text:tool.tool+' · '+(turnStates[tool.status]||tool.status)+(tool.error?' · '+tool.error:'')}));});
    node.appendChild(h('p',{class:'settings-note',text:'不确定的发送不能直接重发。已执行动作不能通过取消撤回。'}));
    node.appendChild(h('button',{type:'button',class:'btn',text:'刷新此轮次',onclick:function(){showTurn(turnId);}}));
    if(item.can_retry)node.appendChild(h('button',{type:'button',class:'btn',text:'仅补发明确未送达的分段',onclick:function(){
      var button=this;button.disabled=true;
      request('/api/console/turns/'+encodeURIComponent(turnId)+'/recover-delivery',{method:'POST'}).then(function(){toast('分段恢复完成。');return showTurn(turnId);}).catch(function(error){toast(error.message);showTurn(turnId);});
    }}));
    if(item.can_cancel)node.appendChild(h('button',{type:'button',class:'btn',text:'取消后续处理',onclick:function(){
      var button=this;button.disabled=true;
      request('/api/console/turns/'+encodeURIComponent(turnId)+'/cancel',{method:'POST'}).then(function(){toast('已请求取消；已执行动作保留记录。');return showTurn(turnId);}).catch(function(error){toast(error.message);button.disabled=false;});
    }}));
  }).catch(function(error){if(id===turnDetailRequest)node.textContent=error.message;});
}
document.getElementById('turns-filter').addEventListener('submit',function(event){
  event.preventDefault();var params=[];
  try{[['turns-bot','self_id'],['turns-scene-id','scene_id']].forEach(function(pair){var value=document.getElementById(pair[0]).value.trim();if(value){if(!/^\d+$/.test(value)||!Number.isSafeInteger(Number(value))||Number(value)<=0)throw new Error('账号和会话号码必须是正整数。');params.push(pair[1]+'='+value);}});}catch(error){document.getElementById('turns-note').textContent=error.message;return;}
  var status=document.getElementById('turns-status').value;if(status)params.push('status='+encodeURIComponent(status));turnFilters=params;loadTurns(false);
});
document.getElementById('turns-refresh').addEventListener('click',function(){loadTurns(false);});
document.getElementById('turns-more').addEventListener('click',function(){loadTurns(true);});

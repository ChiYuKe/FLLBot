var taskListRequest=0,taskListCursor=null,taskListScope=null,taskRunRequest=0,taskRunCursor=null,taskSelected=null,taskActionBusy=false,taskSourcesRequest=0;
var taskStateLabels={pending:'未完成',paused:'已暂停',completed:'已完成',cancelled:'已取消'};
var taskDeliveryLabels={waiting:'等待发送',sending:'正在发送',sent:'已收到送达回执',uncertain:'送达不确定，已停止自动重试'};
var taskActionLabels={pause:'暂停',resume:'恢复',cancel:'取消任务',complete:'标为完成',retry:'确认后重试'};
var taskCodes={plugin_disabled:'插件已停用',group_denied:'群访问或群开关不允许发送',private_denied:'私聊未开放',bot_offline:'指定 Bot 未连接',delivery_started:'已提交发送',receipt_received:'已收到发送回执',receipt_missing:'未取得回执，可能已经送达',interrupted:'执行中断，可能已经送达',retry_requested:'管理员已允许重试',pause:'管理员暂停',resume:'管理员恢复',cancel:'任务取消',complete:'标为完成'};
function loadTaskSources(){
  var id=++taskSourcesRequest;
  return request('/api/console/task-sources').then(function(data){
    if(id!==taskSourcesRequest)return;var select=document.getElementById('tasks-source'),previous=select.value;select.textContent='';
    data.items.forEach(function(item){select.appendChild(h('option',{value:item.id,text:item.title+' · '+item.plugin+(item.enabled?'':'（已停用）')}));});
    if(data.items.some(function(item){return item.id===previous;}))select.value=previous;
    if(!data.items.length){++taskListRequest;document.getElementById('tasks-list').textContent='暂无已加载的任务来源。请安装或加载声明任务管理能力的插件。';document.getElementById('tasks-more').hidden=true;return;}
    loadTasks(false);
  }).catch(function(error){document.getElementById('tasks-note').textContent=error.message;});
}
function taskFilter(){
  var params=new URLSearchParams(),scene=document.getElementById('tasks-scene').value,state=document.getElementById('tasks-state').value;
  if(scene)params.set('scene',scene);if(state)params.set('state',state);
  [['tasks-bot','self_id'],['tasks-scene-id','scene_id'],['tasks-owner','owner']].forEach(function(pair){var value=document.getElementById(pair[0]).value.trim();if(value){if(!/^\d+$/.test(value)||!Number.isSafeInteger(Number(value))||Number(value)<=0)throw new Error('QQ 和会话号必须是正整数');params.set(pair[1],value);}});
  if(params.has('scene_id')&&!scene)throw new Error('指定会话号时请选择场景');return params;
}
function loadTasks(older){
  var source=document.getElementById('tasks-source').value;if(!source)return;
  var params;try{params=taskFilter();}catch(error){document.getElementById('tasks-note').textContent=error.message;return;}
  var scope=source+'?'+params;
  if(older&&scope!==taskListScope)older=false;
  if(older&&taskListCursor)params.set('before',taskListCursor);
  var id=++taskListRequest;
  return request('/api/console/tasks/'+encodeURIComponent(source)+'?'+params).then(function(data){
    if(id!==taskListRequest)return;var node=document.getElementById('tasks-list');if(!older)node.textContent='';
    data.items.forEach(function(task){
      var actions=h('div',{class:'task-actions'}),due=task.due_at==null?'无定时提醒（待办）':new Date(task.due_at*1000).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai'})+' UTC+08:00';
      (task.actions||[]).forEach(function(action){actions.appendChild(h('button',{class:'btn',type:'button',text:taskActionLabels[action],disabled:taskActionBusy?'':null,onclick:function(){changeTask(source,task,action);}}));});
      actions.appendChild(h('button',{class:'btn',type:'button',text:'运行记录',onclick:function(){taskSelected={source:source,id:task.id};loadTaskRuns(false);}}));
      node.appendChild(h('div',{class:'maintenance-row'},h('div',{},h('strong',{text:'#'+task.id+' · '+(taskStateLabels[task.state]||task.state)+' · '+(task.due_at==null?'待办':taskDeliveryLabels[task.delivery]||task.delivery)}),h('p',{text:task.content}),h('p',{class:'settings-note',text:'Bot '+task.self_id+' · '+(task.scene==='group'?'群聊 ':'私聊 ')+task.scene_id+' · 创建者 '+task.owner}),h('p',{class:'settings-note',text:due}),h('p',{class:'settings-note',text:task.last_code?'上次状态：'+(taskCodes[task.last_code]||task.last_code):''}),actions)));
    });
    if(!older&&!data.items.length)node.textContent='当前筛选没有任务。任务通过插件的提醒或待办工具创建。';
    taskListScope=scope;taskListCursor=data.next_cursor;document.getElementById('tasks-more').hidden=!taskListCursor;
    document.getElementById('tasks-note').textContent='列表为当前快照。发送前重新检查连接、插件和场景权限；不确定的发送结果需明确确认后重试。插件文件数据不包含在主数据库备份中。';
  }).catch(function(error){if(id===taskListRequest)document.getElementById('tasks-note').textContent=error.message;});
}
function changeTask(source,task,action){
  if(taskActionBusy)return;
  if(action==='retry'&&!window.confirm('上次未收到回执，消息可能已经送达。重试可能重复发送给 Bot '+task.self_id+' 的'+(task.scene==='group'?'群 ':'私聊 ')+task.scene_id+'。确定允许重试任务 #'+task.id+'？'))return;
  if(action==='cancel'&&!window.confirm('取消任务 #'+task.id+' 的后续执行？已发送的消息无法撤回。'))return;
  taskActionBusy=true;document.querySelectorAll('#tasks-list button').forEach(function(button){button.disabled=true;});
  request('/api/console/tasks/'+encodeURIComponent(source)+'/'+task.id+'/actions',{method:'POST',body:{action:action,revision:task.revision,acknowledge_duplicate:action==='retry'}}).then(function(){return loadTasks(false);}).then(function(){if(taskSelected&&taskSelected.id===task.id&&taskSelected.source===source)loadTaskRuns(false);}).catch(function(error){document.getElementById('tasks-note').textContent=error.message+'；请刷新后核对，勿重复提交。';}).finally(function(){taskActionBusy=false;document.querySelectorAll('#tasks-list button').forEach(function(button){button.disabled=false;});});
}
function loadTaskRuns(older){
  if(!taskSelected)return;var selected=taskSelected,id=++taskRunRequest,params=older&&taskRunCursor?'?before='+taskRunCursor:'';
  document.getElementById('task-runs-title').textContent='任务 #'+selected.id+' · 运行记录';
  return request('/api/console/tasks/'+encodeURIComponent(selected.source)+'/'+selected.id+'/runs'+params).then(function(data){
    if(id!==taskRunRequest)return;var node=document.getElementById('task-runs');if(!older)node.textContent='';
    data.items.forEach(function(run){node.appendChild(h('p',{text:new Date(run.created_at*1000).toLocaleString('zh-CN')+' · '+(taskCodes[run.code]||run.code)+' · '+(taskDeliveryLabels[run.status]||({action:'管理操作',blocked:'等待条件满足'}[run.status])||run.status)}));});
    if(!older&&!data.items.length)node.textContent='尚无运行记录。旧任务迁移不补造历史发送记录。';
    taskRunCursor=data.next_cursor;document.getElementById('task-runs-more').hidden=!taskRunCursor;
  }).catch(function(error){if(id===taskRunRequest)document.getElementById('task-runs').textContent=error.message;});
}
document.getElementById('tasks-filter').addEventListener('submit',function(event){event.preventDefault();loadTasks(false);});
document.getElementById('tasks-source').addEventListener('change',function(){++taskRunRequest;taskSelected=null;document.getElementById('task-runs-title').textContent='运行记录';document.getElementById('task-runs').textContent='选择任务查看记录。';document.getElementById('task-runs-more').hidden=true;loadTasks(false);});
document.getElementById('tasks-refresh').addEventListener('click',function(){loadTaskSources();if(taskSelected)loadTaskRuns(false);});
document.getElementById('tasks-more').addEventListener('click',function(){loadTasks(true);});
document.getElementById('task-runs-more').addEventListener('click',function(){loadTaskRuns(true);});

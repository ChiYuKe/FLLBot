var retentionRevision=null,retentionLoaded=false,cleanupPreviewId=null,cleanupPreviewRequest=0,cleanupJobId=sessionStorage.getItem('fllbot-cleanup-job')||null,cleanupPollGeneration=0,cleanupRunCursor=null,cleanupRunRequest=0;
var cleanupLabels={message_log:'消息记录',chat_messages:'AI 会话记录',group_memory_messages:'群记忆原文',group_memory_summaries:'群记忆摘要',llm_usage:'模型用量',message_traces:'消息诊断',management_changes:'配置变更记录'};
['input','change'].forEach(function(type){document.getElementById('retention-form').addEventListener(type,function(){dirtyPages.add('retention');updateDirtyState();});});
function loadRetention(force){
  loadCleanupRuns(false);if(cleanupJobId)pollCleanupJob(cleanupJobId,++cleanupPollGeneration);
  if(dirtyPages.has('retention')&&!force)return;
  document.getElementById('retention-save').disabled=true;
  return request('/api/console/data-retention').then(function(data){
    if(dirtyPages.has('retention'))return;
    retentionRevision=data.revision;retentionLoaded=true;
    document.getElementById('retention-enabled').checked=data.policy.enabled;
    ['messages','conversations','usage','changes'].forEach(function(key){document.getElementById('retention-'+key).value=data.policy[key+'_days'];});
    document.getElementById('retention-interval').value=data.policy.interval_seconds;
    document.getElementById('retention-note').textContent=data.note;
  }).catch(function(error){document.getElementById('retention-note').textContent=error.message;}).finally(function(){document.getElementById('retention-save').disabled=!retentionLoaded;});
}
document.getElementById('retention-reload').addEventListener('click',function(){if(dirtyPages.has('retention')&&!window.confirm('重新读取会放弃尚未保存的保留策略，继续吗？'))return;markSaved('retention');loadRetention(true);});
document.getElementById('retention-form').addEventListener('submit',function(event){
  event.preventDefault();if(!retentionLoaded||!this.reportValidity())return;
  var values={enabled:document.getElementById('retention-enabled').checked,interval_seconds:Number(document.getElementById('retention-interval').value)};
  ['messages','conversations','usage','changes'].forEach(function(key){values[key+'_days']=Number(document.getElementById('retention-'+key).value);});
  var button=document.getElementById('retention-save');button.disabled=true;
  request('/api/console/data-retention',{method:'PUT',headers:retentionRevision?{'If-Match':retentionRevision}:{},body:values}).then(function(data){retentionRevision=data.revision;markSaved('retention');document.getElementById('retention-note').textContent='策略已保存；今后的定时清理按此执行，当前未立即删除数据。';}).catch(function(error){document.getElementById('retention-note').textContent=error.message;}).finally(function(){button.disabled=false;});
});
function invalidateCleanupPreview(){++cleanupPreviewRequest;cleanupPreviewId=null;document.getElementById('cleanup-confirm').checked=false;document.getElementById('cleanup-confirm').disabled=true;document.getElementById('cleanup-execute').disabled=true;document.getElementById('cleanup-preview-result').hidden=true;}
document.getElementById('cleanup-scope-form').addEventListener('input',invalidateCleanupPreview);
document.getElementById('cleanup-scope-form').addEventListener('change',invalidateCleanupPreview);
document.getElementById('cleanup-scope-form').addEventListener('submit',function(event){
  event.preventDefault();if(!this.reportValidity())return;invalidateCleanupPreview();
  var requestId=cleanupPreviewRequest,values={datasets:Array.from(document.querySelectorAll('[name="cleanup-dataset"]:checked')).map(function(input){return input.value;}),older_than_days:Number(document.getElementById('cleanup-days').value)};
  var note=document.getElementById('cleanup-job-note');
  if(!values.datasets.length){note.textContent='请先选择需要清理的数据类别。';return;}
  try{
    [['cleanup-scene-id','scene_id'],['cleanup-bot','self_id']].forEach(function(pair){var value=document.getElementById(pair[0]).value.trim();if(value){if(!/^\d+$/.test(value)||!Number.isSafeInteger(Number(value))||Number(value)<=0)throw new Error('会话号和 Bot QQ 必须是正整数');values[pair[1]]=Number(value);}});
    var scene=document.getElementById('cleanup-scene').value;if(scene)values.scene=scene;
    if(values.scene_id&&!scene)throw new Error('指定会话号时请选择群聊或私聊');
    if(values.self_id&&values.datasets.some(function(key){return !['messages','traces'].includes(key);}))throw new Error('指定 Bot 时只能选择消息记录和消息诊断');
  }catch(error){note.textContent=error.message;return;}
  note.textContent='正在只读预览，不删除数据…';
  request('/api/console/data-cleanup/preview',{method:'POST',body:values}).then(function(data){
    if(requestId!==cleanupPreviewRequest)return;
    var node=document.getElementById('cleanup-preview-result');node.hidden=false;node.textContent='';
    var spec=data.specification,cutoff=Object.values(spec.cutoffs)[0];
    node.appendChild(h('h3',{text:'本次预览：'+data.total+' 条'}));
    node.appendChild(h('p',{class:'settings-note',text:'早于 '+new Date(cutoff*1000).toLocaleString('zh-CN')+' · '+(spec.scene==='group'?'群聊':spec.scene==='private'?'私聊':'全部场景')+(spec.scene_id?' '+spec.scene_id:' · 全部会话')+(spec.self_id?' · Bot '+spec.self_id:' · 全部 Bot')}));
    var counts=h('ul',{});Object.keys(data.counts).forEach(function(key){counts.appendChild(h('li',{text:(cleanupLabels[key]||key)+'：'+data.counts[key]+' 条'}));});node.appendChild(counts);
    note.textContent=data.total>data.max_rows?'数量超过单次上限，请缩小时间或会话范围。':data.total?'请核对以上范围与数量，勾选确认后提交。':'没有符合范围的记录。';
    cleanupPreviewId=data.can_execute?data.id:null;document.getElementById('cleanup-confirm').disabled=!cleanupPreviewId||!!cleanupJobId;
  }).catch(function(error){if(requestId===cleanupPreviewRequest)note.textContent=error.message;});
});
document.getElementById('cleanup-confirm').addEventListener('change',function(){document.getElementById('cleanup-execute').disabled=!this.checked||!cleanupPreviewId||!!cleanupJobId;});
document.getElementById('cleanup-execute').addEventListener('click',function(){
  if(!cleanupPreviewId||!document.getElementById('cleanup-confirm').checked||cleanupJobId)return;
  this.disabled=true;document.getElementById('cleanup-confirm').disabled=true;
  request('/api/console/data-cleanup',{method:'POST',body:{preview_id:cleanupPreviewId,confirm:true}}).then(function(job){cleanupPreviewId=null;cleanupJobId=job.id;sessionStorage.setItem('fllbot-cleanup-job',job.id);pollCleanupJob(job.id,++cleanupPollGeneration);}).catch(function(error){document.getElementById('cleanup-job-note').textContent=error.message;invalidateCleanupPreview();});
});
function pollCleanupJob(jobId,generation){
  request('/api/console/data-cleanup/jobs/'+jobId).then(function(job){
    if(generation!==cleanupPollGeneration)return;
    if(job.status==='running'||job.status==='pending'){document.getElementById('cleanup-job-note').textContent='清理作业执行中；可以切换页面。';setTimeout(function(){if(generation===cleanupPollGeneration)pollCleanupJob(jobId,generation);},1500);return;}
    document.getElementById('cleanup-job-note').textContent=job.status==='completed'?'清理已完成：'+job.result.total+' 条；详细数量见下方持久化结果。':job.error||'清理未完成，请核对结果';
    cleanupJobId=null;sessionStorage.removeItem('fllbot-cleanup-job');invalidateCleanupPreview();loadCleanupRuns(false);
  }).catch(function(error){if(generation!==cleanupPollGeneration)return;document.getElementById('cleanup-job-note').textContent='查询状态失败：'+error.message+'。不要重复提交，请刷新清理结果核对。';if(error.status===404){cleanupJobId=null;sessionStorage.removeItem('fllbot-cleanup-job');}});
}
function loadCleanupRuns(older){
  var id=++cleanupRunRequest,query=older&&cleanupRunCursor?'?before='+cleanupRunCursor:'';
  return request('/api/console/data-cleanup/runs'+query).then(function(data){
    if(id!==cleanupRunRequest)return;var node=document.getElementById('cleanup-runs');if(!older)node.textContent='';
    data.items.forEach(function(run){
      var scope=run.specification,labels={messages:'消息记录',conversations:'AI 会话和群记忆',usage:'模型用量',traces:'消息诊断',changes:'配置变更'},cutoffs=Object.keys(scope.cutoffs).map(function(key){return labels[key]+'：早于 '+new Date(scope.cutoffs[key]*1000).toLocaleString('zh-CN');}).join('；');
      node.appendChild(h('div',{class:'maintenance-row'},h('div',{},h('strong',{text:new Date(run.created_at*1000).toLocaleString('zh-CN')+' · '+(run.kind==='automatic'?'定时清理':'手动清理')}),h('p',{class:'settings-note',text:(scope.scene?(scope.scene==='group'?'群聊':'私聊')+(scope.scene_id?' '+scope.scene_id:''):'全部场景')+(scope.self_id?' · Bot '+scope.self_id:' · 全部 Bot')}),h('p',{class:'settings-note',text:cutoffs}),h('p',{class:'settings-note',text:Object.keys(run.counts).map(function(key){return (cleanupLabels[key]||key)+' '+run.counts[key]+' 条';}).join('、')}))));
    });
    if(!older&&!data.items.length)node.appendChild(h('p',{class:'settings-note',text:'暂无已提交的清理结果。未删除数据的检查不生成结果记录。'}));
    cleanupRunCursor=data.next_cursor;document.getElementById('cleanup-runs-more').hidden=!cleanupRunCursor;
  }).catch(function(error){if(id===cleanupRunRequest)document.getElementById('cleanup-runs').textContent=error.message;});
}
document.getElementById('cleanup-runs-refresh').addEventListener('click',function(){loadCleanupRuns(false);if(cleanupJobId)pollCleanupJob(cleanupJobId,++cleanupPollGeneration);});
document.getElementById('cleanup-runs-more').addEventListener('click',function(){loadCleanupRuns(true);});

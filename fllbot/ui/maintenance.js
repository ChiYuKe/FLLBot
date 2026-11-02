var serviceCheckBusy=false,serviceHealthRequest=0,backupCursor=null,backupListRequest=0,backupPollGeneration=0,restoreGuideRequest=0;
var backupJobId=sessionStorage.getItem('fllbot-backup-job')||null;
var serviceStates={unknown:'未检查',ready:'就绪',loading:'加载中',idle:'空闲卸载',unavailable:'不可连接',error:'异常'};
var serviceReasons={not_checked:'点击检查后读取服务状态',ready:'服务报告就绪',loading:'模型正在加载，请稍后检查',idle:'模型已卸载释放资源；下次生成时自动加载',timeout:'健康检查超时',connection_failed:'无法连接或健康接口返回错误',service_unhealthy:'服务报告未就绪或内部异常',invalid_response:'健康接口返回格式不符合约定',check_failed:'检查失败，请查看插件与服务日志',checked:'检查结束'};
function renderServiceHealth(data){
  var node=document.getElementById('service-health-list');node.textContent='';
  document.getElementById('service-health-note').textContent=(data.checking?'健康检查正在执行。':'')+data.note;
  document.getElementById('service-check-all').disabled=serviceCheckBusy||data.checking||!data.items.length;
  data.items.forEach(function(item){
    var button=h('button',{type:'button',class:'btn',text:'检查健康接口',onclick:function(){checkServiceHealth(item.id);}});button.disabled=serviceCheckBusy||data.checking;
    node.appendChild(h('div',{class:'maintenance-row'},h('div',{},h('strong',{text:item.title+' · '+(item.checking?'检查中':serviceStates[item.state]||item.state)}),
      h('p',{class:'settings-note',text:'来源：'+item.plugin+(item.selected?' · 当前选用':' · 未选用')+(item.plugin_enabled?'':' · 插件全局停用')}),
      h('p',{class:'settings-note',text:(serviceReasons[item.code]||item.code)+(item.checked_at?' · '+new Date(item.checked_at*1000).toLocaleString('zh-CN')+' · '+item.latency_ms+' ms':'')+(item.stale?' · 状态已过期，请重新检查':'')})),button));
  });
  if(!data.items.length)node.appendChild(h('p',{class:'settings-note',text:'当前已加载插件未声明外部服务检查。语音插件加载后会提供对应入口。'}));
}
function loadServiceHealth(){
  var id=++serviceHealthRequest;
  return request('/api/console/services').then(function(data){if(id===serviceHealthRequest)renderServiceHealth(data);}).catch(function(error){if(id===serviceHealthRequest)document.getElementById('service-health-note').textContent=error.message;});
}
function checkServiceHealth(serviceId){
  if(serviceCheckBusy)return;serviceCheckBusy=true;++serviceHealthRequest;
  document.getElementById('service-health-note').textContent='正在检查健康接口，不生成语音或请求模型回答…';
  document.querySelectorAll('#service-health-list button,#service-check-all').forEach(function(button){button.disabled=true;});
  request('/api/console/services/check',{method:'POST',body:serviceId?{service_id:serviceId}:{}}).then(function(data){serviceCheckBusy=false;++serviceHealthRequest;renderServiceHealth(data);}).catch(function(error){serviceCheckBusy=false;++serviceHealthRequest;document.getElementById('service-health-note').textContent=error.message;document.getElementById('service-check-all').disabled=false;document.querySelectorAll('#service-health-list button').forEach(function(button){button.disabled=false;});});
}
document.getElementById('service-check-all').addEventListener('click',function(){checkServiceHealth(null);});
document.getElementById('service-health-refresh').addEventListener('click',loadServiceHealth);

function loadBackups(older){
  var id=++backupListRequest,query=older&&backupCursor?'?before='+encodeURIComponent(backupCursor):'';
  document.getElementById('backup-list-note').textContent='正在读取备份列表…';
  return request('/api/console/backups'+query).then(function(data){
    if(id!==backupListRequest)return;
    var node=document.getElementById('backup-list');if(!older)node.textContent='';
    document.getElementById('backup-scope-note').textContent=data.note;
    document.getElementById('backup-create').disabled=data.active;
    data.items.forEach(function(item){
      var note=h('p',{class:'settings-note',text:(item.size_bytes/1024/1024).toFixed(2)+' MB · '+new Date(item.modified_at*1000).toLocaleString('zh-CN')+' · '+(item.validation==='valid'?'已校验 · 数据库版本 '+item.schema_version:'尚未校验')});
      var validate=h('button',{type:'button',class:'btn',text:'校验',onclick:function(){
        validate.disabled=true;note.textContent='正在校验完整性…';
        request('/api/console/backups/'+encodeURIComponent(item.name)+'/validate',{method:'POST'}).then(function(result){note.textContent='校验通过 · 数据库版本 '+result.schema_version+' · '+result.tables+' 个表';}).catch(function(error){note.textContent=error.message;}).finally(function(){validate.disabled=false;});
      }});
      var download=h('button',{type:'button',class:'btn',text:'下载',onclick:function(){downloadBackup(item.name,download,note);}});
      var guide=h('button',{type:'button',class:'btn',text:'恢复指引',onclick:function(){showRestoreGuide(item.name);}});
      node.appendChild(h('div',{class:'maintenance-row'},h('div',{},h('strong',{text:item.name}),note),h('div',{class:'toolbar'},validate,download,guide)));
    });
    if(!older&&!data.items.length)node.appendChild(h('p',{class:'settings-note',text:'暂无管理目录中的备份。点击“创建数据库备份”生成一致性快照。'}));
    backupCursor=data.next_cursor;document.getElementById('backups-more').hidden=!backupCursor;
    document.getElementById('backup-list-note').textContent=data.active?'有备份正在执行，请稍后刷新。':'仅列出当前备份目录内由 FLLBot 命名的数据库文件。';
    if(backupJobId)pollBackupJob(backupJobId,++backupPollGeneration);
  }).catch(function(error){if(id===backupListRequest)document.getElementById('backup-list-note').textContent=error.message;});
}
function pollBackupJob(jobId,generation){
  request('/api/console/backup-jobs/'+jobId).then(function(job){
    if(generation!==backupPollGeneration)return;
    var node=document.getElementById('backup-job-note');
    if(job.status==='pending'||job.status==='running'){
      node.textContent='数据库备份正在执行；可以切换页面，作业会继续。';document.getElementById('backup-create').disabled=true;
      setTimeout(function(){if(generation===backupPollGeneration)pollBackupJob(jobId,generation);},1500);return;
    }
    node.textContent=job.status==='completed'?'备份已完成并通过校验：'+job.result.name:job.error||'备份未完成，请检查日志';
    backupJobId=null;sessionStorage.removeItem('fllbot-backup-job');loadBackups(false);
  }).catch(function(error){
    if(generation!==backupPollGeneration)return;
    document.getElementById('backup-job-note').textContent='查询作业失败：'+error.message+'。不会重复创建备份，可刷新列表核对。';
    if(error.status===404){backupJobId=null;sessionStorage.removeItem('fllbot-backup-job');}
  });
}
document.getElementById('backup-create').addEventListener('click',function(){
  var button=this;button.disabled=true;document.getElementById('backup-job-note').textContent='正在提交备份作业…';
  request('/api/console/backups',{method:'POST'}).then(function(job){backupJobId=job.id;sessionStorage.setItem('fllbot-backup-job',job.id);pollBackupJob(job.id,++backupPollGeneration);}).catch(function(error){document.getElementById('backup-job-note').textContent=error.message;loadBackups(false);});
});
document.getElementById('backups-refresh').addEventListener('click',function(){loadBackups(false);});
document.getElementById('backups-more').addEventListener('click',function(){loadBackups(true);});
function downloadBackup(name,button,note){
  button.disabled=true;note.textContent='正在准备下载…';
  fetch('/api/console/backups/'+encodeURIComponent(name)+'/download',{headers:token?{'Authorization':'Bearer '+token}:{},cache:'no-store'}).then(function(response){if(!response.ok)throw new Error('下载失败，请检查管理令牌和备份文件');return response.blob();}).then(function(blob){
    var url=URL.createObjectURL(blob),link=document.createElement('a');link.href=url;link.download=name;document.body.appendChild(link);link.click();link.remove();setTimeout(function(){URL.revokeObjectURL(url);},10000);note.textContent='已交给浏览器下载。数据库备份可能包含会话和密钥，请妥善保存。';
  }).catch(function(error){note.textContent=error.message;}).finally(function(){button.disabled=false;});
}
function showRestoreGuide(name){
  var id=++restoreGuideRequest;
  var node=document.getElementById('backup-restore-guide');node.hidden=false;node.textContent='正在读取恢复指引…';
  request('/api/console/backups/'+encodeURIComponent(name)+'/restore-guide').then(function(data){
    if(id!==restoreGuideRequest)return;
    node.textContent='';node.appendChild(h('h3',{text:'停机恢复：'+data.name}));var steps=h('ol',{});data.steps.forEach(function(step){steps.appendChild(h('li',{text:step}));});node.appendChild(steps);node.appendChild(h('pre',{class:'restore-command',text:data.command}));node.appendChild(h('p',{class:'settings-note',text:'此处仅展示 PowerShell 命令，不会执行恢复或重启机器人。'}));node.scrollIntoView({behavior:'smooth',block:'nearest'});
  }).catch(function(error){if(id===restoreGuideRequest)node.textContent=error.message;});
}

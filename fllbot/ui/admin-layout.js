// Page-level context is shared by every management surface.
var adminPageContext = {
  overview:['实例总览','自动更新运行状态'],
  bots:['实例默认','修改后点击保存'], accounts:['当前账号','修改后点击保存'],
  groups:['群聊配置','列表修改自动保存'], agent:['默认回复规则','修改后点击保存'],
  models:['共享模型资源','修改后点击保存'], personas:['共享人格资源','修改后点击保存'],
  plugins:['全实例插件','开关立即保存'], tools:['默认工具授权','开关立即保存'],
  playground:['独立测试会话','发送测试会产生模型用量'],
  tasks:['持久化插件任务','先筛选，再选择任务操作'],
  logs:['实例运行日志','按条件筛选'], traces:['消息处理记录','选择记录查看处理过程'],
  history:['已保存的会话','选择会话查看内容'], usage:['实例用量','统计受数据保留范围影响'],
  system:['实例设置','修改后点击保存'], services:['外部服务','健康检查由你发起'],
  maintenance:['主数据库','先创建备份，再校验或下载'], retention:['业务数据','清理前必须预览确认']
};
function renderAdminPageContext() {
  var view=document.body.dataset.view,info=adminPageContext[view]||['实例管理',''];
  var scope=document.getElementById('page-scope'),state=document.getElementById('page-save-state');
  if(!scope||!state)return;
  scope.textContent=info[0];
  state.textContent=dirtyPages.has(view)?'有未保存的修改':info[1];
  state.classList.toggle('has-changes',dirtyPages.has(view));
  document.querySelectorAll('.nav-item').forEach(function(item){
    var section=item.dataset.section;
    item.classList.toggle('has-draft',Array.from(dirtyPages).some(function(page){return section?sectionForView[page]===section:page===item.dataset.view;}));
  });
}

// Keep the source order and keyboard order identical to the visible action order.
['agent-model-form','system-form'].forEach(function(id){
  var form=document.getElementById(id),bar=form.querySelector('.settings-footer');
  if(bar){bar.classList.add('admin-savebar');form.insertBefore(bar,form.firstChild);}
});
var agentForm=document.getElementById('agent-settings-form');
var agentBar=agentForm.parentElement.querySelector('.settings-footer');
if(agentBar){agentBar.classList.add('admin-savebar');agentForm.parentElement.insertBefore(agentBar,agentForm);}
[['account-form','account-save','account-note'],['retention-form','retention-save','retention-note']].forEach(function(ids){
  var form=document.getElementById(ids[0]),bar=h('div',{class:'admin-savebar'});
  bar.appendChild(document.getElementById(ids[2]));bar.appendChild(document.getElementById(ids[1]));
  form.insertBefore(bar,form.firstChild);
});

// Everyday status comes first; setup remains available after operational information.
var guide=document.getElementById('setup-guide'),firstPage=document.querySelector('[data-page="bots"]');
firstPage.parentElement.insertBefore(guide,firstPage);
renderAdminPageContext();

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
  if (consoleShell) updateConsoleShell(view);
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

// Promote primary actions into the page header without duplicating controls or handlers.
var workbenchPanels={tools:'tools-card',plugins:'plugins-card',personas:'persona-page',logs:'logs-card',bots:'chat-settings-card',models:'agent-model-panel'};
var workbenchActions=document.createElement('div');
workbenchActions.className='page-actions';workbenchActions.setAttribute('aria-label','当前页面操作');
document.querySelector('.page-heading').insertBefore(workbenchActions,document.getElementById('last-sync'));
Object.keys(workbenchPanels).forEach(function(view){
  var panel=document.getElementById(workbenchPanels[view]);if(!panel)return;
  var head=panel.querySelector(':scope > .card-head');if(!head)return;
  panel.classList.add('primary-surface');
  var actions=Array.from(head.children).slice(1);
  actions.forEach(function(action){action.dataset.workbenchView=view;workbenchActions.appendChild(action);});
});
function syncWorkbenchActions(){
  var view=document.body.dataset.view;
  Array.from(workbenchActions.children).forEach(function(action){action.hidden=action.dataset.workbenchView!==view;});
  workbenchActions.hidden=!workbenchPanels[view];
  if (consoleShell) updateConsoleShell(view);
}
var navigateWorkbenchBase=navigate;
navigate=function(view){navigateWorkbenchBase(view);syncWorkbenchActions();};
syncWorkbenchActions();

// Compose workspaces from existing controls, retaining IDs, handlers and form ownership.
var chatSave=document.getElementById('chat-save');
chatSave.setAttribute('form','chat-form');chatSave.dataset.workbenchView='bots';
workbenchActions.appendChild(chatSave);
syncWorkbenchActions();
var monitor=document.createElement('div');monitor.className='monitor-workspace';monitor.dataset.page='overview';
var usagePanel=document.getElementById('overview-model-usage');
usagePanel.parentNode.insertBefore(monitor,usagePanel);
var trendPanel=document.getElementById('overview-trend').closest('.card');trendPanel.classList.add('monitor-chart');
var monitorStatus=document.querySelector('[data-page="overview"].overview-grid');monitorStatus.classList.add('monitor-status');
monitor.appendChild(trendPanel);monitor.appendChild(usagePanel);monitor.appendChild(monitorStatus);
monitor.hidden=document.body.dataset.view!=='overview';

var toolsCard=document.getElementById('tools-card'),toolWorkspace=document.createElement('div');
toolWorkspace.className='tool-workspace';
var toolCategories=document.createElement('aside');toolCategories.className='tool-categories';toolCategories.id='tool-categories';toolCategories.setAttribute('aria-label','工具插件分类');
var toolWorkspaceMain=document.createElement('div');toolWorkspaceMain.className='tool-workspace-main';
Array.from(toolsCard.children).filter(function(node){return !node.classList.contains('card-head');}).forEach(function(node){toolWorkspaceMain.appendChild(node);});
toolWorkspace.appendChild(toolCategories);toolWorkspace.appendChild(toolWorkspaceMain);toolsCard.appendChild(toolWorkspace);
function renderToolCategories(){
  var picker=document.getElementById('tool-plugin');toolCategories.textContent='';
  toolCategories.appendChild(h('h3',{text:'插件分类'}));
  Array.from(picker.options).forEach(function(option){
    toolCategories.appendChild(h('button',{type:'button',class:'tool-category'+(picker.value===option.value?' active':''),'aria-pressed':String(picker.value===option.value),text:option.value?option.textContent:'全部工具（'+toolsCatalog.length+'）',onclick:function(){picker.value=option.value;picker.dispatchEvent(new Event('change',{bubbles:true}));refreshSelectControls();}}));
  });
  toolCategories.appendChild(h('p',{class:'settings-note',text:'选择插件缩小范围，再搜索或批量调整默认授权。'}));
}

// Frequent destinations stay visible across pages. Maintenance is disclosed on demand.
var consoleAreas = [
  {name:'工作台', pages:['overview','groups','bots','models','personas','tools','plugins']},
  {name:'运行', pages:['tasks','logs','traces','usage']},
  {name:'更多设置', pages:['accounts','agent','playground','history','system','services','maintenance','retention'], compact:true}
];
var navLabels={bots:'Agent 聊天设置',groups:'群与回复',models:'模型服务',tools:'Agent 工具',usage:'模型用量'};
var consoleShell=buildConsoleShell();
updateConsoleShell(document.body.dataset.view);

function buildConsoleShell() {
  var sidebar=document.querySelector('.sidebar');
  var brand=sidebar.querySelector('.brand');
  var footer=sidebar.querySelector('.sidebar-bottom');
  footer.querySelector('.sidebar-settings').remove();
  var menu=h('nav',{class:'direct-nav','aria-label':'后台页面'});
  consoleAreas.forEach(function(area) {
    var group=h(area.compact?'details':'section',{class:'direct-group'});
    group.appendChild(h(area.compact?'summary':'h2',{text:area.name}));
    area.element=group;
    area.links=area.pages.map(function(page) {
      var link=h('button',{type:'button',class:'direct-link',text:navLabels[page]||viewInfo[page][0],onclick:function(){
        navigate(page);
        document.body.classList.remove('mobile-navigation-open');
        menuToggle.setAttribute('aria-expanded','false');
      }});
      group.appendChild(link);
      return link;
    });
    menu.appendChild(group);
  });
  var menuToggle=h('button',{class:'btn mobile-nav-toggle',type:'button','aria-expanded':'false',text:'页面导航',onclick:function(){
    var open=document.body.classList.toggle('mobile-navigation-open');
    menuToggle.setAttribute('aria-expanded',String(open));
  }});
  sidebar.replaceChildren(brand,menuToggle,menu,footer);
  sidebar.classList.add('console-shell');
  document.body.classList.add('console-redesign');
  return sidebar;
}
function updateConsoleShell(view) {
  consoleAreas.forEach(function(area) {
    if(area.compact && area.pages.includes(view) && consoleShell.dataset.current!==view) area.element.open=true;
    area.links.forEach(function(link,index) {
      var target=area.pages[index];
      link.classList.toggle('active',target===view);
      link.classList.toggle('has-draft',dirtyPages.has(target));
      if(target===view) link.setAttribute('aria-current','page');
      else link.removeAttribute('aria-current');
    });
  });
  consoleShell.dataset.current=view;
  document.getElementById('page-title').textContent=navLabels[view]||viewInfo[view][0];
}

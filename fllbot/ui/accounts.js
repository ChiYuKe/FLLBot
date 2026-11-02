var accountRequest=0,accountRevision=null,accountEditingId='',accountPreferredId='',accountSaving=false,accountSyncing=false,accountReady=false;
function showAccountDefaults(selfId){
  if(accountSaving||accountSyncing){toast('账号默认值正在保存，请稍后再切换。');return;}
  if(dirtyPages.has('accounts')&&accountEditingId!==String(selfId)){
    if(!window.confirm('前往其他账号会放弃当前未保存的默认值草稿，继续吗？'))return;
    markSaved('accounts');
  }
  accountPreferredId=String(selfId);navigate('accounts');
}
function accountEditorEnabled(enabled){
  document.querySelectorAll('#account-form input,#account-form select,#account-form button').forEach(function(control){control.disabled=!enabled;});
}
function fillAccountChoices(id,items,current,inherit){
  var select=document.getElementById(id);select.textContent='';select.appendChild(h('option',{value:'',text:inherit}));
  items.forEach(function(item){select.appendChild(h('option',{value:item.name,text:(item.title||item.name)+(item.title&&item.title!==item.name?' · '+item.name:'')}));});
  if(current&&!items.some(function(item){return item.name===current;}))select.appendChild(h('option',{value:current,text:current+'（资源已失效，请更换）'}));
  select.value=current||'';
}
function loadAccountDetail(selfId){
  var id=++accountRequest;accountReady=false;accountEditorEnabled(false);accountRevision=null;
  if(!selfId){document.getElementById('account-context').textContent='尚无已知账号；请先连接 NapCat。';return Promise.resolve();}
  document.getElementById('account-note').textContent='正在读取账号默认设置…';
  return request('/api/console/bots/'+encodeURIComponent(selfId)+'/defaults').then(function(data){
    if(id!==accountRequest||document.getElementById('account-id').value!==String(data.self_id))return;
    accountEditingId=String(data.self_id);accountRevision=data.revision;accountReady=true;
    document.getElementById('account-reply-mode').value=data.defaults.reply_mode;
    document.getElementById('account-wake-word').value=data.defaults.wake_word;
    fillAccountChoices('account-provider',data.providers.map(function(name){return {name:name};}),data.defaults.provider,'跟随实例模型路由');
    fillAccountChoices('account-persona',data.personas,data.defaults.persona,'跟随实例默认人格');
    document.getElementById('account-context').textContent=(data.nickname||'未同步昵称')+' · QQ '+data.self_id+' · '+(data.online?'在线':'离线，设置会保留')+(data.global_wake_word?' · 当前全局唤醒词优先：'+data.global_wake_word:'');
    document.getElementById('account-note').textContent=data.note;markSaved('accounts');accountEditorEnabled(true);
  }).catch(function(error){if(id===accountRequest)document.getElementById('account-note').textContent=error.message+'；草稿未提交，请重新读取后核对。';});
}
function loadAccounts(force){
  if(accountSaving||accountSyncing||dirtyPages.has('accounts')&&!force)return;
  var id=++accountRequest;accountEditorEnabled(false);
  return request('/api/console/bots').then(function(data){
    if(id!==accountRequest)return;var select=document.getElementById('account-id'),previous=accountPreferredId||select.value||accountEditingId;accountPreferredId='';select.textContent='';
    data.items.forEach(function(item){select.appendChild(h('option',{value:String(item.self_id),text:(item.nickname||'未同步昵称')+' · '+item.self_id+(item.online?' · 在线':' · 离线')}));});
    if(data.items.some(function(item){return String(item.self_id)===previous;}))select.value=previous;
    return loadAccountDetail(select.value);
  }).catch(function(error){if(id===accountRequest)document.getElementById('account-note').textContent=error.message;});
}
document.getElementById('account-id').addEventListener('change',function(){
  if(accountSaving||accountSyncing)return;
  if(dirtyPages.has('accounts')&&!window.confirm('切换账号会放弃当前未保存的默认值草稿，继续吗？')){this.value=accountEditingId;refreshSelectControls();return;}
  loadAccountDetail(this.value);
});
document.getElementById('accounts-reload').addEventListener('click',function(){
  if(accountSaving||accountSyncing)return;if(dirtyPages.has('accounts')&&!window.confirm('重新读取会放弃账号默认值草稿，请先复制需要保留的内容。继续吗？'))return;
  loadAccounts(true);
});
document.getElementById('accounts-sync').addEventListener('click',function(){
  if(accountSaving||accountSyncing)return;if(dirtyPages.has('accounts')&&!window.confirm('同步后会重新读取账号默认值并放弃草稿，继续吗？'))return;
  var button=this;button.disabled=true;accountSyncing=true;accountEditorEnabled(false);['account-id','accounts-reload'].forEach(function(id){document.getElementById(id).disabled=true;});
  request('/api/console/bots/refresh',{method:'POST'}).then(function(data){accountSyncing=false;return loadAccounts(true).then(function(){document.getElementById('account-note').textContent=data.failed?'部分在线账号未能同步昵称，已保留原资料；请检查连接。':'在线账号资料已同步。';});}).catch(function(error){document.getElementById('account-note').textContent=error.message;}).finally(function(){accountSyncing=false;button.disabled=false;accountEditorEnabled(accountReady);['account-id','accounts-reload'].forEach(function(id){document.getElementById(id).disabled=false;});});
});
document.getElementById('account-form').addEventListener('submit',function(event){
  event.preventDefault();if(accountSaving||accountSyncing||!accountReady||!accountRevision||!this.reportValidity())return;
  var selfId=accountEditingId;if(document.getElementById('account-id').value!==selfId)return;
  var values={reply_mode:document.getElementById('account-reply-mode').value,wake_word:document.getElementById('account-wake-word').value.trim(),provider:document.getElementById('account-provider').value,persona:document.getElementById('account-persona').value};
  accountSaving=true;accountEditorEnabled(false);['account-id','accounts-reload','accounts-sync'].forEach(function(id){document.getElementById(id).disabled=true;});
  request('/api/console/bots/'+encodeURIComponent(selfId)+'/defaults',{method:'PUT',headers:{'If-Match':accountRevision},body:values}).then(function(data){
    accountRevision=data.revision;document.getElementById('account-wake-word').value=data.defaults.wake_word;markSaved('accounts');document.getElementById('account-note').textContent='账号默认值已保存；群单独配置仍优先。未发送测试消息。';toast('账号默认值已保存');
  }).catch(function(error){document.getElementById('account-note').textContent=error.message;}).finally(function(){accountSaving=false;accountEditorEnabled(accountReady);['account-id','accounts-reload','accounts-sync'].forEach(function(id){document.getElementById(id).disabled=false;});});
});

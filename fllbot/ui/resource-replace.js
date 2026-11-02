function appendResourceReplacement(node,kind,name,references){
  if(!references.count)return;
  if(!references.alternatives||!references.alternatives.length){node.appendChild(h('p',{class:'settings-note',text:'先创建其他'+(kind==='persona'?'人格':'模型服务')+'，才能批量更换直接引用。'}));return;}
  var root=h('details',{class:'advanced resource-replacement'}),select=h('select',{'aria-label':'替换引用的目标资源'}),previewButton=h('button',{class:'btn',type:'button',text:'预览全部直接引用'}),previewNode=h('div'),confirm=h('input',{type:'checkbox'}),execute=h('button',{class:'btn',type:'button',text:'确认更换全部预览引用'}),note=h('p',{class:'settings-note',role:'status'}),previewId=null,sequence=0,busy=false;
  select.appendChild(h('option',{value:'',text:'请选择替换后的资源'}));
  references.alternatives.forEach(function(item){select.appendChild(h('option',{value:item.name,text:(item.title||item.name)+(item.title&&item.title!==item.name?' · '+item.name:'')}));});
  root.appendChild(h('summary',{text:'批量更换直接引用'}));
  root.appendChild(h('p',{class:'settings-note',text:'更换系统、机器人和群的全部直接引用；继承场景随上层设置生效。原资源、密钥和历史保留。'}));
  root.appendChild(h('label',{class:'setting-field'},h('span',{text:'替换引用的目标资源'}),select));root.appendChild(previewButton);root.appendChild(previewNode);
  root.appendChild(h('label',{class:'inline-check'},confirm,h('span',{text:'我已核对以上目标和全部引用范围。'})));root.appendChild(execute);root.appendChild(note);node.appendChild(root);
  confirm.disabled=true;execute.disabled=true;
  function invalidate(){++sequence;previewId=null;confirm.checked=false;confirm.disabled=true;execute.disabled=true;previewNode.textContent='';}
  select.addEventListener('change',function(){invalidate();note.textContent='请重新预览所选目标。';});
  confirm.addEventListener('change',function(){execute.disabled=!confirm.checked||!previewId||busy;});
  previewButton.addEventListener('click',function(){
    if(busy)return;invalidate();if(!select.value){note.textContent='请先选择替换后的资源。';return;}
    var id=sequence;note.textContent='正在预览…';
    request('/api/console/resources/'+kind+'/'+encodeURIComponent(name)+'/replacement-preview',{method:'POST',body:{target:select.value}}).then(function(data){
      if(id!==sequence||!root.isConnected)return;
      previewId=data.id;
      previewNode.appendChild(h('strong',{text:data.source+' → '+data.target+' · 共 '+data.count+' 处直接引用'}));
      previewNode.appendChild(h('p',{class:'settings-note',text:'预览有效至 '+new Date(data.expires_at*1000).toLocaleString('zh-CN')+'。提交前重新核对资源和配置，任意变化都会要求重新预览。'}));
      var list=h('ul',{class:'replacement-preview-list'});data.items.forEach(function(item){list.appendChild(h('li',{text:item.label}));});previewNode.appendChild(list);
      confirm.disabled=!data.can_execute;note.textContent=data.can_execute?'核对预览并勾选确认后提交。':data.truncated?'直接引用超过 1000 处，请通过引用入口逐项调整。':'没有需要更换的直接引用。';
    }).catch(function(error){if(id===sequence&&root.isConnected)note.textContent=error.message;});
  });
  execute.addEventListener('click',function(){
    if(!previewId||!confirm.checked||busy)return;
    busy=true;select.disabled=true;previewButton.disabled=true;confirm.disabled=true;execute.disabled=true;
    var id=previewId;previewId=null;
    request('/api/console/resources/replacements',{method:'POST',body:{preview_id:id,confirm:true}}).then(function(data){
      toast('已将 '+data.count+' 处直接引用更换为 '+data.target+'；请重新读取受影响配置。');
      if(root.isConnected)return loadResourceReferences(node,kind,name);
    }).catch(function(error){if(root.isConnected){invalidate();note.textContent=error.message+'。请重新预览，勿重复提交。';}}).finally(function(){busy=false;select.disabled=false;previewButton.disabled=false;});
  });
}

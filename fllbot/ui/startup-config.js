function renderStartupConfig(data){
  var node=document.getElementById('startup-config-info');node.textContent='';if(!data)return;
  var labels={'server.host':'监听地址','server.port':'监听端口','server.cors_origins':'允许的网页来源','admin.enabled':'管理入口','admin.token_file':'管理凭据文件','plugin.dirs':'插件目录','plugin.builtin':'内置插件','plugin.hot_reload':'自动重载插件','plugin.watch_interval':'插件检查间隔','plugin.max_concurrency':'消息处理并行数','plugin.max_pending_events':'消息排队上限','storage.path':'数据库位置','logging.dir':'日志目录','logging.file':'日志文件','logging.json_lines':'结构化日志','logging.ring_size':'内存日志条数'};
  node.appendChild(h('p',{class:'settings-note',text:data.note}));
  function display(value){return typeof value==='boolean'?(value?'开启':'关闭'):Array.isArray(value)?value.join('、'):String(value);}
  var details=h('details',{class:'advanced'},h('summary',{text:'查看启动配置值'}));
  Object.keys(data.active||{}).sort().forEach(function(field){details.appendChild(h('p',{class:'settings-note',text:(labels[field]||field)+'：'+display(data.active[field])}));});node.appendChild(details);
  var inactive=data.inactive_overrides||{},fields=Object.keys(inactive);
  if(fields.length){
    var old=h('details',{class:'advanced'},h('summary',{text:'已归档 '+fields.length+' 项旧版未生效的启动覆盖'}));
    old.appendChild(h('p',{class:'settings-note',text:'以下仅为历史记录，不参与当前配置，也不会在下次重启自动应用。需要采用时，请修改启动文件或启动命令。'}));
    fields.sort().forEach(function(field){old.appendChild(h('p',{class:'settings-note',text:(labels[field]||field)+'：'+display(inactive[field])}));});node.appendChild(old);
  }
}

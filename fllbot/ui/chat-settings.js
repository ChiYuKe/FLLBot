// Agent chat settings are global; account management stays in the desktop launcher.
var chatSettingsLoaded = false;
var chatSettingsLoading = false;
var chatFlagKeys = ['send_nickname', 'send_user_id', 'send_role', 'send_time', 'allow_images', 'read_forward', 'quote_reply', 'at_sender'];
var chatNumberFields = {'forward_threshold': 'chat-forward-threshold', 'reply_chunk_size': 'chat-reply-chunk-size', 'reply_interval_ms': 'chat-reply-interval-ms'};
function chatDraft() {
  var values = {};
  chatFlagKeys.forEach(function (key) { values[key] = document.getElementById('chat-' + key).checked; });
  values.wake_word = document.getElementById('chat-wake-word').value.trim();
  values.wake_only_mode = document.getElementById('chat-wake-only-mode').value;
  values.wake_only_prompt = document.getElementById('chat-wake-only-prompt').value;
  values.wake_only_replies = document.getElementById('chat-wake-only-replies').value.split('\n').map(function (line) { return line.trim(); }).filter(Boolean);
  values.reply_mode = document.getElementById('chat-reply-mode').value;
  Object.keys(chatNumberFields).forEach(function (key) { values[key] = Number(document.getElementById(chatNumberFields[key]).value); });
  return values;
}
function renderChatPreview() {
  var values = chatDraft();
  var mode = values.wake_only_mode;
  document.getElementById('chat-prompt-field').hidden = mode !== 'prompt';
  document.getElementById('chat-fixed-field').hidden = mode !== 'fixed';
  document.getElementById('chat-threshold-field').hidden = values.reply_mode !== 'auto';
  if (mode === 'fixed') {
    document.getElementById('chat-wake-preview').textContent = values.wake_only_replies.length
      ? '不请求模型，随机回复其中一条，例如：' + values.wake_only_replies[0]
      : '请至少填写一条固定回复。';
    return;
  }
  var content = mode === 'raw' ? values.wake_word || 'bot' : values.wake_only_prompt;
  var name = values.send_nickname ? '示例用户' : values.send_user_id ? 'QQ 123456' : '';
  var tags = [];
  if (values.send_user_id) tags.push('123456');
  if (values.send_role) tags.push('成员');
  if (name) content = name + (tags.length ? '(' + tags.join('，') + ')' : '') + '：' + content;
  else if (values.send_role) content = '成员：' + content;
  if (values.send_time) content = '[2026-01-01 12:00:00] ' + content;
  document.getElementById('chat-wake-preview').textContent = mode === 'ignore' ? '不向模型发送消息，也不生成回复。' : content || '请填写仅唤醒时发送给模型的提示。';
}
function loadChatSettings() {
  if (chatSettingsLoading || dirtyPages.has('bots')) return;
  chatSettingsLoading = true;
  chatSettingsLoaded = false;
  document.querySelectorAll('#chat-form input,#chat-form select,#chat-form textarea').forEach(function (control) { control.disabled = true; });
  document.getElementById('chat-save').disabled = true;
  document.getElementById('chat-note').textContent = '正在读取聊天参数…';
  return request('/api/console/chat').then(function (values) {
    chatFlagKeys.forEach(function (key) { document.getElementById('chat-' + key).checked = Boolean(values[key]); });
    document.getElementById('chat-wake-word').value = values.wake_word || '';
    document.getElementById('chat-wake-only-mode').value = values.wake_only_mode;
    document.getElementById('chat-wake-only-prompt').value = values.wake_only_prompt;
    document.getElementById('chat-wake-only-replies').value = (values.wake_only_replies || []).join('\n');
    document.getElementById('chat-reply-mode').value = values.reply_mode || 'auto';
    Object.keys(chatNumberFields).forEach(function (key) { document.getElementById(chatNumberFields[key]).value = values[key] == null ? ({forward_threshold:1200, reply_chunk_size:0, reply_interval_ms:350})[key] : values[key]; });
    chatSettingsLoaded = true;
    document.getElementById('chat-note').textContent = '';
    renderChatPreview(); refreshSelectControls();
  }).catch(function (error) {
    document.getElementById('chat-note').textContent = error.message || '读取失败，请重新读取。';
  }).finally(function () {
    chatSettingsLoading = false;
    document.querySelectorAll('#chat-form input,#chat-form select,#chat-form textarea').forEach(function (control) { control.disabled = !chatSettingsLoaded; });
    document.getElementById('chat-save').disabled = !chatSettingsLoaded;
  });
}
document.getElementById('chat-form').addEventListener('input', renderChatPreview);
document.getElementById('chat-form').addEventListener('change', renderChatPreview);
document.getElementById('chat-reload').addEventListener('click', function () {
  if (dirtyPages.has('bots')) { toast('请先保存当前修改。'); return; }
  loadChatSettings();
});
document.getElementById('chat-form').addEventListener('submit', function (event) {
  event.preventDefault();
  if (!chatSettingsLoaded || chatSettingsLoading) return;
  var values = chatDraft();
  if (values.wake_only_mode === 'prompt' && !values.wake_only_prompt.trim()) {
    document.getElementById('chat-note').textContent = '请填写只发唤醒词时发送给模型的内容。'; return;
  }
  if (values.wake_only_mode === 'fixed' && !values.wake_only_replies.length) {
    document.getElementById('chat-note').textContent = '请至少填写一条只发唤醒词时的固定回复。'; return;
  }
  document.getElementById('chat-save').disabled = true;
  chatSettingsLoading = true;
  request('/api/console/chat', {method: 'PUT', body: values}).then(function () {
    markSaved('bots'); document.getElementById('chat-note').textContent = '已保存，后续聊天立即生效。'; toast('Agent 聊天参数已保存');
  }).catch(function (error) { document.getElementById('chat-note').textContent = error.message; })
    .finally(function () { chatSettingsLoading = false; document.getElementById('chat-save').disabled = false; });
});
document.querySelectorAll('#chat-form input,#chat-form select,#chat-form textarea').forEach(function (control) { control.disabled = true; });

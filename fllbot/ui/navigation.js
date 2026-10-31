// Resource pages, configuration and operational data are separate concerns.
// Existing hashes stay valid after folding the old entries into page tabs.
var sectionForView = {
  groups: 'config', agent: 'config', models: 'models', playground: 'models',
  history: 'data', usage: 'data', logs: 'data'
};
var sectionInfo = {
  config: { title: '配置', tabs: ['groups', 'agent'] },
  models: { title: '模型服务', tabs: ['models', 'playground'] },
  data: { title: '数据与日志', tabs: ['logs', 'history', 'usage'] }
};
var lastSectionViews = { config: 'groups', models: 'models', data: 'logs' };

function renderSectionTabs(view) {
  var node = document.getElementById('section-tabs');
  var section = sectionInfo[sectionForView[view]];
  var restoreFocus = node.contains(document.activeElement);
  node.textContent = '';
  node.hidden = !section;
  if (!section) return;
  node.setAttribute('aria-label', section.title + '栏目');
  section.tabs.forEach(function (target, index) {
    var panels = Array.from(document.querySelectorAll('[data-page="' + target + '"]'));
    panels.forEach(function (panel, position) { if (!panel.id) panel.id = 'page-' + target + '-' + position; });
    var button = h('button', {
      id: 'section-tab-' + target, type: 'button', role: 'tab',
      class: 'section-tab' + (target === view ? ' active' : ''),
      'aria-selected': String(target === view),
      'aria-controls': panels.map(function (panel) { return panel.id; }).join(' '),
      tabindex: target === view ? '0' : '-1', text: viewInfo[target][0],
      onclick: function () { navigate(target); }
    });
    button.addEventListener('keydown', function (event) {
      var next;
      if (event.key === 'ArrowRight') next = (index + 1) % section.tabs.length;
      else if (event.key === 'ArrowLeft') next = (index + section.tabs.length - 1) % section.tabs.length;
      else if (event.key === 'Home') next = 0;
      else if (event.key === 'End') next = section.tabs.length - 1;
      else return;
      event.preventDefault();
      navigate(section.tabs[next]);
      document.getElementById('section-tab-' + section.tabs[next]).focus();
    });
    node.appendChild(button);
  });
  if (restoreFocus) document.getElementById('section-tab-' + view).focus();
}

function navigate(view) {
  if (!viewInfo[view]) view = 'overview';
  if (activeSelectControl) activeSelectControl.close(false);
  document.body.dataset.view = view;
  document.querySelectorAll('[data-page]').forEach(function (panel) { panel.hidden = panel.dataset.page !== view; });
  document.querySelectorAll('.nav-item').forEach(function (item) {
    var active = item.dataset.section ? item.dataset.section === sectionForView[view] : item.dataset.view === view;
    item.classList.toggle('active', active);
    if (active) item.setAttribute('aria-current', 'page'); else item.removeAttribute('aria-current');
  });
  var section = sectionInfo[sectionForView[view]];
  if (section) lastSectionViews[sectionForView[view]] = view;
  document.getElementById('page-title').textContent = section ? section.title : viewInfo[view][0];
  renderSectionTabs(view);
  updateDirtyState();
  if (location.hash !== '#' + view) history.replaceState(null, '', '#' + view);
  if (!token || needsAuth) return;
  if ((view === 'models' || view === 'personas') && !agentModelLoaded) loadAgentModel();
  if (view === 'bots' && !dirtyPages.has('bots')) loadChatSettings();
  if (view === 'models') loadProviders();
  if (view === 'tools') loadTools();
  if (view === 'agent' && !loadedPages.has('agent')) loadAgentSettings();
  if ((view === 'groups' || view === 'agent') && !groupDirectoryLoaded) loadGroupDirectory();
  if (view === 'groups' && groupDirectoryLoaded) loadGroupTable();
  if (view === 'playground') loadTestOptions();
  if (view === 'history') loadHistory();
  if (view === 'usage') loadUsage();
  if (view === 'system' && !systemInitial) loadSystem();
}

document.querySelectorAll('.nav-item,[data-go]').forEach(function (button) {
  button.addEventListener('click', function () {
    navigate(button.dataset.section ? lastSectionViews[button.dataset.section] : button.dataset.go || button.dataset.view);
  });
});
window.addEventListener('hashchange', function () { navigate(location.hash.slice(1)); });

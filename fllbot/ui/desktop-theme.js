// Receive the actual desktop theme without reloading the frame or losing drafts.
(function () {
  if (window.parent === window) return;
  var colors = ['canvas','panel','sidebar','field','selected','ink','muted','subtle','line','line-strong','accent','accent-dark','green','red','yellow'];
  window.addEventListener('message', function (event) {
    if (event.source !== window.parent || !event.data || event.data.type !== 'fllbot:desktop-theme') return;
    var data = event.data;
    if (data.theme !== 'light' && data.theme !== 'dark') return;
    var root = document.documentElement;
    root.dataset.theme = data.theme;
    root.dataset.embedded = 'true';
    root.dataset.density = data.density === 'compact' ? 'compact' : 'comfortable';
    root.dataset.reducedMotion = String(data.reducedMotion === true);
    var variables = data.variables || {};
    colors.forEach(function (key) {
      var name = '--desktop-' + key; var value = variables[name];
      if (typeof value === 'string' && value.length < 200 && CSS.supports('color', value)) root.style.setProperty(name, value);
      else root.style.removeProperty(name);
    });
    var font = variables['--app-font-family'];
    ['--scrollbar-thumb','--scrollbar-thumb-hover','--scrollbar-track'].forEach(function(name){
      var value=variables[name];
      if(typeof value==='string'&&value.length<200&&CSS.supports('color',value))root.style.setProperty(name,value);
      else root.style.removeProperty(name);
    });
    var scrollbarWidth=variables['--scrollbar-width'];
    if(typeof scrollbarWidth==='string'&&/^\d+(\.\d+)?px$/.test(scrollbarWidth)&&parseFloat(scrollbarWidth)>=4&&parseFloat(scrollbarWidth)<=20)root.style.setProperty('--scrollbar-width',scrollbarWidth);
    else root.style.removeProperty('--scrollbar-width');
    if (typeof font === 'string' && font.length < 500 && CSS.supports('font-family', font) && !/[;{}]|url\s*\(/i.test(font)) root.style.setProperty('--app-font-family', font);
    var shadow = variables['--desktop-shadow'];
    if (typeof shadow === 'string' && shadow.length < 200 && CSS.supports('box-shadow', shadow) && !/url\s*\(/i.test(shadow)) root.style.setProperty('--desktop-shadow', shadow);
  });
  // Appearance-only handshake. No token or other user data leaves this frame.
  window.parent.postMessage({ type:'fllbot:theme-ready' }, '*');
})();

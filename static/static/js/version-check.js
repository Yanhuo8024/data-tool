/* version-check.js — 前端资源版本自动检测
 * 后端按 static 下 js/css 最新 mtime 生成 asset_ver（见 /api/version）。
 * 页面加载后与注入值 window.ASSET_VER 比对，不一致即提示刷新，摆脱手动 Ctrl+F5。 */
(function () {
  'use strict';
  function showUpdateBar() {
    if (document.getElementById('wb-ver-bar')) return;
    var bar = document.createElement('div');
    bar.id = 'wb-ver-bar';
    bar.style.cssText = 'position:fixed;top:14px;right:18px;z-index:99999;background:#FF6B6B;color:#fff;' +
      'padding:10px 16px;border-radius:10px;font-size:13px;box-shadow:0 6px 20px rgba(0,0,0,.25);' +
      'display:flex;align-items:center;gap:12px;font-family:inherit;';
    bar.innerHTML = '<span>✨ 检测到新版本，已更新资源</span>' +
      '<button style="background:#fff;color:#FF6B6B;border:none;border-radius:6px;padding:5px 14px;' +
      'font-size:12px;font-weight:600;cursor:pointer;">刷新</button>';
    bar.querySelector('button').onclick = function () { location.reload(true); };
    document.body.appendChild(bar);
  }
  setTimeout(function () {
    try {
      fetch('/api/version', { cache: 'no-store' })
        .then(function (r) { return r.json(); })
        .then(function (d) {
          var cur = window.ASSET_VER || '';
          if (d && d.version && cur && String(d.version) !== String(cur)) {
            showUpdateBar();
          }
        })
        .catch(function () {});
    } catch (e) { /* 静默失败，不影响主流程 */ }
  }, 4000);
})();

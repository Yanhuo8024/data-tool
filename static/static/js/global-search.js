/**
 * global-search.js — 全局搜索命令面板（Ctrl+K / Cmd+K）
 * - 数据源：GET /api/search/index（标签页 + 200+ API，一次性拉取本地过滤，零延迟）
 * - 键盘：↑↓ 选择、Enter 跳转、Esc 关闭
 * - 跳转：标签页 → switchToTab；API → 新窗口打开 /docs
 */
(function () {
  'use strict';

  var _index = null;          // {tabs:[], apis:[]}
  var _results = [];          // 当前过滤结果 [{kind:'tab'|'api', data, score}]
  var _active = -1;           // 当前高亮下标
  var _panel = null, _overlay = null, _input = null, _list = null;
  var _opened = false;

  /* ---------- UI 构建 ---------- */
  function ensureUI() {
    if (_panel) return;
    _overlay = document.createElement('div');
    _overlay.id = 'gs-overlay';
    _overlay.style.cssText = [
      'position:fixed;inset:0;background:rgba(10,12,16,.55);backdrop-filter:blur(3px);',
      'z-index:100000;display:none;align-items:flex-start;justify-content:center;',
      'padding-top:12vh;'
    ].join('');
    _panel = document.createElement('div');
    _panel.id = 'gs-panel';
    _panel.style.cssText = [
      'width:min(640px,92vw);max-height:60vh;display:flex;flex-direction:column;',
      'background:var(--bg-card,#fff);border:1px solid var(--glass-border,rgba(0,0,0,.12));',
      'border-radius:14px;box-shadow:0 20px 60px rgba(0,0,0,.35);overflow:hidden;'
    ].join('');
    var box = document.createElement('div');
    box.style.cssText = 'display:flex;align-items:center;gap:10px;padding:14px 16px;border-bottom:1px solid var(--glass-border,rgba(0,0,0,.08));';
    var icon = document.createElement('span');
    icon.textContent = '🔍';
    icon.style.cssText = 'font-size:16px;opacity:.7;';
    _input = document.createElement('input');
    _input.id = 'gs-input';
    _input.placeholder = '搜索功能、标签页、API…（↑↓ 选择，Enter 打开，Esc 关闭）';
    _input.setAttribute('autocomplete', 'off');
    _input.spellcheck = false;
    _input.style.cssText = [
      'flex:1;background:transparent;border:none;outline:none;font-size:15px;',
      'color:var(--ink,#222);'
    ].join('');
    _list = document.createElement('div');
    _list.style.cssText = 'overflow-y:auto;padding:8px;';
    box.appendChild(icon);
    box.appendChild(_input);
    _panel.appendChild(box);
    _panel.appendChild(_list);
    _overlay.appendChild(_panel);
    document.body.appendChild(_overlay);

    _overlay.addEventListener('mousedown', function (e) {
      if (e.target === _overlay) close();
    });
    _input.addEventListener('input', function () { filter(); });
    _input.addEventListener('keydown', function (e) {
      if (e.key === 'ArrowDown') { e.preventDefault(); move(1); }
      else if (e.key === 'ArrowUp') { e.preventDefault(); move(-1); }
      else if (e.key === 'Enter') { e.preventDefault(); select(); }
      else if (e.key === 'Escape') { e.preventDefault(); close(); }
      e.stopPropagation();
    });
  }

  /* ---------- 数据 ---------- */
  function loadIndex(cb) {
    if (_index) { cb(); return; }
    fetch('/api/search/index')
      .then(function (r) { return r.json(); })
      .then(function (j) {
        if (j && j.ok) { _index = { tabs: j.tabs || [], apis: j.apis || [] }; }
        else { _index = { tabs: [], apis: [] }; }
        cb();
      })
      .catch(function () { _index = { tabs: [], apis: [] }; cb(); });
  }

  /* ---------- 过滤 ---------- */
  function scoreOf(text, q) {
    text = text.toLowerCase();
    q = q.toLowerCase();
    if (text === q) return 100;
    if (text.indexOf(q) === 0) return 80;
    if (text.indexOf(q) > 0) return 60;
    return -1;
  }

  function filter() {
    var q = _input.value.trim();
    if (!q) { _results = []; render(); return; }
    var tabs = [], apis = [];
    (_index.tabs || []).forEach(function (t) {
      var hay = [t.name, t.keywords, t.category].join(' ');
      var s = scoreOf(hay, q);
      if (s >= 0) tabs.push({ kind: 'tab', data: t, score: s });
    });
    (_index.apis || []).forEach(function (a) {
      var hay = [a.path, a.summary, a.tag].join(' ');
      var s = scoreOf(hay, q);
      if (s >= 0) apis.push({ kind: 'api', data: a, score: s });
    });
    tabs.sort(function (x, y) { return y.score - x.score; });
    apis.sort(function (x, y) { return y.score - x.score; });
    _results = tabs.slice(0, 12).concat(apis.slice(0, 12));
    _active = _results.length ? 0 : -1;
    render();
  }

  /* ---------- 渲染 ---------- */
  function render() {
    _list.innerHTML = '';
    if (!_input.value.trim()) {
      _list.innerHTML = '<div style="padding:26px 16px;text-align:center;color:var(--ink-subtle,#999);font-size:13px;">输入关键词搜索 36 个标签页与 200+ API<br>例如：评分卡、DID、数据库、kline、模板…</div>';
      return;
    }
    if (!_results.length) {
      _list.innerHTML = '<div style="padding:26px 16px;text-align:center;color:var(--ink-subtle,#999);font-size:13px;">未找到匹配项</div>';
      return;
    }
    var tabCount = 0, apiCount = 0;
    _results.forEach(function (r, i) {
      if (r.kind === 'tab') tabCount++;
      else apiCount++;
    });
    var head = document.createElement('div');
    head.style.cssText = 'padding:4px 10px 6px;font-size:11px;color:var(--ink-subtle,#999);letter-spacing:.5px;';
    head.textContent = '标签页 ' + tabCount + ' ｜ API ' + apiCount;
    _list.appendChild(head);

    _results.forEach(function (r, i) {
      var row = document.createElement('div');
      var active = i === _active;
      row.style.cssText = [
        'display:flex;align-items:center;gap:10px;padding:9px 10px;border-radius:8px;cursor:pointer;',
        active ? 'background:var(--accent,rgba(94,106,210,.14));' : 'background:transparent;',
        'font-size:13px;color:var(--ink,#222);'
      ].join('');
      row.dataset.idx = i;
      if (r.kind === 'tab') {
        var badge = document.createElement('span');
        badge.textContent = '页';
        badge.style.cssText = 'flex:none;font-size:10px;padding:2px 6px;border-radius:4px;background:rgba(94,106,210,.16);color:#5e6ad2;';
        var txt = document.createElement('span');
        txt.style.cssText = 'flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;';
        txt.innerHTML = '<b>' + esc(r.data.name) + '</b> <span style="color:var(--ink-subtle,#999);font-size:12px;">· ' + esc(r.data.category) + '</span>';
        row.appendChild(badge);
        row.appendChild(txt);
        row.appendChild(arrow(i));
      } else {
        var m = document.createElement('span');
        m.textContent = r.data.method;
        m.style.cssText = 'flex:none;font-size:10px;padding:2px 6px;border-radius:4px;background:rgba(46,164,79,.15);color:#2ea44f;font-weight:600;letter-spacing:.5px;';
        var t2 = document.createElement('span');
        t2.style.cssText = 'flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;';
        t2.innerHTML = '<code style="font-family:monospace;font-size:12px;">' + esc(r.data.path) + '</code> <span style="color:var(--ink-subtle,#999);font-size:12px;">· ' + esc(r.data.summary || r.data.tag) + '</span>';
        row.appendChild(m);
        row.appendChild(t2);
        row.appendChild(arrow(i));
      }
      row.addEventListener('mouseenter', function () { _active = parseInt(this.dataset.idx, 10); render(); });
      row.addEventListener('mousedown', function (e) { e.preventDefault(); _active = parseInt(this.dataset.idx, 10); select(); });
      _list.appendChild(row);
    });
  }

  function arrow(i) {
    var s = document.createElement('span');
    s.textContent = '↵';
    s.style.cssText = 'flex:none;font-size:11px;color:var(--ink-subtle,#bbb);' + (i === _active ? '' : 'visibility:hidden;');
    return s;
  }

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  /* ---------- 交互 ---------- */
  function move(d) {
    if (!_results.length) return;
    _active = (_active + d + _results.length) % _results.length;
    render();
    var row = _list.querySelector('[data-idx="' + _active + '"]');
    if (row && row.scrollIntoView) row.scrollIntoView({ block: 'nearest' });
  }

  function select() {
    var r = _results[_active];
    if (!r) return;
    close();
    if (r.kind === 'tab') {
      if (typeof switchToTab === 'function') switchToTab(r.data.id);
      else {
        var btn = document.querySelector('.nbtn[data-t="' + r.data.id + '"]');
        if (btn) btn.click();
      }
    } else {
      window.open('/docs', '_blank');
    }
  }

  function open() {
    ensureUI();
    _opened = true;
    _overlay.style.display = 'flex';
    _panel.style.display = 'flex';
    _input.value = '';
    _results = [];
    _active = -1;
    render();
    loadIndex(function () { filter(); });
    setTimeout(function () { _input.focus(); }, 30);
  }

  function close() {
    _opened = false;
    if (_overlay) _overlay.style.display = 'none';
  }

  /* ---------- 全局快捷键 ---------- */
  document.addEventListener('keydown', function (e) {
    var k = (e.key || '').toLowerCase();
    var mod = e.metaKey || e.ctrlKey;
    if (mod && k === 'k') {
      e.preventDefault();
      e.stopPropagation();
      if (_opened) close(); else open();
    }
  }, true);

  // 暴露调试接口
  window.openGlobalSearch = open;
  window.closeGlobalSearch = close;
  console.log('[GlobalSearch] Command Palette ready (Ctrl+K / Cmd+K)');
})();

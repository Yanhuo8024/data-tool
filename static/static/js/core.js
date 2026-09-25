// =====================================================================
// core.js — 全局基础设施（工具/图表收集/流程推荐/导航）
// =====================================================================

console.log('[core.js] v2026080804 loaded - AI analysis enabled');

// ===== utils.js =====

function showLoading(text) {
  const el = document.getElementById('global-loading');
  if (!el) return;
  const txt = el.querySelector('.loading-text');
  if (txt) txt.textContent = text || '加载中...';
  el.classList.add('show');
}

function hideLoading() {
  const el = document.getElementById('global-loading');
  if (el) el.classList.remove('show');
}

function showToast(message, type) {
  const container = document.getElementById('toast-container');
  if (!container) return;
  const toast = document.createElement('div');
  toast.className = 'toast ' + (type || 'info');
  const icons = { success: '✅', error: '❌', warning: '⚠️', info: 'ℹ️' };
  toast.innerHTML = '<span>' + (icons[type] || icons.info) + '</span><span>' + message + '</span>';
  container.appendChild(toast);
  setTimeout(function() {
    toast.style.animation = 'toastOut 0.3s ease forwards';
    setTimeout(function() { toast.remove(); }, 300);
  }, 3000);
}

function initTheme() {
  const saved = localStorage.getItem('theme');
  const btn = document.getElementById('theme-toggle');
  if (saved === 'light') {
    document.body.classList.add('light-theme');
    if (btn) btn.textContent = '☀️';
  } else {
    if (btn) btn.textContent = '🌙';
  }
}

function toggleTheme() {
  const body = document.body;
  const btn = document.getElementById('theme-toggle');
  if (body.classList.contains('light-theme')) {
    body.classList.remove('light-theme');
    localStorage.setItem('theme', 'dark');
    if (btn) btn.textContent = '🌙';
    showToast('已切换到深色主题', 'info');
  } else {
    body.classList.add('light-theme');
    localStorage.setItem('theme', 'light');
    if (btn) btn.textContent = '☀️';
    showToast('已切换到浅色主题', 'info');
  }
}

function saveCurrentTab(tabId) {
  localStorage.setItem('lastTab', tabId);
}

function restoreLastTab() {
  const lastTab = localStorage.getItem('lastTab');
  if (lastTab && lastTab !== 'tab-help') {
    const btn = document.querySelector('.nbtn[data-t="' + lastTab + '"]');
    if (btn) {
      btn.click();
      return true;
    }
  }
  return false;
}

function initKeyboardShortcuts() {
  console.log('[Shortcut] Keyboard shortcuts initialized');

  // ── 手动追踪修饰键状态（解决浏览器在某些按键上丢失 modifier 状态的问题）──
  const _mods = { ctrl: false, shift: false, alt: false };

  document.addEventListener('keydown', function(e) {
    // 更新修饰键状态
    if (e.code === 'ControlLeft' || e.code === 'ControlRight') _mods.ctrl = true;
    if (e.code === 'ShiftLeft' || e.code === 'ShiftRight') _mods.shift = true;
    if (e.code === 'AltLeft' || e.code === 'AltRight') _mods.alt = true;

    // ── 如果焦点在输入框中，跳过 ──
    const tag = (e.target || {}).tagName || '';
    const isInput = tag === 'INPUT' || tag === 'TEXTAREA' || e.target.isContentEditable;
    if (isInput) return;

    // ── 用 _mods 判断（不用 e.ctrlKey/e.shiftKey）──

    // Ctrl+Shift+L → 切换主题
    if (_mods.ctrl && _mods.shift && (e.key === 'l' || e.key === 'L')) {
      e.preventDefault();
      toggleTheme();
      return;
    }

    // Ctrl+Enter → 执行操作
    if (_mods.ctrl && !_mods.shift && !_mods.alt && e.key === 'Enter') {
      e.preventDefault();
      const activeBtn = document.querySelector('.nbtn.on');
      if (activeBtn) {
        triggerMainAction(activeBtn.dataset.t);
      } else {
        showToast('请先选择一个标签页', 'info');
      }
      return;
    }

    // Ctrl+Shift+数字 → 切换标签页
    // 支持主键盘(Digit1-9)、小键盘(Numpad1-9)、以及被映射的符号键(!@#$等)
    if (_mods.ctrl && _mods.shift && !_mods.alt) {
      const codeMap = {
        // 主键盘
        'Digit1':0, 'Digit2':1, 'Digit3':2, 'Digit4':3, 'Digit5':4,
        'Digit6':5, 'Digit7':6, 'Digit8':7, 'Digit9':8,
        // 小键盘（原始码）
        'Numpad1':0, 'Numpad2':1, 'Numpad3':2, 'Numpad4':3, 'Numpad5':4,
        'Numpad6':5, 'Numpad7':6, 'Numpad8':7, 'Numpad9':8,
        // 小键盘被浏览器映射后的键名（Ctrl+Shift+NumpadX 的实际 key 值）
        'End':0, 'DownArrow':2, 'PageDown':3, 'LeftArrow':4,
        'Clear':5, 'RightArrow':6, 'Home':7, 'UpArrow':8,
        // 主键盘 Shift+数字产生的符号（备用）
        '!':0, '@':1, '#':2, '$':3, '%':4, '^':5, '&':6, '*':7, '(':8
      };
      const idx = codeMap[e.code] !== undefined ? codeMap[e.code] : codeMap[e.key];
      if (idx !== undefined) {
        e.preventDefault();
        e.stopPropagation();
        const btns = document.querySelectorAll('.nbtn');
        console.log('[Shortcut] MATCH! code=' + e.code + ' key=' + JSON.stringify(e.key) + ' → tab[' + idx + ']');
        if (btns[idx]) {
          btns[idx].click();
          showToast('→ ' + (btns[idx].textContent || '').trim(), 'success');
        } else {
          showToast('Tab ' + (idx+1) + ' not found', 'warning');
        }
        return;
      }
    }

    // ? 或 / → 帮助
    if (!_mods.ctrl && !_mods.alt && !_mods.shift && (e.key === '?' || e.key === '/')) {
      e.preventDefault();
      showShortcutHelp();
      return;
    }

    // Esc → 关闭加载
    if (e.key === 'Escape') { hideLoading(); return; }
  }, { capture: true });

  // 释放修饰键时重置状态
  document.addEventListener('keyup', function(e) {
    if (e.code === 'ControlLeft' || e.code === 'ControlRight') _mods.ctrl = false;
    if (e.code === 'ShiftLeft' || e.code === 'ShiftRight') _mods.shift = false;
    if (e.code === 'AltLeft' || e.code === 'AltRight') _mods.alt = false;
  });

  // 窗口失焦时重置（防止状态卡住）
  window.addEventListener('blur', function() { _mods.ctrl = false; _mods.shift = false; _mods.alt = false; });

  console.log('[Shortcut] Ready (modifier tracking mode)');
}

function triggerMainAction(tabId) {
  const tabActions = {
    'tab-analyze': function() { doDescribe(); },
    'tab-ml': function() { const btn = document.querySelector('#tab-ml .btn-p'); if (btn) btn.click(); },
    'tab-nlq': function() { runNLQ(); },
    'tab-report': function() { generateFullReport(); },
    'tab-finance-viz': function() { runFinViz(); },
    'tab-clean': function() { const btn = document.querySelector('#tab-clean .btn-p'); if (btn) btn.click(); },
    'tab-ts': function() { const btn = document.querySelector('#tab-ts .btn-p'); if (btn) btn.click(); },
    'tab-ht': function() { const btn = document.querySelector('#tab-ht .btn-p'); if (btn) btn.click(); },
    'tab-fs': function() { const btn = document.querySelector('#tab-fs .btn-p'); if (btn) btn.click(); },
    'tab-finance-model': function() { const btn = document.querySelector('#tab-finance-model .btn-p'); if (btn) btn.click(); }
  };
  if (tabActions[tabId]) tabActions[tabId]();
}

window.addEventListener('error', function(e) {
  console.error('[Global Error]', e.message, e.filename + ':' + e.lineno);
});

window.addEventListener('unhandledrejection', function(e) {
  console.error('[Unhandled Promise]', e.reason);
  showToast('操作异常: ' + (e.reason?.message || e.reason), 'error');
});

const _debounceTimers = {};

// ===== chart-collector.js =====

let _reportCharts = [];
let _reportChartId = 0;
let _reportConclusions = [];
let _reportConcId = 0;

// 会话持久化：图表/结论存 localStorage，刷新页面不丢（本次会话语义）
function _loadReportState() {
  try {
    _reportCharts = JSON.parse(localStorage.getItem('sessionReportCharts') || '[]');
    _reportChartId = _reportCharts.length;
  } catch(e) { _reportCharts = []; }
  try {
    _reportConclusions = JSON.parse(localStorage.getItem('sessionReportConclusions') || '[]');
    _reportConcId = _reportConclusions.length;
  } catch(e) { _reportConclusions = []; }
}
function _persistReportCharts() {
  try { localStorage.setItem('sessionReportCharts', JSON.stringify(_reportCharts.slice(-60))); } catch(e) {}
}
function _persistReportConclusions() {
  try { localStorage.setItem('sessionReportConclusions', JSON.stringify(_reportConclusions.slice(-80))); } catch(e) {}
}
_loadReportState();

function saveChartToReport(imgName, title, stats, source, htmlContent) {
  if (!imgName && !htmlContent) return;
  _reportCharts.push({
    id: ++_reportChartId,
    img: imgName || '',
    title: title || ('图表 ' + _reportChartId),
    stats: stats || '',
    source: source || '',
    html: htmlContent || '',
    time: new Date().toLocaleTimeString()
  });
  _persistReportCharts();
  updateReportPreview();
}

// 结论收集：分析/AI 产生的结论自动同步到本次会话报告
function saveConclusionToReport(title, content, source) {
  if (!content) return;
  if (typeof content !== 'string') content = String(content);
  content = content.replace(/#{1,6}\s*/g, '').replace(/[*_`>]/g, '').trim();
  if (!content) return;
  _reportConclusions.push({
    id: ++_reportConcId,
    title: title || '结论',
    content: content.slice(0, 600),
    source: source || '',
    time: new Date().toLocaleTimeString()
  });
  _persistReportConclusions();
  updateReportPreview();
}

function removeConclusionFromReport(id) {
  _reportConclusions = _reportConclusions.filter(c => c.id !== id);
  _persistReportConclusions();
  updateReportPreview();
}

function removeChartFromReport(id) {
  _reportCharts = _reportCharts.filter(c => c.id !== id);
  _persistReportCharts();
  updateReportPreview();
}

function clearAllCharts() {
  _reportCharts = [];
  _reportConclusions = [];
  _persistReportCharts();
  _persistReportConclusions();
  updateReportPreview();
}

function updateReportPreview() {
  // 兼容 report-charts（旧）和 report-charts-preview（tab-report.html 实际使用）
  const container = document.getElementById('report-charts-preview') || document.getElementById('report-charts');
  const countEl = document.getElementById('report-chart-count');
  const concCountEl = document.getElementById('report-conclusion-count');
  if (countEl) countEl.textContent = _reportCharts.length;
  if (concCountEl) concCountEl.textContent = _reportConclusions.length;
  // 结论区
  const concContainer = document.getElementById('report-conclusions-preview');
  if (concContainer) {
    if (_reportConclusions.length === 0) {
      concContainer.innerHTML = '<div style="color:var(--ink-subtle);font-size:13px;text-align:center;padding:14px;">暂无结论，AI 助手/分析页生成的结论将自动同步到这里</div>';
    } else {
      concContainer.innerHTML = _reportConclusions.map(c => {
        return '<div class="report-conc-item" data-id="' + c.id + '">' +
          '<div style="display:flex;align-items:center;gap:8px;margin-bottom:2px;">' +
          '<b style="font-size:13px;">' + esc(c.title || '结论') + '</b>' +
          '<span style="font-size:10px;color:var(--ink-tertiary);">' + esc(c.source || '') + (c.time ? ' · ' + c.time : '') + '</span>' +
          '<button class="rcht-remove" onclick="removeConclusionFromReport(' + c.id + ')" style="margin-left:auto;">✕</button></div>' +
          '<div style="font-size:13px;line-height:1.7;color:var(--ink-muted);">' + esc(c.content) + '</div>' +
          '</div>';
      }).join('');
    }
  }
  if (!container) return;
  if (_reportCharts.length === 0) {
    container.innerHTML = '<div style="color:var(--ink-subtle);font-size:13px;text-align:center;padding:20px;">暂无图表，分析/图表标签页生成的图表将自动收集到这里</div>';
    return;
  }
  container.innerHTML = _reportCharts.map(c => {
    const src = c.html ? c.html : (c.img.indexOf('data:') === 0 ? c.img : '/api/chart/charts/' + c.img);
    return '<div class="report-chart-item" data-id="' + c.id + '">' +
      '<div class="rcht-title"><span>' + (c.title || '图表') + '</span>' +
      '<button class="rcht-remove" onclick="removeChartFromReport(' + c.id + ')">✕</button></div>' +
      '<div class="rcht-body">' + (c.html ? c.html : '<img src="' + src + '" style="max-width:100%;height:auto">') + '</div>' +
      (c.stats ? '<div class="rcht-stats">' + c.stats + '</div>' : '') +
      '<div class="rcht-meta" style="font-size:10px;color:var(--ink-tertiary);margin-top:4px;">' + (c.source || '') + (c.time ? ' · ' + c.time : '') + '</div>' +
      '</div>';
  }).join('');
}

function scanAndCollect() {
  const targets = document.querySelectorAll('.chart-result, .result-area, .ht-result, .ts-result, .fin-result, .ml-result, .analyze-result, .fs-result');
  targets.forEach(el => {
    if (!el.dataset.chartScanned && el.querySelector('img, .plotly-graph-div, canvas')) {
      el.dataset.chartScanned = 'true';
      const title = el.querySelector('.result-title, h4, b')?.textContent || '图表';
      const stats = el.querySelector('.result-stats, .stats')?.textContent || '';
      const img = el.querySelector('img');
      const plotly = el.querySelector('.plotly-graph-div');
      if (img && img.src) {
        saveChartToReport(img.src.split('/').pop(), title, stats, 'auto');
      } else if (plotly) {
        saveChartToReport('', title, stats, 'auto', plotly.outerHTML);
      }
    }
  });
}

const FLOW_RULES = {
  'analyst|csv|stats': ['tab-up', 'tab-clean', 'tab-analyze', 'tab-report'],
  'analyst|csv|ml': ['tab-up', 'tab-clean', 'tab-ml', 'tab-report'],
  'analyst|timeseries|predict': ['tab-up', 'tab-ts', 'tab-ml', 'tab-report'],
  'finance|stock|charts': ['tab-finance', 'tab-finance-viz', 'tab-report'],
  'finance|stock|backtest': ['tab-finance', 'tab-finance-model', 'tab-report'],
  'accountant|finstmt|finanalysis': ['tab-finance', 'tab-fs', 'tab-report'],
  'student|csv|stats': ['tab-up', 'tab-clean', 'tab-analyze', 'tab-report'],
  'ops|csv|clean_export': ['tab-up', 'tab-clean', 'tab-store', 'tab-report'],
  'dev|web|full_auto': ['tab-scrape', 'tab-clean', 'tab-analyze', 'tab-ml', 'tab-report']
};

const QUICK_FLOWS = {
  'stock_chart': { label: '查看股票K线图', flow: ['tab-finance', 'tab-finance-viz', 'tab-report'] },
  'stock_predict': { label: '股票价格预测', flow: ['tab-finance', 'tab-finance-model', 'tab-finance-viz'] },
  'fin_analysis': { label: '财务报表分析', flow: ['tab-finance', 'tab-fs', 'tab-report'] },
  'strategy_backtest': { label: '交易策略回测', flow: ['tab-finance', 'tab-finance-model', 'tab-report'] },
  'data_clean': { label: '数据清洗导出', flow: ['tab-up', 'tab-clean', 'tab-store', 'tab-report'] },
  'ml_train': { label: '机器学习建模', flow: ['tab-up', 'tab-clean', 'tab-ml', 'tab-report'] },
  'timeseries_forecast': { label: '时序趋势预测', flow: ['tab-up', 'tab-ts', 'tab-ml', 'tab-report'] },
  'hypothesis_test': { label: '统计假设检验', flow: ['tab-up', 'tab-ht', 'tab-report'] }
};

function recommendFlow() {
  const bg = document.getElementById('gd-bg').value;
  const data = document.getElementById('gd-data').value;
  const goal = document.getElementById('gd-goal').value;
  const area = document.getElementById('gd-flow-area');
  if (!area) return;

  const key = (bg || '') + '|' + (data || '') + '|' + (goal || '');
  const matched = FLOW_RULES[key];

  if (!matched) {
    area.innerHTML = '<div style="color:#888;padding:20px;text-align:center;">未找到精确匹配，请使用快捷场景或自由探索</div>';
    return;
  }

  area.innerHTML = '<div class="flow-steps">' + matched.map((tab, i) =>
    '<div class="flow-step" onclick="switchToTab(\'' + tab + '\')">' +
    '<span class="flow-num">' + (i + 1) + '</span>' +
    '<span class="flow-label">' + (_tabNames[tab] || tab) + '</span></div>'
  ).join('') + '</div>';
}

function quickFlow(scenario) {
  const flow = QUICK_FLOWS[scenario];
  if (!flow) return;
  const area = document.getElementById('gd-flow-area');
  if (!area) return;
  area.innerHTML = '<div class="flow-steps">' + flow.flow.map((tab, i) =>
    '<div class="flow-step" onclick="switchToTab(\'' + tab + '\')">' +
    '<span class="flow-num">' + (i + 1) + '</span>' +
    '<span class="flow-label">' + (_tabNames[tab] || tab) + '</span></div>'
  ).join('') + '</div>';
}

function clearFlow() {
  const area = document.getElementById('gd-flow-area');
  if (area) area.innerHTML = '请选择场景后点击"推荐流程"，系统将自动规划操作路径';
}

function switchToTab(tabId) {
  const btn = document.querySelector('.nbtn[data-t="' + tabId + '"]');
  if (btn) btn.click();
}

// ===== navigation.js =====

const _tabNames = {
  'tab-guide': '⚡ 智能流程',
  'tab-up': '📤 上传',
  'tab-store': '💾 存储',
  'tab-scrape': '🕷 爬取',
  'tab-finance': '💰 金融数据',
  'tab-finance-viz': '📊 可视化',
  'tab-finance-model': '💹 金融建模',
  'tab-screen': '🔍 筛选器',
  'tab-table': '🔢 表格工具',
  'tab-clean': '🧹 清洗',
  'tab-analyze': '📈 分析',
  'tab-did': '📊 DID计量',
  'tab-ml': '🧠 机器学习',
  'tab-pipeline': '⛓ Pipeline',
  'tab-nlp': '📝 NLP分析',
  'tab-fs': '📑 财务报表',
  'tab-ts': '📉 时序分析',
  'tab-ht': '🔬 假设检验',
  'tab-report': '📄 报告',
  'tab-nlq': '💬 自然语言查数',
  'tab-rag': '🔍 RAG 问答',
  'tab-agent-flow': '🧭 Agent 流程',
  'tab-security': '🔐 安全中心',
  'tab-ai-assistant': '🤖 AI 助手',
  'tab-help': '📖 使用教程'
};

const tabInitMap = {
  'tab-clean': rf, 'tab-analyze': rf, 'tab-store': rf, 'tab-up': rf,
  'tab-report': function() { if (typeof refreshReportList === 'function') refreshReportList(); else if (typeof rf === 'function') rf(); },
  'tab-help': loadDashboard,
  // 以下函数定义在其他 JS 文件中，用惰性包装避免未定义错误
  // 首次进入才拉历史：重复拉取会清空 DOM 里尚未落库的消息（消息无前端副本）
  'tab-ai-assistant': function() {
    if (typeof loadConclusions === 'function') loadConclusions();
    if (window.__aiHistoryLoaded) return;
    window.__aiHistoryLoaded = true;
    if (typeof loadChatHistory === 'function') loadChatHistory();
  },
  // 首次进入才给 iframe 设 src（画布页面 209KB，不拖慢首屏）
  'tab-agent-flow': function() { if (typeof agentFlowVizInit === 'function') agentFlowVizInit(); },
  'tab-nlp': function() { if (typeof loadNLPFiles === 'function') loadNLPFiles(); else console.warn('[TabInit] loadNLPFiles not loaded yet'); },
  'tab-ht': function() { if (typeof htTypeChange === 'function') htTypeChange(); else console.warn('[TabInit] htTypeChange not loaded yet'); }
};

function toggleAllNav() {
  const rows = document.querySelectorAll('.nav-row');
  rows.forEach(r => r.classList.toggle('collapsed'));
}

function showShortcutHelp() {
  // 尝试查找或创建快捷键帮助面板
  let el = document.getElementById('shortcut-help');
  if (!el) {
    // 动态创建帮助面板
    el = document.createElement('div');
    el.id = 'shortcut-help';
    el.style.cssText = 'position:fixed;top:50%;left:50%;transform:translate(-50%,-50%);z-index:9999;background:var(--bg-card,#fff);border:1px solid var(--border,#ddd);border-radius:12px;padding:24px;max-width:480px;box-shadow:0 8px 32px rgba(0,0,0,0.2);display:none;font-size:14px;line-height:1.8;';
    el.innerHTML = '<h3 style="margin:0 0 16px;color:#45B7D1;">⌨ Keyboard Shortcuts</h3>' +
      '<table style="width:100%;border-collapse:collapse;">' +
      '<tr><td style="padding:4px 8px;font-weight:600;font-family:monospace;background:rgba(0,0,0,0.04);">Ctrl+Shift+1~9</td><td>Switch Tab</td></tr>' +
      '<tr><td style="padding:4px 8px;font-weight:600;font-family:monospace;">Ctrl+Enter</td><td>Execute Action</td></tr>' +
      '<tr><td style="padding:4px 8px;font-weight:600;font-family:monospace;background:rgba(0,0,0,0.04);">Ctrl+Shift+L</td><td>Toggle Theme</td></tr>' +
      '<tr><td style="padding:4px 8px;font-weight:600;font-family:monospace;">Cmd+K / Ctrl+K</td><td>Command Palette</td></tr>' +
      '<tr><td style="padding:4px 8px;font-weight:600;font-family:monospace;background:rgba(0,0,0,0.04);">? or /</td><td>Show/Hide Help</td></tr>' +
      '<tr><td style="padding:4px 8px;font-weight:600;font-family:monospace;">Esc</td><td>Close</td></tr>' +
      '</table>' +
      '<div style="text-align:center;margin-top:16px;"><span style="color:#888;cursor:pointer;" onclick="showShortcutHelp()">Click anywhere or press ? to close</span></div>';
    document.body.appendChild(el);

    // 点击背景关闭（不点击内容区域）
    el.addEventListener('click', function(e) {
      if (e.target === el) {
        el.style.display = 'none';
      }
    });
  }

  const isVisible = el.style.display !== 'none';
  el.style.display = isVisible ? 'none' : 'block';
}

// ===== Command Palette (Cmd+K) =====
function updateStatusBarTab(tabId) {
  const el = document.getElementById('status-bar-tab');
  if (el) el.textContent = _tabNames[tabId] || tabId;
}

async function loadDashboard() {
  showLoading('加载仪表盘...');
  try {
    const r = await fetch('/api/dashboard');
    const data = await r.json();
    if (data.files !== undefined) {
      // 兼容数字和数组两种格式
      const count = Array.isArray(data.files) ? data.files.length : data.files;
      document.getElementById('db-file-count').textContent = count;
      if (Array.isArray(data.files)) renderFL(data.files);
    }
    if (data.models !== undefined) {
      const mCount = Array.isArray(data.models) ? data.models.length : data.models;
      document.getElementById('db-model-count').textContent = mCount;
    }
  } catch (e) {
    console.error('Dashboard load error:', e);
  } finally {
    initTutHelp();
    hideLoading();
  }
}

// ===== 使用教程页：目录点击展开（内联 onclick 调用，避免模板脚本执行时机问题） =====
function tutToggleChapter(id) {
  var d = document.getElementById(id);
  if (!d) return;
  if (!d.open) d.open = true;
  var tut = document.getElementById('tab-help');
  if (tut) {
    var links = Array.prototype.slice.call(tut.querySelectorAll('.tut-toc a, .tut-chips a'));
    links.forEach(function (a) {
      a.classList.toggle('active', a.getAttribute('href') === ('#' + id));
    });
  }
  var sum = d.querySelector('summary');
  if (sum) setTimeout(function () { sum.scrollIntoView({ behavior: 'smooth', block: 'start' }); }, 60);
}

function initTutHelp() {
  var tut = document.getElementById('tab-help');
  if (!tut) return;
  var bar = tut.querySelector('.tut-progress > i');
  var chapters = Array.prototype.slice.call(tut.querySelectorAll('details.tut-ch-d'));
  function updateProgress() {
    var n = chapters.filter(function (d) { return d.open; }).length;
    if (bar) bar.style.width = (chapters.length ? (n / chapters.length * 100) : 0) + '%';
  }
  if (!window.__tutHelpBound) {
    window.__tutHelpBound = true;
    chapters.forEach(function (d) {
      d.addEventListener('toggle', function () {
        updateProgress();
        if (d.open) tutToggleChapter(d.id);
      });
    });
    var expBtn = tut.querySelector('#tut-expand');
    var colBtn = tut.querySelector('#tut-collapse');
    if (expBtn) expBtn.onclick = function () { tut.querySelectorAll('.tut-mod').forEach(function (m) { m.open = true; }); };
    if (colBtn) colBtn.onclick = function () { tut.querySelectorAll('.tut-mod').forEach(function (m) { m.open = false; }); };
  }
  updateProgress();
}

function rf() {
  if (typeof loadMultiFiles === 'function') {
    loadMultiFiles();
  } else {
    console.warn('[TabInit] loadMultiFiles not loaded yet');
  }
}

function renderFL(files) {
  const el = document.getElementById('db-files');
  if (!el) return;
  if (!files || files.length === 0) {
    el.innerHTML = '<span style="color:var(--ink-subtle);font-size:13px;">暂无数据文件</span>';
    return;
  }
  el.innerHTML = files.map(f => {
    // 安全提取文件名（兼容对象和字符串）
    const name = typeof f === 'string' ? f : (f.name || f.filename || JSON.stringify(f).slice(0, 30));
    const size = f.size || '';
    // 后端删除接口按绝对路径定位文件（data-path 传值，规避路径中的引号/反斜杠转义问题）
    const p = (f && typeof f === 'object' && f.path) ? f.path : name;
    return '<div class="db-file-item"><span>' + esc(name) + '</span>' +
      '<span class="db-file-size">' + size + '</span>' +
      '<button class="db-file-del" data-path="' + esc(p) + '" data-fname="' + esc(name) +
      '" onclick="delF(this.dataset.path, this.dataset.fname)">🗑</button></div>';
  }).join('');
}

function delF(path, name) {
  if (!path) { showToast('缺少文件路径，无法删除', 'error'); return; }
  if (!confirm('确定删除 ' + (name || path) + ' ?')) return;
  fetch('/api/files/delete', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ path: path })
  })
    .then(r => r.json())
    .then(d => {
      // 必须校验结果：此前无条件提示"已删除"，实际 404 时文件仍在（静默失败）
      if (d && d.ok) { showToast('已删除', 'success'); rf(); }
      else {
        const msg = (d && d.errors && d.errors[0] && d.errors[0].error) || (d && d.error) || '未知错误';
        showToast('删除失败: ' + msg, 'error');
      }
    })
    .catch(e => showToast('删除失败: ' + e.message, 'error'));
}

// ===== 导航按钮点击事件绑定（必须早于 loadDashboard 执行） =====
const tabMultiFileTabs = ['tab-up', 'tab-table', 'tab-ts', 'tab-ht'];

document.querySelectorAll('.nbtn').forEach(b => {
  b.onclick = () => {
    // 移动端：点击导航后自动收起侧边栏抽屉
    const appEl = document.getElementById('app');
    if (appEl && window.innerWidth <= 720) appEl.classList.remove('mobile-open');
    if (b.classList.contains('on')) return;
    document.querySelectorAll('.nbtn').forEach(x => x.classList.remove('on'));
    b.classList.add('on');
    document.querySelectorAll('.sec').forEach(s => s.classList.remove('on'));
    document.getElementById(b.dataset.t).classList.add('on');
    if (b.dataset.t) saveCurrentTab(b.dataset.t);
    updateStatusBarTab(b.dataset.t);
    if (tabMultiFileTabs.includes(b.dataset.t)) loadMultiFiles();
    updateCurrentFileDisplay(b.dataset.t);
    if (tabInitMap[b.dataset.t]) tabInitMap[b.dataset.t]();
    if (b.dataset.t === 'tab-guide') {
      document.getElementById('back-flow-btn').classList.remove('show');
    }
  };
});

loadDashboard();
initTheme();
initKeyboardShortcuts();

// ===== 数据清洗（迁移自旧 core.js）=====
async function doClean() {
  const file = getCurrentFile();
  if (!file) return showToast('请选择数据源文件', 'warning');
  showLoading('正在清洗...');
  try {
    document.getElementById('cl-diag-card').classList.remove('hide');
    document.getElementById('cl-res-card').classList.add('hide');
    const r = await fetch('/api/clean', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({
        file, keep_dupes: !document.getElementById('c-dup').checked,
        fill: document.getElementById('c-fill').value,
        outlier: parseFloat(document.getElementById('c-out').value)||0,
      })
    });
    const d = await r.json();
    if (d.error) { document.getElementById('cl-diag').innerHTML = `<span class="te">${esc(d.error)}</span>`; showToast(d.error, 'error'); return; }
    let diagHtml = '';
    diagHtml += `<div class="stats"><div class="sc"><div class="sn">${d.diag.duplicates}</div><div class="sl">重复行</div></div>
      <div class="sc"><div class="sn">${d.diag.total_missing||0}</div><div class="sl">总缺失值</div></div>
      <div class="sc"><div class="sn">${d.rows_before}</div><div class="sl">原始行数</div></div>
      <div class="sc"><div class="sn">${d.rows_after}</div><div class="sl">清洗后行数</div></div></div>`;
    if (d.diag.missing_detail && d.diag.missing_detail.length) {
      diagHtml += '<table><tr><th>列名</th><th>缺失数</th><th>缺失比例</th></tr>';
      d.diag.missing_detail.forEach(m => {
        diagHtml += `<tr><td>${m.col}</td><td>${m.count}</td><td>${m.pct}%</td></tr>`;
      });
      diagHtml += '</table>';
    } else {
      diagHtml += '<p class="tm">无缺失值</p>';
    }
    document.getElementById('cl-diag').innerHTML = diagHtml;
    document.getElementById('cl-res-card').classList.remove('hide');
    document.getElementById('cl-info').textContent = `已保存: ${d.path}`;
    const parts = [];
    if (d.diag.duplicates > 0) parts.push('发现并删除 <b>' + d.diag.duplicates + '</b> 条重复行');
    if (d.diag.total_missing > 0) parts.push('共 <b>' + d.diag.total_missing + '</b> 个缺失值，已用 <b>' + {none:'不处理',drop:'删除行',mean:'均值',median:'中位数'}[document.getElementById('c-fill').value] + '</b> 填充');
    if (d.rows_before !== d.rows_after) parts.push('数据行数从 <b>' + d.rows_before + '</b> 减少到 <b>' + d.rows_after + '</b>（-'+((1-d.rows_after/d.rows_before)*100).toFixed(1)+'%）');
    const outSigma = parseFloat(document.getElementById('c-out').value)||0;
    if (outSigma > 0) parts.push('以 <b>'+outSigma+'σ</b> 阈值剔除异常值');
    if (d.diag.missing_detail) parts.push('数据完整性 <b>' + (100-(d.diag.missing_detail.reduce((a,m)=>a+m.count,0)/d.rows_after*100||0)).toFixed(1) + '%</b>');
    renderConclusion('cl-info', '清洗结论', parts.length ? parts : ['数据状态良好，无需特殊处理'], 'good');
    if (typeof refreshGlobalFileSelect === 'function') refreshGlobalFileSelect();
    showToast('清洗完成: ' + d.rows_after + ' 行', 'success');
  } catch(e) { showToast('清洗失败: ' + e.message, 'error'); }
  finally { hideLoading(); }
}

// 快照回滚对话框
function closeRollbackDialog() {
  const el = document.getElementById('rollback-dialog');
  if (el) el.style.display = 'none';
}

// ===== File Selection State =====

function getCurrentFile() {
  return window._selectedFile || null;
}

// 移动端：关闭侧边栏抽屉
function closeMobileSidebar() {
  const app = document.getElementById('app');
  if (app) app.classList.remove('mobile-open');
}

function refreshGlobalFileSelect() {
  const sel = document.getElementById('globalFileSelect');
  if (!sel) return;
  fetch('/api/files').then(r=>r.json()).then(data=>{
    const files = Array.isArray(data) ? data : (data.files || []);
    sel.innerHTML = '<option value="">选择文件...</option>';
    files.forEach(f=>{
      const name = typeof f==='string'?f:(f.name||f.filename||'unknown');
      sel.innerHTML += '<option value="'+name+'">'+name+'</option>';
    });
    if (window._selectedFile) sel.value = window._selectedFile;
  }).catch(()=>{});
}

function updateCurrentFileDisplay(tabId) {
  const file = getCurrentFile();
  const ids = ['analyzeFileName', 'mlFileName', 'cleanFileName', 'tsFileName', 'htFileName', 'financeFileName'];
  ids.forEach(id => {
    const el = document.getElementById(id);
    if (el) el.textContent = file || '未选择';
  });
}

(function initFileState() {
  // 不持久化文件选择：每次启动默认"未选择"，除非用户手动选择（仅当前会话有效）
  window._selectedFile = null;
  localStorage.removeItem('selectedFile');
  setTimeout(refreshGlobalFileSelect, 500);
  
  // 绑定顶栏文件选择器事件
  setTimeout(function(){
    var sel = document.getElementById('globalFileSelect');
    if (sel) {
      sel.onchange = function(){
        if (this.value && typeof window.selectFile === 'function') {
          window.selectFile(this.value);
        }
      };
    }
  }, 800);
})();

// ===== AI Assistant =====

// 多会话（2026-09-20）：当前会话键，持久化到 localStorage，刷新后仍回到同一会话。
// 默认 'legacy' = 升级前的老对话所在会话，保证既有记录照常可见。
const AI_CONV_KEY = 'ai_conversation_id';
function _currentConversationId() {
  try { return localStorage.getItem(AI_CONV_KEY) || 'legacy'; } catch (e) { return 'legacy'; }
}
function _setConversationId(cid) {
  const next = cid || 'legacy';
  window.__aiConversationId = next;
  try { localStorage.setItem(AI_CONV_KEY, next); } catch (e) {}
}

function toggleAIAssistant() {
  const tab = document.getElementById('tab-ai-assistant');
  if (!tab) return;
  switchToTab('tab-ai-assistant');
  const input = document.getElementById('ai-input');
  if (input) input.focus();
  updateAIContextBar();
  // 不再重复调用 loadChatHistory：switchToTab 已触发 tabInitMap（每次调用都会重绘消息区）
  loadConclusions();
}
// 显式挂到全局：header 按钮为内联 onclick，需保证全局可见（防加载时序竞态）
window.toggleAIAssistant = toggleAIAssistant;

async function loadChatHistory() {
  const container = document.getElementById('ai-messages');
  if (!container) return;
  // P0：消息只存在于 DOM（无前端内存副本），清空前先留快照。
  // 否则后端无可恢复记录时，切标签页会把进行中的会话整段抹掉。
  const snapshot = container.innerHTML;
  const hadMessages = !!container.querySelector('.ai-msg');
  try {
    // 多会话：只拉当前会话的消息；老会话已不在主存（100 条上限）时由后端回退读本地归档
    const cid = _currentConversationId();
    const r = await fetch('/api/assistant/history?conversation_id=' + encodeURIComponent(cid));
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const data = await r.json();
    if (!data || !data.ok) throw new Error(data && data.error ? data.error : '历史加载失败');

    // 聚合后端持久化记录与 Agent localStorage 记录，按时间戳去重排序
    const messages = [];
    const seen = new Set();

    function addMessages(list) {
      if (!Array.isArray(list)) return;
      list.forEach(function(msg) {
        if (!msg || !msg.role) return;
        const text = msg.content == null ? '' : String(msg.content);
        const key = (msg.timestamp || '') + '|' + msg.role + '|' + text.slice(0, 80);
        if (seen.has(key)) return;
        seen.add(key);
        messages.push({ role: msg.role, content: text, timestamp: msg.timestamp || '' });
      });
    }

    if (Array.isArray(data.data)) {
      addMessages(data.data);
    }
    if (typeof frontendAgent !== 'undefined' && frontendAgent.loadMessages) {
      addMessages(frontendAgent.loadMessages());
    }

    // 按时间戳排序（无时间戳的放最后）
    messages.sort(function(a, b) {
      if (!a.timestamp) return 1;
      if (!b.timestamp) return -1;
      return String(a.timestamp).localeCompare(String(b.timestamp));
    });

    if (!messages.length) {
      // 后端无记录：保留现场（此前会清空，导致切标签页后进行中的对话全部消失）
      if (!hadMessages) container.innerHTML = '';
      return;
    }

    container.innerHTML = '';
    messages.forEach(function(msg) {
      if (msg.role === 'user') {
        container.innerHTML += '<div class="ai-msg user">' + esc(msg.content) + '</div>';
      } else {
        container.innerHTML += '<div class="ai-msg assistant md">' + formatAIResponse(msg.content) + '</div>';
      }
    });

    container.scrollTop = container.scrollHeight;
  } catch (e) {
    console.error('loadChatHistory:', e);
    if (hadMessages) {
      // 加载失败：恢复快照，绝不把用户会话清掉
      container.innerHTML = snapshot;
      return;
    }
    container.innerHTML = '<div class="ai-msg assistant" style="border-color:var(--error);">❌ 加载聊天记录失败：' + esc(e.message || String(e)) + '</div>';
  }
}

// ===== 多会话：新建 / 切换 / 删除 =====

// 新建对话：后端生成合法会话键 → 切过去 → 清空消息区。
// 刻意不调 loadChatHistory()：那会把旧会话消息拉回来（语义同 tabInitMap 守卫）。
async function newAIConversation() {
  let cid = null;
  try {
    const r = await fetch('/api/assistant/new-conversation', { method: 'POST' });
    const d = await r.json();
    if (d && d.ok && d.conversation_id) cid = d.conversation_id;
  } catch (e) { console.error('newAIConversation:', e); }
  if (!cid) { showToast('新建对话失败，请重试', 'error'); return; }
  _setConversationId(cid);
  const container = document.getElementById('ai-messages');
  if (container) container.innerHTML = '';
  if (typeof loadAiSidebar === 'function') loadAiSidebar();
  const input = document.getElementById('ai-input');
  if (input) input.focus();
  showToast('已新建对话', 'success');
}
window.newAIConversation = newAIConversation;

// 切换到某个历史会话：拉该会话消息（老会话已不在主存时由后端回退读本地归档）。
async function switchAIConversation(cid) {
  if (!cid || cid === _currentConversationId()) return;
  _setConversationId(cid);
  const container = document.getElementById('ai-messages');
  // 占位符刻意不用 .ai-msg：否则 loadChatHistory 会当作"已有消息"保留现场，
  // 加载中提示就永远不消失。
  if (container) container.innerHTML = '<div class="ai-sb-empty">正在载入会话…</div>';
  await loadChatHistory();
  if (container && !container.querySelector('.ai-msg')) {
    container.innerHTML = '<div class="ai-msg assistant">💬 这是新对话，开始提问吧。</div>';
  }
  if (typeof loadAiSidebar === 'function') loadAiSidebar();
  updateAIContextBar();
}
window.switchAIConversation = switchAIConversation;

// 删除整个会话（主存消息 + 本地归档一起清），不影响其他会话。
async function deleteAIConversation(ev, cid) {
  if (ev) { ev.stopPropagation(); ev.preventDefault(); }
  if (!cid) return;
  if (!confirm('删除该对话？该会话的消息与本地归档都会被清除，且不可恢复。')) return;
  const wasCurrent = (cid === _currentConversationId());
  try {
    await fetch('/api/assistant/delete-conversation', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ conversation_id: cid })
    });
  } catch (e) { console.error('deleteAIConversation:', e); }
  if (wasCurrent) {
    // 删的就是当前会话 → 新开一个，避免后续消息写进已删会话
    await newAIConversation();
  } else if (typeof loadAiSidebar === 'function') {
    loadAiSidebar();
  }
  if (!wasCurrent) showToast('已删除对话', 'success');
}
window.deleteAIConversation = deleteAIConversation;

async function loadConclusions() {
  const container = document.getElementById('ai-conclusions');
  if (!container) return;
  try {
    const r = await fetch('/api/assistant/conclusions');
    const data = await r.json();
    if (data.ok && data.conclusions && data.conclusions.length > 0) {
      let html = '<div style="font-size:11px;font-weight:600;color:var(--ink-subtle);margin-bottom:8px;">📋 分析结论</div>';
      data.conclusions.forEach(function(c, i) {
        html += '<div style="padding:8px 12px;margin-bottom:6px;background:rgba(255,255,255,.03);border-radius:6px;border-left:3px solid var(--primary);">';
        html += '<div style="font-weight:600;color:var(--ink);font-size:12px;">' + esc(c.conclusion) + '</div>';
        if (c.details) html += '<div style="font-size:11px;color:var(--ink-subtle);margin-top:4px;">' + esc(c.details) + '</div>';
        html += '<div style="font-size:10px;color:var(--ink-tertiary);margin-top:4px;">' + esc(c.timestamp) + (c.file ? ' · ' + esc(c.file) : '') + '</div>';
        html += '</div>';
      });
      container.innerHTML = html;
    } else {
      container.innerHTML = '<div style="font-size:11px;color:var(--ink-subtle);text-align:center;padding:20px;">暂无分析结论</div>';
    }
  } catch (e) { console.error('loadConclusions:', e); }
}

// ===== AI 助手内部右栏（分析结论 + 会话列表）=====
function toggleAiSidebar() {
  const rail = document.getElementById('aiRail');
  if (!rail) return;
  const closed = rail.classList.toggle('closed');
  const t = document.getElementById('aiRailToggle');
  if (t) t.textContent = closed ? '»' : '«';
}
function openAIConclusion() {
  // 已在 AI 标签页内：切到该标签并把消息区滚到最新
  switchToTab('tab-ai-assistant');
  const m = document.getElementById('ai-messages');
  if (m) m.scrollTop = m.scrollHeight;
  updateAIContextBar();
}
async function loadAiSidebar() {
  const cEl = document.getElementById('aiSbConclusions');
  const hEl = document.getElementById('aiSbChat');
  const countEl = document.getElementById('aiRailCount');
  if (!cEl || !hEl) return;
  // 分析结论
  try {
    const r = await fetch('/api/assistant/conclusions');
    const d = await r.json();
    if (d.ok && Array.isArray(d.conclusions) && d.conclusions.length) {
      const list = d.conclusions.slice(0, 6);
      let html = '';
      list.forEach(function(c) {
        const title = (c.details ? esc(c.details) : (c.conclusion ? esc(c.conclusion) : ''));
        html += '<div class="ai-sb-item" title="' + title + '" onclick="openAIConclusion()">'
          + '<div class="ai-sb-item-text">' + esc(c.conclusion) + '</div>'
          + '<div class="ai-sb-item-meta">' + esc(c.timestamp || '') + (c.file ? ' · ' + esc(c.file) : '') + '</div>'
          + '</div>';
      });
      cEl.innerHTML = html;
      if (countEl) { countEl.style.display = 'grid'; countEl.textContent = d.conclusions.length; }
    } else {
      cEl.innerHTML = '<div class="ai-sb-empty">暂无分析结论</div>';
    }
  } catch (e) { cEl.innerHTML = '<div class="ai-sb-empty">加载失败</div>'; }
  // 会话列表（多会话）：后端按本地归档聚合，点条目切换会话。
  // 以归档为数据源 → 会话不会因主存 100 条上限而"消失"。
  try {
    const r = await fetch('/api/assistant/conversations');
    const d = await r.json();
    const cur = _currentConversationId();
    const list = (d && d.ok && Array.isArray(d.conversations)) ? d.conversations.slice() : [];
    // 刚新建、还没发首条消息的会话不在后端列表里 → 前端补一条，
    // 否则"点了新建却看不到新会话"，用户会以为按钮没生效。
    if (cur && !list.some(function(c) { return c && c.id === cur; })) {
      list.unshift({ id: cur, title: '新对话', timestamp: '', message_count: 0 });
    }
    const latest = list.slice(0, 10);
    if (latest.length) {
      let html = '';
      latest.forEach(function(c) {
        const active = c.id === cur;
        const n = c.message_count || 0;
        const title = c.title || c.id;
        html += '<div class="ai-sb-item' + (active ? ' active' : '') + '"'
          + ' title="' + esc(title) + '"'
          + ' onclick="switchAIConversation(\'' + esc(c.id) + '\')">'
          + '<div class="ai-sb-item-text">' + (active ? '▶ ' : '💬 ') + esc(title) + '</div>'
          + '<div class="ai-sb-item-meta">'
          + '<span class="ai-sb-del" title="删除该会话"'
          + ' onclick="deleteAIConversation(event,\'' + esc(c.id) + '\')">&times;</span>'
          + (n ? n + ' 条 · ' : '') + esc(c.timestamp || '')
          + '</div></div>';
      });
      hEl.innerHTML = html;
    } else {
      hEl.innerHTML = '<div class="ai-sb-empty">暂无对话记录</div>';
    }
  } catch (e) { hEl.innerHTML = '<div class="ai-sb-empty">加载失败</div>'; }
}

function formatAIResponse(content) {
  if (!content) return '';
  return formatMD(content);
}

// Markdown 渲染（标题/加粗/列表/表格/代码块/行内代码）
// P0-1：剥离模型输出中的思维链标签（后端已剥离，此处为流式渲染兜底）
// 支持完整块、未闭合开标签（流式中途）、零散标签与 ```thinking 围栏。
function stripThinking(text) {
  if (!text) return '';
  let s = String(text);
  if (s.indexOf('<') === -1 && s.indexOf('```') === -1) return s;
  s = s.replace(/```(?:thinking|think|reasoning)[\s\S]*?```/gi, '');
  s = s.replace(/<(thinking|think|reasoning|analysis|思考|分析)\b[^>]*>[\s\S]*?<\/\1\s*>/gi, '');
  s = s.replace(/<(thinking|think|reasoning|analysis)\b[^>]*>[\s\S]*$/gi, '');
  s = s.replace(/<\/?(thinking|think|reasoning)\s*>/gi, '');
  return s.replace(/\n{3,}/g, '\n\n').replace(/^\s+/, '');
}

function formatMD(text) {
  if (!text) return '';
  let html = String(stripThinking(text))
    .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  html = html.replace(/```(\w*)\n?([\s\S]*?)```/g, function(m, lang, code) {
    return '<pre style="background:rgba(0,0,0,.06);padding:10px 14px;border-radius:6px;font-size:12px;overflow-x:auto;border:1px solid rgba(0,0,0,.08);margin:6px 0;"><code>' + code.trim() + '</code></pre>';
  });
  html = html.replace(/`([^`]+)`/g, '<code style="background:rgba(0,0,0,.06);padding:1px 5px;border-radius:3px;font-size:12px;">$1</code>');
  html = html.replace(/^### (.+)$/gm, '<h4 style="margin:8px 0 4px;font-size:14px;">$1</h4>');
  html = html.replace(/^## (.+)$/gm, '<h3 style="margin:10px 0 4px;font-size:15px;">$1</h3>');
  html = html.replace(/\*\*(.+?)\*\*/g, '<b>$1</b>');
  html = html.replace(/\*(.+?)\*/g, '<i>$1</i>');
  html = html.replace(/^[\-\*] (.+)$/gm, '<li style="margin-left:16px;">$1</li>');
  html = html.replace(/^\|(.+)\|$/gm, function(m) {
    var cols = m.split('|').filter(c => c.trim());
    if (cols.every(c => /^[\-:]+$/.test(c.trim()))) return '';
    return '<tr>' + cols.map(c => '<td style="padding:3px 8px;border:1px solid rgba(0,0,0,.08);font-size:12px;">' + c.trim() + '</td>').join('') + '</tr>';
  });
  html = html.replace(/\n/g, '<br/>');
  html = html.replace(/<br\/>(<h[34]|<li|<tr>|<pre)/g, '$1');
  html = html.replace(/(<\/h[34]>|<\/li>|<\/tr>|<\/pre>)<br\/>/g, '$1');
  return html;
}

// 代码执行后的图表解读（自动洞察）
async function runCodeInsight(codeResult, step, container) {
  try {
    const stdout = codeResult.stdout || '';
    const numbers = {};
    const corrMatch = stdout.match(/相关系数:\s*([\-\d.]+)/);
    const meanMatch = stdout.match(/均值:\s*([\-\d.]+)/g);
    if (corrMatch) numbers.corr = parseFloat(corrMatch[1]);
    if (meanMatch) numbers.means = meanMatch.map(m => parseFloat(m.match(/[\-\d.]+/)[0]));
    let insight = '';
    if (numbers.corr !== undefined) {
      const abs = Math.abs(numbers.corr);
      let strength = abs > 0.7 ? '强' : abs > 0.4 ? '中等' : '弱';
      let dir = numbers.corr > 0 ? '正' : '负';
      insight += `📌 相关系数 ${numbers.corr.toFixed(4)}（${strength}${dir}相关）`;
    }
    if (numbers.means && numbers.means.length >= 2) {
      const ratio = numbers.means[1] / numbers.means[0];
      if (ratio > 1.5) insight += `｜第2列均值是第 ${ratio.toFixed(1)} 倍`;
      else if (ratio < 0.67) insight += `｜第2列均值是第 ${(1/ratio).toFixed(1)} 分之一`;
    }
    if (insight) {
      container.innerHTML += '<div class="ai-msg assistant" style="border-left:3px solid #22d3ee;font-size:12px;line-height:1.8;"><b>💡 图表解读：</b>' + insight + '</div>';
    }
  } catch(e) { /* 解读失败静默忽略 */ }
}

// 快捷提问：填充输入框并发送
// 更新 AI 上下文栏（当前文件/标签页）
function updateAIContextBar() {
  const fileEl = document.getElementById('ai-ctx-file');
  const tabEl = document.getElementById('ai-ctx-tab');
  if (!fileEl || !tabEl) return;
  
  const currentFile = getCurrentFile();
  if (currentFile) {
    fileEl.style.display = 'inline';
    fileEl.innerHTML = '📁 <b>' + esc(currentFile) + '</b>';
  } else {
    fileEl.style.display = 'none';
  }
  
  const activeTab = document.querySelector('.nbtn.on');
  if (activeTab) {
    tabEl.textContent = '📍 ' + activeTab.textContent.trim();
  }
  if (!_egressPolicy && !_egressLoading) loadEgressPolicy();   // 首次进来自动取一次出境状态
  updateEgressHint();
}

function updateAIFab() {
  const fab = document.getElementById('ai-fab');
  if (!fab) return;
  fab.style.display = 'block';
}

// 监听标签页切换
const _origSwitchToTab = switchToTab;
switchToTab = function(tabId) {
  _origSwitchToTab(tabId);
  updateAIContextBar();
  if (tabId === 'tab-ai-assistant') loadAiSidebar();
};

// 监听文件选择
const _origSelectFile = window.selectFile;
window.selectFile = function(fileName) {
  if (_origSelectFile) _origSelectFile(fileName);
  updateAIContextBar();
};

document.addEventListener('DOMContentLoaded', function() {
  updateAIFab();
  updateAIContextBar();
});

// ===== AI 识图：图片选择 / 压缩 / 预览 / 发送 =====
let _aiImages = [];   // [{name, dataUrl, size}]

function pickAIImage() {
  const inp = document.getElementById('ai-image-input');
  if (inp) inp.click();
}

// 图片压缩为 base64（最长边 1280，JPEG 0.85）
function compressImage(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = (e) => {
      const img = new Image();
      img.onload = () => {
        const MAX = 1280;
        let w = img.width, h = img.height;
        if (Math.max(w, h) > MAX) {
          if (w >= h) { h = Math.round(h * MAX / w); w = MAX; }
          else { w = Math.round(w * MAX / h); h = MAX; }
        }
        const canvas = document.createElement('canvas');
        canvas.width = w; canvas.height = h;
        const ctx = canvas.getContext('2d');
        ctx.drawImage(img, 0, 0, w, h);
        resolve(canvas.toDataURL('image/jpeg', 0.85));
      };
      img.onerror = reject;
      img.src = e.target.result;
    };
    reader.onerror = reject;
    reader.readAsDataURL(file);
  });
}

function handleAIImages(inp) {
  const files = Array.from(inp.files || []).slice(0, 4);
  if (!files.length) return;
  Promise.all(files.map(async (f) => {
    try { return { name: f.name, dataUrl: await compressImage(f) }; }
    catch (e) { return null; }
  })).then(items => {
    items.filter(Boolean).forEach(it => _aiImages.push(it));
    renderAIPreviews();
  }).finally(() => { inp.value = ''; });
}

function removeAIImage(idx) {
  _aiImages.splice(idx, 1);
  renderAIPreviews();
}

function renderAIPreviews() {
  const box = document.getElementById('ai-image-previews');
  if (!box) return;
  box.innerHTML = _aiImages.map((it, i) =>
    '<span style="position:relative;display:inline-block;">' +
    '<img src="' + it.dataUrl + '" style="width:56px;height:56px;object-fit:cover;border-radius:8px;border:1px solid var(--glass-border);">' +
    '<span onclick="removeAIImage(' + i + ')" style="position:absolute;top:-6px;right:-6px;width:18px;height:18px;border-radius:50%;background:#ef4444;color:#fff;font-size:11px;line-height:18px;text-align:center;cursor:pointer;">✕</span>' +
    '</span>').join('');
  box.style.display = _aiImages.length ? 'flex' : 'none';
}

async function sendAIMessage() {
  const input = document.getElementById('ai-input');
  if (!input || !input.value.trim()) return;
  const message = input.value.trim();
  input.value = '';
  
  // 本地拦截无意义/超长输入（纯标点表情、乱码），不发请求
  if (message.length > 2000) {
    showToast('消息过长（最多 2000 字）', 'warning');
    return;
  }
  if (!_aiImages.length && !/[\u4e00-\u9fffA-Za-z0-9]/.test(message)) {
    showToast('请输入有效内容后发送', 'warning');
    return;
  }
  
  const container = document.getElementById('ai-messages');
  if (!container) return;
  
  container.innerHTML += '<div class="ai-msg user">' + message + '</div>';
  // 图片消息气泡
  if (_aiImages.length) {
    container.innerHTML += '<div class="ai-msg user" style="padding:8px;">' +
      _aiImages.map(it => '<img src="' + it.dataUrl + '" style="max-width:160px;max-height:120px;border-radius:8px;margin:2px;">').join('') +
      '</div>';
  }
  
  const loadingId = 'ai-loading-' + Date.now();
  const modeHint = aiMode === 'fast' ? '⚡ 快速回答中...' : '🧠 深度思考中...';
  container.innerHTML += '<div class="ai-msg assistant" id="' + loadingId + '"><span class="spin"></span> ' + modeHint + '</div>';
  container.scrollTop = container.scrollHeight;
  
  // 显示停止生成按钮
  const stopBtn = document.getElementById('ai-stop-btn');
  if (stopBtn) stopBtn.style.display = '';
  
  try {
    const llmConfig = loadLLMConfig();
    // 单一模式开关：⚡快速 / 🧠深度思考；深度思考固定走后端流式深度分析
    // （技能加载/分步纪律/思考面板/防幻觉全在后端，旧的浏览器端 Agent 引擎已退役）
    const body = { message: message, llm_config: llmConfig, file: getCurrentFile() || '',
                   mode: aiMode, conversation_id: _currentConversationId() };
    // 多Agent协作 · 多模型协作：自动模式下由后端探测可用大模型并轮换分配（用户无需选择）
    const pipeCfg = loadLLMPipelineConfig();
    if (pipeCfg && pipeCfg.auto) {
      body.pipeline_auto = true;
    } else if (pipeCfg && pipeCfg.stages) {
      body.pipeline_models = pipeCfg.stages;
    }
    if (_aiImages.length) body.images = _aiImages.map(it => it.dataUrl);

    // 流式输出（SSE）：边生成边显示，感知提速
    await streamChatResponse(body, container, loadingId);
  } catch (e) {
    const loadingEl = document.getElementById(loadingId);
    if (loadingEl) loadingEl.remove();
    container.innerHTML += '<div class="ai-msg assistant" style="border-color:var(--error);">❌ 网络错误</div>';
  } finally {
    if (stopBtn) stopBtn.style.display = 'none';
    if (_aiImages.length) { _aiImages = []; renderAIPreviews(); }
  }
  
  container.scrollTop = container.scrollHeight;
}

// ===== Skills 技能管理（导入 / 列表 / 启停 / 删除） =====
let _skillsPanelOpen = false;

function toggleSkillsPanel() {
  _skillsPanelOpen = !_skillsPanelOpen;
  const body = document.getElementById('ai-skills-body');
  const hint = document.getElementById('ai-skills-toggle-hint');
  if (body) body.style.display = _skillsPanelOpen ? '' : 'none';
  if (hint) hint.textContent = _skillsPanelOpen ? '收起 ▴' : '展开 ▾';
  if (_skillsPanelOpen) loadSkills();
}

async function loadSkills() {
  const list = document.getElementById('ai-skills-list');
  const count = document.getElementById('ai-skills-count');
  if (!list) return;
  list.innerHTML = '<div style="font-size:12px;color:var(--ink-subtle);padding:4px;">加载中...</div>';
  try {
    const r = await fetch('/api/skills');
    const d = await r.json();
    const skills = d.skills || [];
    if (count) count.textContent = '(' + skills.length + ')';
    if (!skills.length) {
      list.innerHTML = '<div style="font-size:12px;color:var(--ink-subtle);padding:4px;">暂无技能，粘贴 SKILL.md 或上传 zip 导入。</div>';
      return;
    }
    list.innerHTML = skills.map(s =>
      '<div style="display:flex;align-items:center;gap:8px;padding:6px 8px;border:1px solid var(--glass-border);border-radius:8px;margin-bottom:6px;background:rgba(255,255,255,.02);">' +
      '<input type="checkbox" ' + (s.enabled ? 'checked' : '') + ' onchange="toggleSkill(\'' + s.name + '\', this.checked)" style="cursor:pointer;">' +
      '<div style="flex:1;min-width:0;">' +
      '<div style="font-size:13px;color:var(--ink);font-weight:600;">' + esc(s.name) + (s.enabled ? ' <span style="color:#34d399;font-size:11px;">● 已启用</span>' : '') + '</div>' +
      '<div style="font-size:11px;color:var(--ink-subtle);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">' + esc(s.description || '') + '</div>' +
      '</div>' +
      '<span style="font-size:11px;color:var(--ink-subtle);white-space:nowrap;">' + (s.body_len || 0) + ' 字' + (s.items && s.items.length ? ' · ' + s.items.length + ' 文件' : '') + '</span>' +
      '<button class="btn btn-w btn-sm" style="padding:2px 8px;font-size:11px;" onclick="deleteSkill(\'' + s.name + '\')">删除</button>' +
      '</div>').join('');
  } catch (e) {
    list.innerHTML = '<div style="font-size:12px;color:#f87171;padding:4px;">加载失败: ' + esc(e.message) + '</div>';
  }
}

async function toggleSkill(name, enabled) {
  try {
    const r = await fetch('/api/skills/' + encodeURIComponent(name) + '/toggle', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enabled })
    });
    const d = await r.json();
    if (!d.ok) { showToast(d.error || '切换失败', 'error'); }
    else { showToast((enabled ? '已启用' : '已停用') + '技能: ' + name, 'success'); }
  } catch (e) { showToast('操作失败: ' + e.message, 'error'); }
  loadSkills();
}

async function deleteSkill(name) {
  if (!confirm('确定删除技能「' + name + '」？')) return;
  try {
    const r = await fetch('/api/skills/' + encodeURIComponent(name), { method: 'DELETE' });
    const d = await r.json();
    if (!d.ok) { showToast(d.error || '删除失败', 'error'); return; }
    showToast('已删除技能: ' + name, 'success');
  } catch (e) { showToast('删除失败: ' + e.message, 'error'); }
  loadSkills();
}

async function importSkillFromForm() {
  const md = document.getElementById('ai-skill-md');
  const zip = document.getElementById('ai-skill-zip');
  const mdText = (md && md.value || '').trim();
  const zipFile = zip && zip.files && zip.files[0];
  if (!mdText && !zipFile) { showToast('请粘贴 SKILL.md 内容或选择 zip 文件', 'warning'); return; }
  showLoading('正在导入技能...');
  try {
    let r;
    if (zipFile) {
      const fd = new FormData();
      fd.append('file', zipFile);
      r = await fetch('/api/skills/import', { method: 'POST', body: fd });
    } else {
      r = await fetch('/api/skills/import', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ content: mdText })
      });
    }
    const d = await r.json();
    if (!d.ok) { showToast(d.error || '导入失败', 'error'); return; }
    showToast('技能导入成功: ' + d.name, 'success');
    if (md) md.value = '';
    if (zip) zip.value = '';
    loadSkills();
  } catch (e) {
    showToast('导入失败: ' + e.message, 'error');
  } finally {
    hideLoading();
  }
}

// ===== SSE 流式响应 =====

function parseSSE(chunk) {
  let event = 'message', data = null;
  chunk.split('\n').forEach(function(line) {
    if (line.startsWith('event:')) event = line.slice(6).trim();
    else if (line.startsWith('data:')) {
      try { data = JSON.parse(line.slice(5).trim()); } catch(e) { data = line.slice(5).trim(); }
    }
  });
  return data ? { event: event, data: data } : null;
}

// 全局流式生成中止控制器（停止按钮）+ 当前会话 ID（联动后端取消）
let _aiStreamAbort = null;
let _aiSessionId = null;

function stopAIGeneration() {
  if (_aiStreamAbort) _aiStreamAbort.abort();
  // 联动后端：通知 Agent 循环取消（否则后台线程会继续消耗 LLM 额度跑完）
  if (_aiSessionId) {
    const sid = _aiSessionId;
    fetch('/api/assistant/cancel/' + encodeURIComponent(sid), { method: 'POST' }).catch(function() {});
  }
}

// ===== 思考过程面板（Trae/Zcode 式）：计时器 + 执行计划 + 工具调用卡片 =====

// 3x3 网格加载动画：单元格沿边框顺时针逐格淡入并放大，循环播放。
// order 把网格位置映射为边框流转顺序（--d 0..8），CSS 依 --d 计算每格的动画延迟；
// 中心格（位置 4）最后点亮，呼应"计划→取数→收敛"的思考节奏。
function aiGridLoaderHTML() {
  const order = [0, 1, 2, 5, 8, 7, 6, 3, 4];
  let cells = '';
  for (let pos = 0; pos < 9; pos++) {
    const d = order.indexOf(pos);
    const cls = pos === 4 ? ' class="center"' : '';
    cells += '<i' + cls + ' style="--d:' + d + '"></i>';
  }
  return '<span class="ai-grid" aria-hidden="true">' + cells + '</span>';
}

const AI_TOOL_LABELS = {
  web_search: '🌐 联网检索', list_skills: '📋 查看技能', run_skill: '🧩 加载技能',
  run_analysis_api: '⚙️ 分析端点', describe: '📊 描述统计', overview: '📊 数据概览',
  columns: '📑 列结构', head: '👁 数据预览', group_compare: '📈 分组对比',
  correlation: '🔗 相关性', did_regression: '🧪 DID 回归', did_full: '🧪 DID 全流程',
  hypothesis_test: '🔬 假设检验', adf_test: '📉 平稳性检验', arima: '📈 时序预测',
  ml_train: '🤖 模型训练', feature_eng: '🛠 特征工程', clean_data: '🧹 数据清洗',
  generate_chart: '📊 生成图表', company_analysis: '🏢 公司分析', finance_data: '📈 行情数据',
  stock_valuation: '💰 估值分析', financial_statements: '📑 财务报表', financial_ratios: '🧮 财务比率',
  db_query: '🗄 数据库查询'
};

function _aiToolLabel(t) { return AI_TOOL_LABELS[t] || ('🔧 ' + t); }

function _aiToolSummary(params) {
  if (!params) return '';
  const prefer = ['query', 'skill_name', 'endpoint', 'company', 'symbol', 'file', 'col', 'column', 'target', 'test'];
  for (const k of prefer) {
    if (params[k]) return k + ': ' + String(params[k]).slice(0, 56);
  }
  const keys = Object.keys(params);
  if (!keys.length) return '';
  return keys.slice(0, 2).map(k => k + ': ' + String(params[k]).slice(0, 28)).join('  ');
}

function toggleThinkPanel(id) {
  const body = document.getElementById(id);
  if (!body) return;
  body.style.display = body.style.display === 'none' ? '' : 'none';
  const caret = document.getElementById(id + '-caret');
  if (caret) caret.textContent = body.style.display === 'none' ? '▾ 过程' : '▴ 过程';
}

// 把思考面板折叠为 details 并移到回复气泡之前；无实质内容时清理并返回 false
function _aiSealThinkPanel(loadingId, elapsedSec, toolCount) {
  const bubble = document.getElementById(loadingId);
  const body = document.getElementById(loadingId + '-think-body');
  if (!bubble || !body) return false;
  const planHtml = body.querySelector('.ai-think-plan').innerHTML;
  const toolsHtml = body.querySelector('.ai-think-tools').innerHTML;
  const stepsHtml = body.querySelector('.ai-think-steps').innerHTML;
  const think = bubble.querySelector('.ai-think');
  if (!planHtml && !toolsHtml && !stepsHtml) {
    if (think) think.remove();
    return false;
  }
  const det = document.createElement('details');
  det.className = 'ai-msg assistant ai-think-sealed';
  det.innerHTML =
    '<summary>🧠 思考过程' + (toolCount ? ' · ' + toolCount + ' 次工具调用' : '') +
    (elapsedSec ? ' · 用时 ' + elapsedSec + 's' : '') + '</summary>' +
    '<div class="ai-think-steps">' + stepsHtml + '</div>' +
    '<div class="ai-think-plan">' + planHtml + '</div>' +
    '<div class="ai-think-tools">' + toolsHtml + '</div>';
  bubble.parentNode.insertBefore(det, bubble);
  if (think) think.remove();
  return true;
}

async function streamChatResponse(body, container, loadingId) {
  _aiStreamAbort = new AbortController();
  _aiSessionId = body.session_id || ('web-' + Date.now());
  body.session_id = _aiSessionId;
  const t0 = Date.now();
  // 思考过程面板：注入 loading 气泡（计时器 + 计划 + 工具卡片区域）
  const thinkBodyId = loadingId + '-think-body';
  const loadingEl0 = document.getElementById(loadingId);
  if (loadingEl0) {
    loadingEl0.className = 'ai-msg assistant ai-thinking';
    loadingEl0.innerHTML =
      '<div class="ai-think">' +
        '<div class="ai-think-head" onclick="toggleThinkPanel(\'' + thinkBodyId + '\')">' +
          aiGridLoaderHTML() +
          '<span class="ai-think-label">思考中</span>' +
          '<span class="ai-think-meta"></span>' +
          '<span class="ai-timer">0.0s</span>' +
          '<span class="ai-think-caret" id="' + thinkBodyId + '-caret">▾ 过程</span>' +
        '</div>' +
        '<div class="ai-think-body" id="' + thinkBodyId + '" style="display:none;">' +
          '<div class="ai-think-steps"></div>' +
          '<div class="ai-think-plan"></div>' +
          '<div class="ai-think-tools"></div>' +
        '</div>' +
      '</div>';
  }
  let thinkTools = 0;
  let thinkCount = 0;
  let runningToolRow = null;
  const timer = setInterval(function() {
    const el = document.querySelector('#' + loadingId + ' .ai-timer');
    if (el) el.textContent = ((Date.now() - t0) / 1000).toFixed(1) + 's';
  }, 100);

  const r = await fetch('/api/assistant/chat', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(Object.assign({}, body, { stream: true })),
    signal: _aiStreamAbort.signal
  });
  if (!r.ok || !r.body) {
    clearInterval(timer);
    throw new Error('流式请求失败: ' + r.status);
  }
  
  const reader = r.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  let accumulated = '';
  let lastData = null;
  
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let idx;
      while ((idx = buffer.indexOf('\n\n')) !== -1) {
        const chunk = buffer.slice(0, idx);
        buffer = buffer.slice(idx + 2);
        const evt = parseSSE(chunk);
        if (!evt) continue;
        
        if (evt.event === 'status') {
          const el = document.querySelector('#' + loadingId + ' .ai-think-label');
          if (el) el.textContent = evt.data.message || '处理中...';
          // 思考时间线：每个阶段落一条，所有模式（快答/联网/知识/Agent）都有可见过程
          const stepsEl = document.querySelector('#' + loadingId + ' .ai-think-steps');
          if (stepsEl && evt.data.message) {
            const row = document.createElement('div');
            row.className = 'ai-step-row';
            row.innerHTML = '<span class="ai-step-t">+' + ((Date.now() - t0) / 1000).toFixed(1) + 's</span>' +
              esc(evt.data.message);
            stepsEl.appendChild(row);
          }
        } else if (evt.event === 'progress') {
          const d = evt.data || {};
          if (d.calls_count) thinkTools = d.calls_count;
          // 头部实时状态
          const label = document.querySelector('#' + loadingId + ' .ai-think-label');
          if (label && d.message) label.textContent = d.message;
          const meta = document.querySelector('#' + loadingId + ' .ai-think-meta');
          if (meta && d.calls_count) meta.textContent = d.calls_count + ' 次工具';
          // 首个结构化事件自动展开过程
          const tbody = document.getElementById(thinkBodyId);
          if (tbody && tbody.style.display === 'none' && (d.type === 'plan' || d.type === 'tool_start' || d.type === 'thinking')) {
            toggleThinkPanel(thinkBodyId);
          }
          if (d.type === 'thinking' && d.text) {
            // LLM 真实推理正文：可展开的推理步骤时间线（思考全量展示）
            thinkCount += 1;
            const stepsElT = document.querySelector('#' + loadingId + ' .ai-think-steps');
            if (stepsElT) {
              const trow = document.createElement('details');
              trow.className = 'ai-thought';
              trow.innerHTML = '<summary>💭 推理步骤 ' + thinkCount +
                '<span class="ai-thought-t">+' + ((Date.now() - t0) / 1000).toFixed(1) + 's</span></summary>' +
                '<div class="ai-thought-body">' + esc(String(d.text).slice(0, 1600)) +
                (String(d.text).length > 1600 ? '<div class="ai-thought-trunc">…（已截断，完整推理见运行轨迹）</div>' : '') +
                '</div>';
              stepsElT.appendChild(trow);
            }
          } else if (d.type === 'plan') {
            const planEl = document.querySelector('#' + loadingId + ' .ai-think-plan');
            if (planEl) {
              planEl.innerHTML = '<div class="ai-think-sec">📋 执行计划</div>' +
                (d.steps || []).map(function(s) {
                  return '<div class="ai-plan-step">○ ' + esc(String(s)) + '</div>';
                }).join('');
            }
          } else if (d.type === 'tool_start') {
            const toolsEl = document.querySelector('#' + loadingId + ' .ai-think-tools');
            if (toolsEl) {
              const row = document.createElement('div');
              row.className = 'ai-tool-row running';
              row.innerHTML = '<span class="ai-tool-ic"><span class="spin" style="width:10px;height:10px;"></span></span>' +
                '<span class="ai-tool-name">' + esc(_aiToolLabel(d.tool)) + '</span>' +
                '<span class="ai-tool-sum">' + esc(_aiToolSummary(d.params)) + '</span>';
              toolsEl.appendChild(row);
              runningToolRow = row;
            }
          } else if (d.type === 'tool_end') {
            const toolsEl = document.querySelector('#' + loadingId + ' .ai-think-tools');
            if (toolsEl) {
              const st = d.tool_status === 'ok' ? (d.cached ? '💾' : '✅') : '❌';
              const row = (runningToolRow && runningToolRow.parentNode === toolsEl) ? runningToolRow : document.createElement('div');
              row.className = 'ai-tool-row ' + (d.tool_status === 'ok' ? 'ok' : 'err');
              row.innerHTML = '<span class="ai-tool-ic">' + st + '</span>' +
                '<span class="ai-tool-name">' + esc(_aiToolLabel(d.tool)) + '</span>' +
                '<span class="ai-tool-sum">' + esc(_aiToolSummary(d.params)) + '</span>' +
                '<span class="ai-tool-ms">' + (d.elapsed_ms != null ? (d.elapsed_ms / 1000).toFixed(1) + 's' : '') + '</span>' +
                (d.preview ? '<div class="ai-tool-prev" title="' + esc(String(d.preview)) + '">' + esc(String(d.preview).slice(0, 110)) + '</div>' : '');
              if (row.parentNode !== toolsEl) toolsEl.appendChild(row);
            }
            runningToolRow = null;
          }
          container.scrollTop = container.scrollHeight;
        } else if (evt.event === 'delta') {
          // 首个正文 token：折叠思考面板，气泡开始流式渲染回复
          if (!accumulated) {
            _aiSealThinkPanel(loadingId, ((Date.now() - t0) / 1000).toFixed(1), thinkTools);
          }
          accumulated += (evt.data && evt.data.text) || '';
          const el = document.getElementById(loadingId);
          if (el) {
            if (!el.dataset.streamed) {
              el.dataset.streamed = '1';
              el.className = 'ai-msg assistant md';
            }
            el.innerHTML = formatMD(accumulated);
          }
          container.scrollTop = container.scrollHeight;
        } else if (evt.event === 'done') {
          lastData = evt.data;
        }
      }
    }
  } catch (e) {
    if (e.name === 'AbortError') {
      // 用户点击停止：保留已生成内容，标记停止
      clearInterval(timer);
      const el = document.getElementById(loadingId);
      if (el && accumulated) {
        el.dataset.streamed = '1';
        el.className = 'ai-msg assistant md';
      } else if (el) {
        _aiSealThinkPanel(loadingId, ((Date.now() - t0) / 1000).toFixed(1), thinkTools);
      }
      container.innerHTML += '<div class="ai-msg assistant" style="font-size:11px;color:var(--ink-tertiary);padding:2px 8px;">⏹ 已停止生成</div>';
      // 主动停止时服务端 SSE 的 finally 仍会落库已生成内容，右栏同样需要刷新
      _refreshAiSidebarSoon();
      return;
    }
    throw e;
  } finally {
    _aiStreamAbort = null;
    _aiSessionId = null;
    clearInterval(timer);
  }
  // 流式结束：收尾思考面板（有过程 → 折叠保留；无过程 → 还原为普通气泡占位）
  const finalSec = lastData && lastData.elapsed_ms ? (lastData.elapsed_ms / 1000).toFixed(1) : ((Date.now() - t0) / 1000).toFixed(1);
  // 处理链路摘要：意图 / 模式 / 链路（所有模式都注入，确保思考过程永远有内容）
  const stepsEl0 = document.querySelector('#' + loadingId + ' .ai-think-steps');
  if (stepsEl0 && lastData) {
    const srcMap = {agent: 'ReAct Agent 分步执行', agent_pipeline: '多Agent协作分析（按需命中阶段）', finance_live: '实时金融接口取数 + LLM 解读',
                    knowledge: '知识点检索 + LLM 综合', web: '联网搜索 + LLM 综合',
                    fast: '快速模式直答', rules: '规则引擎', debate: '多智能体辩论',
                    vision: '多模态识图', clarify: '引导式澄清'};
    const intent0 = lastData.intent && lastData.intent.action;
    const sum = document.createElement('div');
    sum.innerHTML = '<div class="ai-think-sec">🧠 处理链路</div>' +
      '<div class="ai-plan-step">意图: ' + esc(String(intent0 || '-')) +
      ' · 链路: ' + esc(String(srcMap[lastData.source] || lastData.source || '-')) +
      ' · 模式: ' + esc(String(body && body.mode || '')) + '</div>';
    stepsEl0.parentNode.insertBefore(sum, stepsEl0);
  }
  const hasThink = _aiSealThinkPanel(loadingId, finalSec, thinkTools);
  
  // 已渲染回复则保留气泡，否则移除 loading 占位
  const loadingEl = document.getElementById(loadingId);
  if (loadingEl && !loadingEl.dataset.streamed && !hasThink) loadingEl.remove();
  else if (loadingEl && !loadingEl.dataset.streamed) {
    // 面板已 seal 成独立 details，气泡本身不再需要
    loadingEl.remove();
  }
  
  if (lastData) {
    if (!lastData.ok) {
      container.innerHTML += '<div class="ai-msg assistant" style="border-color:var(--error);">❌ ' + esc(lastData.error || '出错了') + '</div>';
      return;
    }
    // 流式已渲染 reply：与最终结果比对决定「补差额」还是「整体替换」
    // - 同源且更长（如末尾追加免责声明）→ 只补差额，避免整段重渲闪动
    // - 内容分叉（终稿被纠正后重新生成 / 流式途中混入工具调用 JSON）→ 以最终结果为准整体重渲
    if (accumulated && lastData.reply) {
      const msgs = container.querySelectorAll('.ai-msg.assistant');
      const last = msgs[msgs.length - 1];
      const suffixOk = lastData.reply.length > accumulated.length &&
        lastData.reply.slice(0, accumulated.length) === accumulated;
      if (suffixOk) {
        const extra = lastData.reply.slice(accumulated.length);
        if (last && extra.trim()) last.innerHTML += '<span style="white-space:pre-wrap;">' + formatMD(extra) + '</span>';
      } else if (lastData.reply !== accumulated && last) {
        last.innerHTML = formatMD(lastData.reply);
      }
    }
    renderChatResult(lastData, container, { skipReply: !!accumulated });
    // 思考用时标注（所有模式；面板已含计时时此行省略）
    if (!hasThink) {
      container.innerHTML += '<div class="ai-msg assistant ai-time-meta">⏱ 本次回答用时 ' + finalSec + 's</div>';
    }
  } else if (!accumulated) {
    container.innerHTML += '<div class="ai-msg assistant" style="border-color:var(--error);">❌ 流式响应中断，请重试</div>';
  }
  container.scrollTop = container.scrollHeight;
  // 本轮问答已由服务端 record_chat_exchange 落库。右栏「会话列表」此前只在
  // 切标签页 / 页面加载时刷新（loadAiSidebar 仅两处调用）→ 在 AI 标签页内
  // 连续对话时它永远停在"暂无对话记录"，用户会以为记录没保存。
  _refreshAiSidebarSoon();
}

// 延迟刷新 AI 右栏（分析结论 + 会话列表）。
// 服务端落库发生在 SSE 的 finally 分支，客户端收到 done 事件时可能还没写完，
// 故留 600ms 余量再拉取，避免刚发完消息右栏仍是旧内容。
function _refreshAiSidebarSoon(delay) {
  setTimeout(function () {
    if (typeof loadAiSidebar === 'function') loadAiSidebar();
  }, typeof delay === 'number' ? delay : 600);
}

// 渲染 AI 回复结果（普通/流式共用；流式时 skipReply=true 跳过 reply 渲染避免重复）
async function renderChatResult(data, container, opts) {
  opts = opts || {};

  // P1-5：降级/截断提示。此前 agent_loop 返回的 truncated/degraded 在出口被白名单丢弃，
  // 前端 0 处消费 → 被轮次上限截断的报告按完整报告展示，用户无从判断完整性。
  // 2026-09-18 语义拆分：truncated=终稿被 max_tokens 截断（真问题）；
  // force_final=达到最大轮数自动收尾（正常流程，此前被误标成"截断"造成假警报）。
  if (data.truncated || data.degraded || data.force_final) {
    const _why = data.degraded
      ? '本次为降级结果（模型限流 / 数据源异常 / 耗时过长），内容可能不完整'
      : data.truncated
        ? '终稿因输出长度上限被截断，内容可能不完整'
        : '本轮分析已达最大轮数，自动收尾生成报告';
    const _whyIcon = data.force_final && !data.truncated && !data.degraded ? 'ℹ️' : '⚠️';
    container.innerHTML += '<div class="ai-msg assistant" style="border-left:3px solid var(--warning,#e0a800);font-size:12px;color:var(--ink-secondary);">' +
      _whyIcon + ' ' + esc(_why) + (data.end_status ? '（状态：' + esc(String(data.end_status)) + '）' : '') + '</div>';
  }
  // Skill 使用可见性：active_skill / skill 来自 agent trace（自动注入或 run_skill）
  if (data.trace && data.trace.active_skill) {
    container.innerHTML += '<div class="ai-msg assistant" style="font-size:11px;color:var(--ink-tertiary);padding:2px 8px;">' +
      '🧩 已使用技能：<b style="color:var(--accent);">' + esc(String(data.trace.active_skill)) + '</b></div>';
  }
  // 事实核查（数据必须真实）：展示核验数据清单
  if (data.no_tool_with_file) {
    container.innerHTML += '<div class="ai-msg assistant" style="border-left:3px solid var(--error,#e5484d);font-size:12px;color:var(--ink-secondary);">' +
      '⚠️ 本轮未成功调用数据工具，结论未基于真实文件核验，请谨慎参考。</div>';
  }
  if (data.fact_audit && data.fact_audit.total > 0) {
    const fa = data.fact_audit;
    const verified = Array.isArray(fa.verified_data) ? fa.verified_data : [];
    const unverified = Array.isArray(fa.unverified_data) ? fa.unverified_data : [];
    const ratio = typeof fa.ratio === 'number' ? Math.round(fa.ratio * 100) : null;
    const okAll = !unverified.length;
    const borderColor = okAll ? 'var(--success,#16a34a)' : 'var(--warning,#e0a800)';
    const vRows = verified.slice(0, 8).map(function (x) {
      return '<div style="font-size:11.5px;line-height:1.7;">✓ <code style="opacity:.9">' +
        esc(String(x.value)) + '</code> ← ' + esc(String(x.source || '工具返回')) + '</div>';
    }).join('');
    const uRows = unverified.slice(0, 8).map(function (x) {
      return '<div style="font-size:11.5px;line-height:1.7;color:var(--error,#e5484d);">✗ <code>' +
        esc(String(x.value)) + '</code> — ' + esc(String(x.reason || '未在本轮工具真实返回中找到')) + '</div>';
    }).join('');
    container.innerHTML += '<div class="ai-msg assistant" style="border-left:3px solid ' + borderColor + ';font-size:12px;color:var(--ink-secondary);">' +
      '<details' + (okAll ? '' : ' open') + '><summary><b>📋 核验数据（数据必须真实）</b>　' +
      '已核验 ' + (fa.verified || 0) + ' · 未核验 ' + (fa.unverified || 0) +
      (ratio != null ? '（未核验率 ' + ratio + '%）' : '') +
      (okAll ? '　✅ 全部可溯源' : '　⚠️ 未核验数字不得作为结论依据') +
      '</summary>' +
      (vRows ? '<div style="margin-top:6px;"><b style="font-size:11px;color:var(--ink-tertiary);">已核验（可作为结论依据）</b>' + vRows + '</div>' : '') +
      (uRows ? '<div style="margin-top:6px;"><b style="font-size:11px;color:var(--ink-tertiary);">未核验（不得视为真实数据）</b>' + uRows + '</div>' : '') +
      (Array.isArray(fa.sources) && fa.sources.length
        ? '<div style="margin-top:6px;font-size:11px;color:var(--ink-tertiary);">证据来源：' + esc(fa.sources.join('、')) + '</div>' : '') +
      '</details></div>';
  }
  // P1-5：暴露执行轨迹入口（trace_id 此前被出口丢弃，340 份轨迹资产无前端入口）
  if (data.trace_id) {
    container.innerHTML += '<div class="ai-msg assistant" style="font-size:11px;color:var(--ink-tertiary);padding:2px 8px;">' +
      '🔎 执行轨迹 <a href="/api/assistant/traces/' + encodeURIComponent(data.trace_id) + '" target="_blank" rel="noopener" style="color:var(--ink-tertiary);">' +
      esc(data.trace_id) + '</a>　<a href="/static/agent-problems.html" target="_blank" rel="noopener" style="color:var(--ink-tertiary);">问题页</a></div>';
  }
  // 多Agent协作：阶段成败明细。此前 stages 只有 {stage, chars}，阶段失败时前端
  // 无从判断是哪一步挂了（agent_pipeline 现已返回 ok / error / elapsed_s / provider）。
  if (Array.isArray(data.stages) && data.stages.length) {
    const _rows = data.stages.map(function (s) {
      const _ok = s.ok !== false;
      const _meta = [];
      if (typeof s.chars === 'number') _meta.push(s.chars + ' 字');
      if (s.elapsed_s != null) _meta.push(s.elapsed_s + 's');
      if (s.provider) _meta.push(esc(String(s.provider)) + '/' + esc(String(s.model || '')));
      const _err = (!_ok && s.error)
        ? '　<span style="color:var(--error);">' + esc(String(s.error).slice(0, 120)) + '</span>' : '';
      return '<div style="font-size:12px;line-height:1.9;">' + (_ok ? '✅' : '❌')
        + ' <b>' + esc(String(s.stage)) + '</b>'
        + '　<span style="color:var(--ink-tertiary);">' + _meta.join(' · ') + '</span>' + _err + '</div>';
    }).join('');
    container.innerHTML += '<div class="ai-msg assistant" style="font-size:12px;color:var(--ink-secondary);">'
      + '<div style="margin-bottom:4px;">🤝 <b>多Agent协作阶段</b></div>' + _rows + '</div>';
  }
  
  // 显示数据上下文（如果有）
  if (data.data_context) {
    container.innerHTML += '<div class="ai-msg assistant ai-context"><details><summary>📊 当前数据概览</summary><pre>' + data.data_context + '</pre></details></div>';
  }
  
  // 显示自动洞察（AI 对数据的一句话业务结论）
  if (data.insight) {
    container.innerHTML += '<div class="ai-msg assistant" style="border-left:3px solid var(--primary,#45B7D1);"><b>💡 数据洞察：</b><span style="font-size:13px;line-height:1.8;">' + esc(data.insight) + '</span></div>';
  }
  
  // 显示增强分析报告
  if (data.enhanced_report) {
    container.innerHTML += '<div class="ai-msg assistant md" style="border-left:3px solid #22d3ee;padding:12px 16px;background:rgba(34,211,238,.03);"><b>📊 增强分析报告</b><div style="margin-top:8px;font-size:13px;line-height:1.8;">' + formatMD(data.enhanced_report) + '</div></div>';
  }
  
  // 显示 LLM 回复（流式模式已渲染，跳过）
  if (data.reply && !data.insight && !opts.skipReply) {
    // ops_* = 智能运营确定性应答（ops_faq / ops_product_match / ops_guide），同样走 Markdown 渲染
    if (data.source === 'knowledge' || data.source === 'web' || data.source === 'agent' || (data.source || '').indexOf('ops_') === 0) {
      const accent = data.source === 'knowledge' ? 'var(--accent,#45B7D1)' : (data.source === 'agent' ? '#22d3ee' : '#22c55e');
      container.innerHTML += '<div class="ai-msg assistant md" style="border-left:3px solid ' + accent + ';padding:12px 16px;">' + formatMD(data.reply) + '</div>';
    } else {
      container.innerHTML += '<div class="ai-msg assistant">' + esc(data.reply) + '</div>';
    }
  }

  // 渲染分析建议卡片（用户确认后自动执行）
  if (data.suggestions && data.suggestions.length > 0) {
    const sugHtml = data.suggestions.map(function(s, i) {
      // 显示关键参数（file 除外），帮助用户预览建议内容
      const extra = Object.keys(s.params || {}).filter(function(k) { return k !== 'file'; })
        .map(function(k) { return k + '=' + s.params[k]; }).join(' ');
      return '<div style="display:flex;align-items:center;gap:8px;padding:5px 0;font-size:13px;">' +
        '<span style="color:var(--ink-subtle);min-width:16px;">' + (i + 1) + '.</span>' +
        '<span>' + esc(s.label || s.action) + '</span>' +
        '<span style="color:var(--ink-tertiary);font-size:11px;margin-left:2px;">(' + esc(s.action) + (extra ? ' · ' + esc(extra) : '') + ')</span></div>';
    }).join('');
    container.innerHTML += '<div class="ai-msg assistant" id="ai-suggestions-card" style="border-left:3px solid #22d3ee;padding:10px 14px;background:rgba(34,211,238,.03);">' +
      '<div style="font-size:13px;font-weight:600;margin-bottom:6px;">💡 分析建议（确认后自动执行并同步到报告）</div>' + sugHtml +
      '<div style="display:flex;gap:8px;margin-top:10px;">' +
      '<button class="btn btn-p btn-sm" onclick="executeAISuggestions(this)">✅ 采纳并执行</button>' +
      '<button class="btn btn-s btn-sm" onclick="dismissAISuggestions(this)">忽略</button>' +
      '</div></div>';
    container.scrollTop = container.scrollHeight;
  }

  // 联网回答来源链接（可点击）
  if (data.source === 'web' && data.sources && data.sources.length) {
    const links = data.sources.map(function(s, i) {
      return '<a href="' + esc(s.url) + '" target="_blank" rel="noopener" style="color:var(--accent);text-decoration:none;margin-right:12px;">[' + (i + 1) + '] ' + esc(s.title) + '</a>';
    }).join('');
    container.innerHTML += '<div class="ai-msg assistant" style="font-size:11px;color:var(--ink-subtle);padding:2px 8px;">🔗 来源：' + links + '</div>';
  }
  
  // 显示来源标签
  if (data.source === 'llm') {
    container.innerHTML += '<div class="ai-msg assistant" style="font-size:10px;color:var(--ink-tertiary);padding:2px 8px;">🤖 ' + (data.plan?.model || 'LLM') + '</div>';
  } else if (data.source === 'knowledge') {
    container.innerHTML += '<div class="ai-msg assistant" style="font-size:10px;color:var(--ink-tertiary);padding:2px 8px;cursor:pointer;" onclick="var b=document.querySelector(\'[data-t=tab-knowledge]\'); if(b) b.click();" title="点击跳转到知识点库">📚 知识点库（点击查看全部）</div>';
  } else if (data.source === 'fast') {
    container.innerHTML += '<div class="ai-msg assistant" style="font-size:10px;color:var(--ink-tertiary);padding:2px 8px;">⚡ 快速模式</div>';
  } else if (data.source === 'web') {
    container.innerHTML += '<div class="ai-msg assistant" style="font-size:10px;color:var(--ink-tertiary);padding:2px 8px;">🌐 联网搜索</div>';
  } else if (data.source === 'agent') {
    container.innerHTML += '<div class="ai-msg assistant" style="font-size:10px;color:var(--ink-tertiary);padding:2px 8px;">🤖 Agent 深度分析 · 已调用网站分析功能</div>';
  } else if ((data.source || '').indexOf('ops_') === 0) {
    container.innerHTML += '<div class="ai-msg assistant" style="font-size:10px;color:var(--ink-tertiary);padding:2px 8px;">🧭 智能运营 · 确定性应答（规则匹配，非模型生成）</div>';
  }

  // 风控初审（确定性规则，非模型判断）：pass 不打扰，needs_review / block 显式提示命中项与转人工状态。
  // 后端 risk_review.attach() 已把结论追加进 reply，此处为【结构化徽标】，便于扫描与合规留痕。
  if (data.risk_review && data.risk_review.verdict && data.risk_review.verdict !== 'pass') {
    const _rv = data.risk_review;
    const _blocked = _rv.verdict === 'block';
    const _items = (_rv.findings || []).map(function (f) { return '· ' + esc(f.label); }).join('<br>');
    container.innerHTML += '<div class="ai-msg assistant" style="border-left:3px solid ' + (_blocked ? '#ef4444' : '#eab308') + ';padding:8px 12px;font-size:12px;">' +
      '<b>' + (_blocked ? '🔴 风控初审：拦截 · 已转人工' : '🟡 风控初审：需人工复核') + '</b>' +
      (_items ? '<div style="margin-top:4px;color:var(--ink-secondary);">' + _items + '</div>' : '') +
      (_rv.handoff ? '<div style="margin-top:4px;color:var(--ink-tertiary);font-size:11px;">⚖️ 未经人工确认前请勿对外发布或用于决策</div>' : '') + '</div>';
  }

  // 显示 token 用量（Agent / LLM 消耗，用于成本透明）
  if (data.usage && (data.usage.total_tokens || data.usage.prompt_tokens || data.usage.completion_tokens)) {
    const t = data.usage.total_tokens || ((data.usage.prompt_tokens || 0) + (data.usage.completion_tokens || 0));
    const cost = data.usage.cost_cny ? ' · ¥' + data.usage.cost_cny : '';
    container.innerHTML += '<div class="ai-msg assistant" style="font-size:10px;color:var(--ink-tertiary);padding:2px 8px;">⚡ 本次消耗 ' + t + ' tokens' + cost + '</div>';
  }
  
  // 批量执行建议链
  if (data.batch_chain && data.batch_chain.length > 0) {
    const totalSteps = data.batch_chain.length;
    container.innerHTML += '<div class="ai-msg assistant" style="border-left:3px solid #22d3ee;padding:8px 12px;">🚀 正在批量执行 ' + totalSteps + ' 个建议...</div>';
    container.scrollTop = container.scrollHeight;

    for (let i = 0; i < totalSteps; i++) {
      const stepInfo = data.batch_chain[i];
      const stepDiv = document.createElement('div');
      stepDiv.className = 'ai-msg assistant';
      stepDiv.style.borderLeft = '3px solid var(--primary,#45B7D1)';
      stepDiv.style.padding = '6px 12px';
      stepDiv.style.fontSize = '12px';
      stepDiv.textContent = '⏳ 执行建议 ' + (i + 1) + '/' + totalSteps + ': ' + (stepInfo.action || '...');
      container.appendChild(stepDiv);
      container.scrollTop = container.scrollHeight;

      const actionMessages = {
        'correlation': '进行相关性分析，识别关键驱动因素',
        'ts_decompose': '使用时序分解分析趋势和季节性',
        'group_data': '按关键维度分组，比较不同类别的指标差异',
        'train_model': '训练预测模型',
        'create_chart': '生成图表',
        'clean_data': '清洗数据',
        'analyze_data': '分析数据'
      };
      const msg = actionMessages[stepInfo.action] || stepInfo.text || stepInfo.action;

      try {
        const r2 = await fetch('/api/assistant/chat', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            message: msg,
            llm_config: loadLLMConfig(),
            file: stepInfo.params?.file || getCurrentFile() || ''
          })
        });
        const d2 = await r2.json();
        if (d2.ok) {
          stepDiv.textContent = '✅ ' + (i + 1) + '/' + totalSteps + ' ' + (stepInfo.action || '完成');
          if (d2.plan && d2.plan.steps && d2.plan.steps.length > 0) {
            await executePlan(d2.plan, container);
          }
        } else {
          stepDiv.textContent = '❌ ' + (i + 1) + '/' + totalSteps + ' 失败: ' + (d2.error || '错误');
        }
      } catch (e) {
        stepDiv.textContent = '❌ ' + (i + 1) + '/' + totalSteps + ' 网络错误';
      }
    }
    container.innerHTML += '<div class="ai-msg assistant" style="padding:8px 12px;border-left:3px solid #10b981;">🎉 全部 ' + totalSteps + ' 个建议执行完成！</div>';
    // 智能流程每步都走 /api/assistant/chat（同步路径），同样会落库，右栏需同步刷新
    _refreshAiSidebarSoon();
  }

  // 执行计划：有建议时等用户确认，其余自动执行
  if (data.plan && data.plan.steps && data.plan.steps.length > 0) {
    if (data.suggestions && data.suggestions.length > 0) {
      window._pendingAISuggestionsPlan = data.plan;
    } else {
      await executePlan(data.plan, container);
    }
  } else if (!data.batch_chain || data.batch_chain.length === 0) {
    // 流式已显示回复时不再追加「✅ 完成」
    if (!opts.skipReply) {
      container.innerHTML += '<div class="ai-msg assistant">✅ 完成</div>';
    }
  }
  
  // AI 结论自动同步到本次会话报告
  if (data && data.reply && typeof data.reply === 'string' && data.reply.trim().length > 5) {
    const _opsTag = (data.source || '').indexOf('ops_') === 0 ? '🧭 运营应答' : '';
    const srcTag = _opsTag || (data.source === 'knowledge' ? '📚 知识回答' : (data.source === 'web' ? '🌐 联网回答' : (data.source === 'agent' ? '🤖 Agent 深度分析' : '🤖 AI 助手')));
    const isError = data.reply.indexOf('❌') === 0 || data.reply.indexOf('⚠️') === 0;
    if (!isError) {
      saveConclusionToReport(srcTag, data.reply.slice(0, 500), 'AI 助手');
    }
  }
  
  // 刷新结论
  loadConclusions();
}

// 用户采纳 AI 建议后，自动执行对应操作并同步图表到报告
async function executeAISuggestions(btn) {
  const plan = window._pendingAISuggestionsPlan;
  const container = document.getElementById('ai-messages');
  if (!plan || !plan.steps || !plan.steps.length) {
    showToast('没有可执行的建议', 'warning');
    return;
  }
  const card = btn ? btn.closest('.ai-msg') : document.getElementById('ai-suggestions-card');
  if (card) {
    card.querySelectorAll('button').forEach(function(b) { b.disabled = true; });
    if (btn) btn.textContent = '执行中...';
  }
  try {
    await executePlan(plan, container);
    if (card) card.querySelectorAll('button').forEach(function(b) { b.disabled = false; });
    showToast('建议已执行，图表已同步到「报告」页', 'success');
  } catch (e) {
    if (card) card.querySelectorAll('button').forEach(function(b) { b.disabled = false; });
    showToast('执行失败: ' + e.message, 'error');
  }
}

function dismissAISuggestions(btn) {
  const card = btn ? btn.closest('.ai-msg') : document.getElementById('ai-suggestions-card');
  if (card) card.style.display = 'none';
  window._pendingAISuggestionsPlan = null;
}

function esc(s) {
  if (!s) return '';
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

async function executePlan(plan, container) {
  const steps = plan.steps;
  const totalSteps = steps.length;
  
  // 顶部进度条
  const barId = 'ai-plan-bar-' + Date.now();
  const pct = Math.round((0 / totalSteps) * 100);
  container.innerHTML += '<div class="ai-msg assistant" id="' + barId + '" style="padding:6px 10px;">' +
    '<div style="display:flex;justify-content:space-between;font-size:11px;color:var(--ink-subtle);margin-bottom:4px;">' +
    '<span>执行计划</span><span id="' + barId + '-pct">0/' + totalSteps + '</span></div>' +
    '<div style="height:4px;background:rgba(255,255,255,.08);border-radius:2px;overflow:hidden;">' +
    '<div id="' + barId + '-fill" style="height:100%;width:0%;background:linear-gradient(90deg,#45B7D1,#22d3ee);border-radius:2px;transition:width .3s;"></div></div>' +
    '</div>';
  
  for (let i = 0; i < totalSteps; i++) {
    const step = steps[i];
    const progressId = 'ai-progress-' + step.id;
    
    // 显示进度
    const progressHtml = '<div class="ai-msg assistant" id="' + progressId + '" style="padding:6px 10px;">' +
      '<div style="display:flex;align-items:center;gap:8px;">' +
      '<span class="spin" style="width:12px;height:12px;border-width:2px;"></span>' +
      '<span style="font-size:12px;">[' + (i+1) + '/' + totalSteps + '] ' + step.description + '</span>' +
      '</div></div>';
    container.innerHTML += progressHtml;
    container.scrollTop = container.scrollHeight;
    
    try {
      // 执行步骤（AI 分析类步骤不重试，避免重复调用 LLM 浪费 16 秒）
      let result = await executeStep(step, container);
      if (!result.ok && step.action !== 'show_message' && step.action !== 'autonomous_analysis') {
        // 重试一次
        await new Promise(r => setTimeout(r, 500));
        result = await executeStep(step, container);
      }
      
      // 更新进度为完成
      const progressEl = document.getElementById(progressId);
      if (progressEl) {
        const statusIcon = result.ok ? '✅' : '❌';
        progressEl.innerHTML = '<div style="display:flex;align-items:center;gap:8px;">' +
          '<span>' + statusIcon + '</span>' +
          '<span style="font-size:12px;">[' + (i+1) + '/' + totalSteps + '] ' + step.description + '</span>' +
          '</div>';
      }
      
      // 更新顶部进度条
      const fill = document.getElementById(barId + '-fill');
      const pctEl = document.getElementById(barId + '-pct');
      if (fill) fill.style.width = Math.round(((i+1) / totalSteps) * 100) + '%';
      if (pctEl) pctEl.textContent = (i+1) + '/' + totalSteps;
      
      if (!result.ok && step.action !== 'show_message') {
        container.innerHTML += '<div class="ai-msg assistant" style="border-color:var(--error);font-size:12px;">❌ 步骤失败: ' + esc(result.error || '执行失败') + '（已重试）</div>';
        break;
      }
      
      // run_code 完成后，如果有图表，调用 InsightEngine 解读
      if (step.action === 'run_code' && result.ok && result.data && result.data.output_type === 'image' && typeof runCodeInsight === 'function') {
        runCodeInsight(result.data, step, container);
      }
      
    // 最后一步或 show_message/show_structured_report 显示结果
    if (step.action === 'show_message' && result.content) {
      container.innerHTML += '<div class="ai-msg assistant md">' + formatMD(result.content) + '</div>';
    } else if (step.action === 'run_code') {
      // 显示代码执行结果（图片/文本/HTML）
      const codeResult = result.data;
      if (codeResult && codeResult.output) {
        if (codeResult.output_type === 'image') {
          container.innerHTML += '<div class="ai-msg assistant"><b>📊 执行结果：</b><br/><img src="' + codeResult.output + '" style="max-width:100%;border-radius:8px;margin-top:8px;"></div>';
          // 自动收集到"本次会话图表"（AI 生成的图表同步到报告页）
          if (typeof saveChartToReport === 'function' && codeResult.output.indexOf('data:image') === 0) {
            saveChartToReport(codeResult.output, 'AI 生成图表', (codeResult.stats || ''), 'AI 代码执行');
          }
        } else if (codeResult.output_type === 'html') {
          container.innerHTML += '<div class="ai-msg assistant"><b>📊 执行结果：</b><div style="margin-top:8px;">' + codeResult.output + '</div></div>';
        } else {
          const stdout = codeResult.stdout || codeResult.output || '';
          const stderr = codeResult.stderr || '';
          let html = '<div class="ai-msg assistant"><b>💻 代码执行完成</b>';
          if (stdout) html += '<pre style="background:rgba(0,0,0,.06);padding:8px 12px;border-radius:6px;font-size:12px;margin-top:8px;max-height:200px;overflow:auto;white-space:pre-wrap;border:1px solid rgba(0,0,0,.08);">' + esc(stdout) + '</pre>';
          if (stderr) html += '<pre style="background:rgba(248,113,113,.06);padding:8px 12px;border-radius:6px;font-size:12px;margin-top:8px;color:var(--error);white-space:pre-wrap;">' + esc(stderr) + '</pre>';
          if (codeResult.error) html += '<div style="color:var(--error);margin-top:8px;font-size:13px;">❌ ' + esc(codeResult.error) + '</div>';
          html += '</div>';
          container.innerHTML += html;
        }
      } else if (codeResult && codeResult.error) {
        container.innerHTML += '<div class="ai-msg assistant" style="border-color:var(--error);">❌ ' + esc(codeResult.error) + '</div>';
      }
    } else if (step.action === 'show_structured_report') {
      // 生成结构化报告（优先使用真实执行结果，而非静态模板）
      const file = step.params.file || '';
      const type = step.params.type || 'analyze';
      const chartType = step.params.chart_type || '';
      const symbol = step.params.symbol || '';
      const results = window._aiStepResults || {};
      
      let reportHtml = '<div class="ai-msg assistant ai-report">';
      
      if (type === 'analyze' || type === 'auto') {
        const st = results['/api/describe'];
        if (st && st.ok && (st.stats_html || st.conclusion)) {
          reportHtml += '<div class="report-section"><h4>🎯 描述性统计</h4>';
          if (st.conclusion && st.conclusion.length) {
            const conc = Array.isArray(st.conclusion) ? st.conclusion.join('<br/>') : st.conclusion;
            reportHtml += '<div style="margin-bottom:8px;padding:8px 10px;background:rgba(94,106,210,.08);border-radius:6px;font-size:13px;line-height:1.8;">📌 ' + conc + '</div>';
            saveConclusionToReport('📈 描述性统计', conc, 'AI 分析');
          }
          if (st.stats_html) reportHtml += '<div style="max-height:240px;overflow:auto;font-size:12px;">' + st.stats_html + '</div>';
          reportHtml += '</div>';
        } else if (st && st.error) {
          reportHtml += '<div class="report-section"><h4>🎯 分析失败</h4><p style="color:var(--error);">' + esc(st.error) + '</p></div>';
        } else {
          reportHtml += '<div class="report-section"><h4>🎯 核心结论</h4><p>分析完成，统计结果已展示在「📈 分析」标签页。</p></div>';
        }
        // 相关性（如已执行）
        const corr = results['/api/correlation'];
        if (corr && corr.ok && (corr.corr_html || corr.conclusion)) {
          reportHtml += '<div class="report-section"><h4>🔗 相关性分析</h4>';
          if (corr.conclusion && corr.conclusion.length) {
            const cconc = Array.isArray(corr.conclusion) ? corr.conclusion.join('<br/>') : corr.conclusion;
            reportHtml += '<div style="margin-bottom:8px;padding:8px 10px;background:rgba(94,106,210,.08);border-radius:6px;font-size:13px;line-height:1.8;">📌 ' + cconc + '</div>';
            saveConclusionToReport('🔗 相关性分析', cconc, 'AI 分析');
          }
          if (corr.corr_html) reportHtml += '<div style="max-height:240px;overflow:auto;font-size:12px;">' + corr.corr_html + '</div>';
          reportHtml += '</div>';
        }
        reportHtml += '<div class="report-section"><h4>🛠 下一步</h4><ul><li>前往「📊 可视化」标签页生成图表</li><li>前往「📄 报告」标签页导出分析报告</li></ul></div>';
      } else if (type === 'clean') {
        const cl = results['/api/clean'];
        if (cl && cl.ok) {
          reportHtml += '<div class="report-section"><h4>🧹 清洗结果</h4>';
          reportHtml += '<ul><li>原始行数：' + cl.rows_before + '</li><li>清洗后：' + cl.rows_after + '</li>';
          if (cl.diag) {
            reportHtml += '<li>重复行：' + (cl.diag.duplicates || 0) + '</li><li>缺失值：' + (cl.diag.total_missing || 0) + '</li>';
          }
          reportHtml += '</ul></div>';
          if (cl.path) reportHtml += '<div class="report-section"><h4>📁 保存</h4><p>' + esc(cl.path) + '</p></div>';
        } else if (cl && cl.error) {
          reportHtml += '<div class="report-section"><h4>🧹 清洗失败</h4><p style="color:var(--error);">' + esc(cl.error) + '</p></div>';
        } else {
          reportHtml += '<div class="report-section"><h4>🧹 清洗完成</h4><p>清洗结果已展示在「数据清洗」标签页。</p></div>';
        }
      } else if (type === 'correlation') {
        const corr = results['/api/correlation'];
        if (corr && corr.ok && corr.corr_html) {
          reportHtml += '<div class="report-section"><h4>🔗 相关性分析</h4><div style="max-height:280px;overflow:auto;font-size:12px;">' + corr.corr_html + '</div></div>';
        } else {
          reportHtml += '<div class="report-section"><h4>🔗 相关性分析</h4><p>相关性矩阵已展示在「📈 分析」标签页。</p></div>';
        }
      } else if (type === 'group') {
        const grp = results['/api/group'];
        if (grp && grp.ok && grp.html) {
          reportHtml += '<div class="report-section"><h4>🔀 分组统计（' + esc(step.params.group_col || '') + '）</h4><div style="max-height:280px;overflow:auto;font-size:12px;">' + grp.html + '</div></div>';
        } else {
          reportHtml += '<div class="report-section"><h4>🔀 分组统计</h4><p>分组统计结果已展示在「📈 分析」标签页。</p></div>';
        }
      } else if (type === 'chart') {
        reportHtml += '<div class="report-section"><h4>🎯 核心结论</h4><p>图表' + (chartType ? '（' + chartType + '）' : '') + '生成完成，已添加到「本次会话图表」。</p></div>';
      } else if (type === 'stock') {
        reportHtml += '<div class="report-section"><h4>🎯 核心结论</h4><p>股票' + (symbol ? '（' + symbol + '）' : '') + '行情数据获取完成。</p></div>';
      }
      
      reportHtml += '</div>';
      container.innerHTML += reportHtml;
    }
    } catch (e) {
      const progressEl = document.getElementById(progressId);
      if (progressEl) {
        progressEl.innerHTML = '<div style="display:flex;align-items:center;gap:8px;">' +
          '<span>❌</span>' +
          '<span>[' + (i+1) + '/' + totalSteps + '] ' + step.description + ' - 错误</span>' +
          '</div>';
      }
    }
    
    container.scrollTop = container.scrollHeight;
    
    // 步骤间短暂延迟，让用户能看到进度
    if (i < totalSteps - 1) {
      await new Promise(resolve => setTimeout(resolve, 300));
    }
  }
}

// 将 AI 分析的 API 结果渲染到对应标签页，使用户在标签页也能看到统计结果
function renderAIResultToTab(url, data) {
  if (!data || data.error) return;
  try {
    // 描述性统计 → 分析标签页
    if (url.includes('/api/describe') && data.stats_html) {
      const stCard = document.getElementById('an-st-card');
      const st = document.getElementById('an-stats');
      if (stCard) stCard.classList.remove('hide');
      if (st) st.innerHTML = data.stats_html + (data.conclusion && data.conclusion.length ? '<div class="conclusion"><div class="c-title">分析结论</div>' + (Array.isArray(data.conclusion) ? data.conclusion.join('<br/>') : data.conclusion) + '</div>' : '');
    }
    // 相关性 → 分析标签页
    else if (url.includes('/api/correlation') && data.corr_html) {
      const cCard = document.getElementById('an-corr-card');
      const c = document.getElementById('an-corr');
      if (cCard) cCard.classList.remove('hide');
      if (c) c.innerHTML = data.corr_html;
    }
    // 分组统计 → 分析标签页
    else if (url.includes('/api/group')) {
      const gCard = document.getElementById('an-group-card');
      const g = document.getElementById('an-group-result');
      if (gCard) gCard.classList.remove('hide');
      if (g) g.innerHTML = data.html || data.group_html || '';
    }
    // 清洗 → 清洗标签页
    else if (url.includes('/api/clean') && data.diag) {
      const diag = document.getElementById('cl-diag');
      const resCard = document.getElementById('cl-res-card');
      const info = document.getElementById('cl-info');
      if (diag) {
        let h = '<div class="stats"><div class="sc"><div class="sn">' + (data.diag.duplicates||0) + '</div><div class="sl">重复行</div></div>' +
          '<div class="sc"><div class="sn">' + (data.diag.total_missing||0) + '</div><div class="sl">缺失值</div></div>' +
          '<div class="sc"><div class="sn">' + data.rows_before + '</div><div class="sl">原始行数</div></div>' +
          '<div class="sc"><div class="sn">' + data.rows_after + '</div><div class="sl">清洗后</div></div></div>';
        diag.innerHTML = h;
      }
      if (resCard) resCard.classList.remove('hide');
      if (info) info.textContent = '已保存: ' + data.path;
    }
    // 时序 Prophet → 时序标签页
    else if (url.includes('/api/ts/prophet') && data.img) {
      const el = document.getElementById('ts-pr-charts');
      if (el) { el.innerHTML = '<img src="/api/chart/charts/' + data.img + '" style="max-width:100%;border-radius:8px;">'; }
    }
  } catch (e) { console.error('renderAIResultToTab:', e); }
}

async function executeStep(step, container) {
  const { action, params } = step;
  // 兼容：container 可能未传入时，回退到 AI 消息容器
  if (!container) {
    container = document.getElementById('ai-messages');
  }

  // 占位符替换：{{lastFile}} 引用上一步 API 返回的 data.file（自动取数→分析链路）
  try {
    const lastFile = (window._aiLastResult && window._aiLastResult.file) || '';
    if (lastFile && JSON.stringify(step).indexOf('{{lastFile}}') !== -1) {
      step = JSON.parse(JSON.stringify(step).split('{{lastFile}}').join(lastFile));
    }
  } catch (e) { /* 占位符处理失败按原步骤执行 */ }

  switch (action) {
    case 'switch_tab':
      if (params.tab) {
        const btn = document.querySelector('.nbtn[data-t="' + params.tab + '"]');
        if (btn) btn.click();
      }
      updateAIContextBar();
      return { ok: true };
    
    case 'do_operation':
      // AI 直接操作：填写表单、选择下拉、点击按钮
      try {
        if (params.fill) {
          for (const [selector, value] of Object.entries(params.fill)) {
            const el = document.querySelector(selector) || document.getElementById(selector);
            if (el) {
              el.value = value;
              el.dispatchEvent(new Event('input', { bubbles: true }));
              el.dispatchEvent(new Event('change', { bubbles: true }));
            }
          }
        }
        if (params.select) {
          for (const [selector, value] of Object.entries(params.select)) {
            const el = document.querySelector(selector) || document.getElementById(selector);
            if (el) {
              el.value = value;
              el.dispatchEvent(new Event('change', { bubbles: true }));
            }
          }
        }
        if (params.click) {
          const btn = document.querySelector(params.click) || document.getElementById(params.click);
          if (btn) btn.click();
        }
        return { ok: true, message: '操作已执行' };
      } catch(e) {
        return { ok: false, error: e.message };
      }
    
    case 'get_page_state':
      // 获取当前页面状态（供 AI 决策）
      try {
        const activeTab = document.querySelector('.nbtn.on');
        return {
          ok: true,
          data: {
            tab: activeTab ? activeTab.dataset.t : '',
            file: getCurrentFile() || '',
            url: window.location.href,
          }
        };
      } catch(e) {
        return { ok: false, error: e.message };
      }
    
    case 'call_api':
      try {
        const apiUrl = params.url || '';
        // 分析类接口依赖有效的 file，空文件会触发后端 500，先补齐或拦截
        if (params.method === 'POST' && (!apiUrl.includes('/api/run_code')) &&
            (apiUrl.includes('/api/describe') || apiUrl.includes('/api/correlation') || apiUrl.includes('/api/group'))) {
          const f = (params.body && params.body.file) || getCurrentFile() || '';
          if (!f) {
            return { ok: false, error: '未选择数据文件，无法执行该分析，请先在数据分析页加载文件' };
          }
          params.body = Object.assign({}, params.body, { file: f });
        }
        const opts = { method: params.method || 'GET' };
        if (params.body && params.method === 'POST') {
          opts.headers = { 'Content-Type': 'application/json' };
          opts.body = JSON.stringify(params.body);
        }
        const r = await fetch(apiUrl, opts);
        const data = await r.json();
        // 缓存结果，供 show_structured_report 生成真实报告
        window._aiStepResults = window._aiStepResults || {};
        window._aiStepResults[params.url] = data;
        window._aiLastResult = data;
        // 将分析结果同步渲染到对应标签页（AI 分析结果在标签页也可见）
        renderAIResultToTab(params.url, data);
        // 自动收集 API 返回的图表/表格到"本次会话图表"
        if (typeof saveChartToReport === 'function') {
          const url = params.url || '';
          if (data.stats_html) {
            saveChartToReport('', '描述性统计', (data.conclusion && data.conclusion.length) ? (Array.isArray(data.conclusion) ? data.conclusion.join(' / ') : data.conclusion) : '', 'AI 分析', data.stats_html);
          } else if (data.corr_html) {
            saveChartToReport('', '相关性分析', '', 'AI 分析', data.corr_html);
          } else if (data.html || data.group_html) {
            saveChartToReport('', '分组统计', '', 'AI 分析', data.html || data.group_html);
          } else if (data.chart) {
            saveChartToReport('', '分析图表', '', 'AI 分析', '<img src="' + data.chart + '" style="max-width:100%;">');
          }
        }
        return { ok: true, data: data };
      } catch (e) {
        return { ok: false, error: e.message };
      }
    
    case 'show_message':
      return { ok: true, content: params.content || '' };
    
    case 'run_code':
      try {
        const r = await fetch('/api/assistant/run_code', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            code: params.code,
            language: params.language || 'python',
            context: params.context || {}
          })
        });
        const data = await r.json();
        return { ok: data.ok, data: data };
      } catch (e) {
        return { ok: false, error: e.message };
      }
    
    case 'show_code_result':
      // 显示代码执行结果（图片/HTML/文本）
      return { ok: true, content: params.content || '', output_type: params.output_type || 'text' };
    
    case 'save_snapshot':
      // 保存当前分析的数据快照（供后续对比）
      try {
        const file = params.file || window._aiLastResult?.file || '';
        const results = window._aiStepResults || {};
        const desc = results['/api/describe'];
        if (desc && desc.ok) {
          const nums = {};
          // 从 stats_html 提取关键数值
          const matches = desc.stats_html?.match(/<td[^>]*>([\d.]+)<\/td>/g) || [];
          const cols = desc.stats_html?.match(/<th[^>]*>([^<]+)<\/th>/g) || [];
          if (matches.length > 0 && cols.length > 0) {
            // 简化：保存原始 stats_html 供后续对比
            nums._html = desc.stats_html.substring(0, 2000);
            nums._ts = Date.now();
          }
          if (!window._aiSnapshots) window._aiSnapshots = [];
          window._aiSnapshots.push({ file, nums, time: new Date().toLocaleTimeString() });
          if (window._aiSnapshots.length > 5) window._aiSnapshots.shift();
        }
      } catch(e) { /* 快照失败忽略 */ }
      return { ok: true };
    
    case 'ht_result':
      // 解读假设检验结果
      try {
        const results = window._aiStepResults || {};
        const ht = results['/api/ht/run'];
        if (ht && ht.statistic !== undefined) {
          const pValue = ht.p_value;
          const sig = pValue < 0.05 ? '显著' : '不显著';
          const sigIcon = pValue < 0.05 ? '✅' : '❌';
          const testName = step.params.test_name || '假设检验';
          let msg = `<b>${sigIcon} ${testName}结果</b><br/>`;
          msg += `p值 = ${pValue}（${sig}）<br/>`;
          msg += pValue < 0.05
            ? `📌 结论：两组数据差异具有统计学意义（p < 0.05）`
            : `📌 结论：两组数据差异不具有统计学意义（p ≥ 0.05）`;
          if (ht.sample_stats) {
            msg += '<br/><small>';
            for (const [k, v] of Object.entries(ht.sample_stats)) {
              msg += `${k}: ${v} `;
            }
            msg += '</small>';
          }
          container.innerHTML += '<div class="ai-msg assistant" style="font-size:12px;line-height:1.8;">' + msg + '</div>';
        }
      } catch(e) {}
      return { ok: true };
    
    case 'compare_snapshots':
      // 对比最近两个数据快照
      try {
        const snaps = window._aiSnapshots || [];
        if (snaps.length >= 2) {
          const curr = snaps[snaps.length - 1];
          const prev = snaps[snaps.length - 2];
          // 简单 HTML 文本差异
          const prevHtml = prev.nums?._html || '';
          const currHtml = curr.nums?._html || '';
          let msg = `📊 数据快照对比（${prev.time} → ${curr.time}）：\n`;
          if (prevHtml === currHtml) {
            msg += '数据未发生变化（可能是同一文件重复分析）';
          } else {
            msg += `文件: ${curr.file}\n数据已更新，建议重新执行分析获取详细对比`;
          }
          container.innerHTML += '<div class="ai-msg assistant" style="border-left:3px solid var(--warning,#fbbf24);font-size:12px;line-height:1.8;white-space:pre-wrap;">' + msg + '</div>';
        } else {
          container.innerHTML += '<div class="ai-msg assistant" style="font-size:12px;">暂无足够的历史数据进行对比（需先执行至少 2 次分析）</div>';
        }
      } catch(e) { /* 对比失败忽略 */ }
      return { ok: true };

    case 'autonomous_analysis':
      // AI 自主分析：调用后端增强分析引擎
      try {
        // 生成 session id 用于进度追踪
        const sessionId = 'ai_' + Date.now().toString(36);
        // 记录原始问题，供断点恢复使用
        window._aiAutonomousMessages = window._aiAutonomousMessages || {};
        window._aiAutonomousMessages[sessionId] = params.message || '';
        
        // 显示进度 UI
        const progressDiv = document.createElement('div');
        progressDiv.id = 'ai-progress-' + sessionId;
        progressDiv.className = 'ai-msg assistant';
        progressDiv.style.borderLeft = '3px solid var(--primary,#45B7D1)';
        progressDiv.style.padding = '8px 12px';
        progressDiv.innerHTML = `
          <div style="display:flex;align-items:center;gap:8px;margin-bottom:6px;">
            <span class="spin" style="width:12px;height:12px;border-width:2px;"></span>
            <span style="font-size:12px;" id="ai-progress-msg-${sessionId}>准备中...</span>
          </div>
          <div style="height:4px;background:rgba(255,255,255,.08);border-radius:2px;overflow:hidden;">
            <div id="ai-progress-bar-${sessionId}" style="height:100%;width:0%;background:linear-gradient(90deg,#45B7D1,#22d3ee);border-radius:2px;transition:width .3s;"></div>
          </div>
          <div id="ai-progress-steps-${sessionId}" style="margin-top:6px;font-size:11px;color:var(--ink-subtle);"></div>
        `;
        container.appendChild(progressDiv);
        container.scrollTop = container.scrollHeight;
        
        const r = await fetch('/api/assistant/autonomous-analysis', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ 
            file: params.file || getCurrentFile() || '',
            llm_config: loadLLMConfig(),
            message: params.message || '',
            session_id: sessionId
          })
        });
        let data;
        try {
          data = await r.json();
        } catch (e) {
          console.error('[AI Analysis] JSON parse error:', e);
          progressDiv.remove();
          return { ok: false, error: '响应解析失败: ' + (e.message || '非JSON响应') };
        }
        
        // 清除进度 UI
        progressDiv.remove();
        
        window._aiAutonomousResult = data;
        if (data.ok && data.report) {
          // 添加保存和取消按钮
          const actionsDiv = document.createElement('div');
          actionsDiv.style = 'display:flex;gap:8px;margin-top:8px;flex-wrap:wrap;';
          actionsDiv.innerHTML = `
            <button class="btn btn-p btn-sm" onclick="saveAIReport('${sessionId}')" style="font-size:11px;padding:4px 10px;">📄 保存到报告</button>
            <button class="btn btn-s btn-sm" onclick="showAIHistory()" style="font-size:11px;padding:4px 10px;">📚 历史记录</button>
            <button class="btn btn-s btn-sm" onclick="copyAIReport('${sessionId}')" style="font-size:11px;padding:4px 10px;">📋 复制内容</button>
          `;
          
          container.innerHTML += '<div class="ai-msg assistant md" style="border-left:3px solid #22d3ee;padding:12px 16px;background:rgba(34,211,238,.03);"><b>AI 深度分析</b><div style="margin-top:8px;font-size:13px;line-height:1.8;" id="ai-report-content-' + sessionId + '">' + formatMD(data.report) + '</div></div>';
          // 执行轨迹（LangSmith 式观测）：计划 + 工具调用状态/耗时/缓存
          const traceHtml = renderAgentTrace(data);
          if (traceHtml) { container.innerHTML += traceHtml; }
          container.appendChild(actionsDiv);
          container.scrollTop = container.scrollHeight;
          
          // 保存 report 内容到全局变量
          window._aiReportContent = window._aiReportContent || {};
          window._aiReportContent[sessionId] = data.report;
          
          return { ok: true, data: data };
        } else if (data.cancelled) {
          // 已取消：断点已保存，提供恢复入口（LangGraph checkpointing）
          container.innerHTML += '<div class="ai-msg assistant" style="font-size:12px;">⏹ ' + esc(data.report || '分析已取消') +
            (data.resumable ? ' <button class="btn btn-p btn-sm" style="font-size:11px;padding:3px 10px;margin-left:8px;" onclick="resumeAgentAnalysis(\'' + sessionId + '\')">▶ 从断点恢复</button>' : '') + '</div>';
          return { ok: false, error: 'cancelled', data: data };
        } else if (data.error) {
          console.error('[AI Analysis] backend error:', data.error);
          container.innerHTML += '<div class="ai-msg assistant" style="border-color:var(--error);font-size:12px;">❌ ' + esc(data.error) + '</div>';
          return { ok: false, error: data.error };
        } else {
          console.error('[AI Analysis] empty response:', data);
          return { ok: false, error: 'AI 分析未返回结果，请检查后端日志或 LLM 配置' };
        }
      } catch (e) {
        console.error('[AI Analysis] fetch error:', e);
        return { ok: false, error: 'AI 分析请求失败: ' + (e.message || e) };
      }
    
    case 'show_structured_report':
      try {
        const r = await fetch('/api/assistant/structured-report', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            type: params.type || 'analyze',
            file: params.file || getCurrentFile() || '',
            chart_type: params.chart_type || '',
          })
        });
        const data = await r.json();
        if (data.ok && data.html) {
          container.innerHTML += '<div class="ai-msg assistant"><b>📊 分析报告</b><div style="margin-top:8px;">' + data.html + '</div></div>';
        } else if (data.error) {
          container.innerHTML += '<div class="ai-msg assistant" style="border-color:var(--error);font-size:12px;">❌ ' + esc(data.error) + '</div>';
        }
        return { ok: data.ok, data: data };
      } catch (e) {
        return { ok: false, error: e.message };
      }
    
    case 'fill_form':
      if (params.selector && params.value) {
        const el = document.querySelector(params.selector);
        if (el) el.value = params.value;
      }
      return { ok: true };
    
    case 'click_button':
      if (params.selector) {
        const el = document.querySelector(params.selector);
        if (el) el.click();
      }
      return { ok: true };
    
    default:
      return { ok: true };
  }
}

// ===== AI Report Helpers =====

// 执行轨迹渲染（LangSmith 式观测）：执行计划 + 每次工具调用的状态/耗时/缓存
function renderAgentTrace(data) {
  if (!data) return '';
  const plan = data.plan, calls = data.calls || [], trace = data.trace;
  if (!plan && !calls.length) return '';

  let planHtml = '';
  if (Array.isArray(plan) && plan.length) {
    planHtml = '<div style="margin:6px 0 2px;font-size:11px;color:var(--ink-subtle);">📋 执行计划（Plan-and-Execute）</div><ol style="margin:2px 0 8px 18px;font-size:11px;line-height:1.7;color:var(--ink-muted);">'
      + plan.map(s => '<li>' + esc(String(s)) + '</li>').join('') + '</ol>';
  }

  let callsHtml = calls.map(c => {
    const isErr = c.status === 'error';
    const icon = isErr ? '❌' : (c.cached ? '⚡' : '✅');
    const dur = c.duration_ms != null ? (c.duration_ms >= 1000 ? (c.duration_ms / 1000).toFixed(1) + 's' : c.duration_ms + 'ms') : '';
    const meta = [c.cached ? '缓存命中' : '', dur].filter(Boolean).join(' · ');
    return '<div style="display:flex;align-items:baseline;gap:6px;font-size:11px;line-height:1.8;">'
      + '<span>' + icon + '</span>'
      + '<code style="font-size:10px;">' + esc(c.tool || '') + '</code>'
      + (meta ? '<span style="color:var(--ink-tertiary);font-size:10px;">' + esc(meta) + '</span>' : '')
      + '</div>';
  }).join('');

  let summaryHtml = '';
  if (trace) {
    const parts = [];
    if (trace.active_skill) parts.push(`Skill：${trace.active_skill}`);
    parts.push(`工具调用 ${trace.total_tools ?? calls.length} 次`);
    if (trace.errors) parts.push(`失败 ${trace.errors} 次`);
    if (trace.cache_hits) parts.push(`缓存 ${trace.cache_hits} 次`);
    if (trace.tool_duration_ms != null && trace.tool_duration_ms > 0) {
      parts.push(`工具耗时 ${trace.tool_duration_ms >= 1000 ? (trace.tool_duration_ms / 1000).toFixed(1) + 's' : trace.tool_duration_ms + 'ms'}`);
    }
    // Token 消耗（成本可观测：帮助用户感知免费额度使用）
    const tot = trace.total_tokens || 0;
    if (tot) {
      const inTok = trace.prompt_tokens || 0, outTok = trace.completion_tokens || 0;
      parts.push(`Token ${tot}（入 ${inTok} / 出 ${outTok}）`);
    }
    summaryHtml = '<div style="font-size:10px;color:var(--ink-tertiary);margin-top:6px;">' + esc(parts.join(' · ')) + '</div>';
  }

  return '<details class="ai-msg assistant" style="border-left:3px solid var(--ink-subtle,#64748b);padding:8px 14px;font-size:12px;">'
    + '<summary style="cursor:pointer;font-size:11px;color:var(--ink-subtle);">🔍 执行轨迹（点击展开）</summary>'
    + planHtml
    + '<div style="margin:6px 0 2px;font-size:11px;color:var(--ink-subtle);">🛠 工具调用</div>'
    + '<div style="margin:2px 0 0;">' + (callsHtml || '<span style="font-size:11px;color:var(--ink-subtle);">无</span>') + '</div>'
    + summaryHtml
    + '</details>';
}

// 断点恢复（LangGraph checkpointing）：取消/中断后从 checkpoint 继续分析
async function resumeAgentAnalysis(sessionId) {
  const container = document.getElementById('ai-messages');
  if (!container) { showToast('未找到消息容器', 'error'); return; }
  const message = (window._aiAutonomousMessages && window._aiAutonomousMessages[sessionId]) || '';
  if (!message) { showToast('未找到原始问题，无法恢复', 'warning'); return; }

  container.innerHTML += '<div class="ai-msg assistant" style="border-left:3px solid #22d3ee;font-size:12px;">⏳ 正在从断点恢复分析...</div>';
  container.scrollTop = container.scrollHeight;

  try {
    const r = await fetch('/api/assistant/autonomous-analysis', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        file: getCurrentFile() || '',
        llm_config: loadLLMConfig(),
        message: message,
        session_id: sessionId,
        resume: true
      })
    });
    const data = await r.json();
    if (data.ok && data.report) {
      container.innerHTML += '<div class="ai-msg assistant md" style="border-left:3px solid #22d3ee;padding:12px 16px;background:rgba(34,211,238,.03);"><b>AI 深度分析（已恢复）</b><div style="margin-top:8px;font-size:13px;line-height:1.8;">' + formatMD(data.report) + '</div></div>';
      const traceHtml = renderAgentTrace(data);
      if (traceHtml) { container.innerHTML += traceHtml; }
      window._aiReportContent = window._aiReportContent || {};
      window._aiReportContent[sessionId] = data.report;
      showToast('已从断点恢复并完成分析', 'success');
    } else {
      container.innerHTML += '<div class="ai-msg assistant" style="border-color:var(--error);font-size:12px;">❌ ' + esc(data.error || data.report || '恢复失败') + '</div>';
    }
    container.scrollTop = container.scrollHeight;
  } catch (e) {
    container.innerHTML += '<div class="ai-msg assistant" style="border-color:var(--error);font-size:12px;">❌ 恢复请求失败: ' + esc(e.message || e) + '</div>';
  }
}

async function saveAIReport(sessionId) {
  const content = window._aiReportContent && window._aiReportContent[sessionId];
  if (!content) { showToast('没有可保存的内容', 'warning'); return; }
  
  const file = getCurrentFile() || 'analysis';
  try {
    const r = await fetch('/api/assistant/save-to-report', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ report: content, file: file, title: 'AI 深度分析报告' })
    });
    const data = await r.json();
    if (data.ok) {
      showToast(`已保存: ${data.file}`, 'success');
    } else {
      showToast('保存失败: ' + (data.error || ''), 'error');
    }
  } catch (e) {
    showToast('保存失败: ' + e.message, 'error');
  }
}

async function showAIHistory() {
  try {
    const r = await fetch('/api/assistant/history');
    const data = await r.json();
    if (!data.ok) { showToast('获取历史失败', 'error'); return; }
    
    const container = document.getElementById('ai-messages');
    if (!container) return;
    
    let html = '<div class="ai-msg assistant"><b>📚 分析历史记录</b><div style="margin-top:8px;font-size:12px;">';
    if (data.history && data.history.length > 0) {
      data.history.forEach(h => {
        const time = new Date(h.time * 1000).toLocaleString('zh-CN');
        html += `<div style="padding:6px 0;border-bottom:1px solid rgba(255,255,255,.05);">📄 ${h.file} <span style="color:var(--ink-subtle);">(${time})</span></div>`;
      });
    } else {
      html += '<div style="color:var(--ink-subtle);">暂无历史记录</div>';
    }
    html += '</div></div>';
    container.innerHTML += html;
    container.scrollTop = container.scrollHeight;
  } catch (e) {
    showToast('获取历史失败: ' + e.message, 'error');
  }
}

function copyAIReport(sessionId) {
  const content = window._aiReportContent && window._aiReportContent[sessionId];
  if (!content) { showToast('没有可复制的内容', 'warning'); return; }
  
  navigator.clipboard.writeText(content).then(() => {
    showToast('已复制到剪贴板', 'success');
  }).catch(() => {
    // fallback
    const ta = document.createElement('textarea');
    ta.value = content;
    document.body.appendChild(ta);
    ta.select();
    document.execCommand('copy');
    document.body.removeChild(ta);
    showToast('已复制到剪贴板', 'success');
  });
}

async function clearAIChat() {
  const container = document.getElementById('ai-messages');
  if (container) container.innerHTML = '';
  // 多会话：🗑 只清当前会话（不动其它会话）；不带 conversation_id 时后端仍全量清空
  const cid = _currentConversationId();
  await fetch('/api/assistant/clear', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ conversation_id: cid })
  });
  // 同时清空 Agent 模式 localStorage 记录
  if (typeof frontendAgent !== 'undefined' && frontendAgent.clearMessages) {
    frontendAgent.clearMessages();
  }
  if (typeof loadAiSidebar === 'function') loadAiSidebar();
}

// 注：原「删除单条最近对话」的前端入口随右栏改造为「会话列表」而移除
// （该入口唯一的挂载点就是被替换掉的列表）。后端 /api/assistant/delete-message
// 与 delete_chat_message() 仍在，供 API / 脚本按需调用。

document.addEventListener('keydown', function(e) {
  if (e.key === 'Enter' && e.target && e.target.id === 'ai-input') {
    e.preventDefault();
    sendAIMessage();
  }
});

// ===== LLM Settings =====

// 默认使用日日新 GLM-5.2（免费公测，API Key 存于服务端加密凭据库 .secure/credentials.enc，无需前端填写）
// 2026-09-17：sensenova 新 key 实测可用（glm-5.2 / deepseek-v4-flash / deepseek-v4-pro /
// sensenova-6.8-flash-lite 均 200），原先因 key 失效临时指向的 zhipu/glm-4.7-flash 已从模型库移除。
const DEFAULT_LLM_CONFIG = { provider: 'sensenova', apiKey: '', apiUrl: 'https://token.sensenova.cn/v1/chat/completions', model: 'glm-5.2' };

const PROVIDER_PRESETS = {
  // 国产
  sensenova:  { url: 'https://token.sensenova.cn/v1/chat/completions' },
  deepseek:   { url: 'https://api.deepseek.com/v1/chat/completions' },
  zhipu:      { url: 'https://open.bigmodel.cn/api/paas/v4/chat/completions' },
  dashscope:  { url: 'https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions' },
  baidu:      { url: 'https://qianfan.baidubce.com/v2/chat/completions' },
  doubao:     { url: 'https://ark.cn-beijing.volces.com/api/v3/chat/completions' },
  moonshot:   { url: 'https://api.moonshot.cn/v1/chat/completions' },
  yi:         { url: 'https://api.lingyiwanwu.com/v1/chat/completions' },
  hunyuan:    { url: 'https://api.hunyuan.tencent.com/v1/chat/completions' },
  spark:      { url: 'https://spark-api-open.xf-yun.com/v1/chat/completions' },
  // 海外
  openai:     { url: 'https://api.openai.com/v1/chat/completions' },
  anthropic:  { url: 'https://api.anthropic.com/v1/messages' },         // 原生非 OpenAI 协议（后端自动适配）
  google:     { url: 'https://generativelanguage.googleapis.com/v1beta/openai/chat/completions' },
  xai:        { url: 'https://api.x.ai/v1/chat/completions' },
  mistral:    { url: 'https://api.mistral.ai/v1/chat/completions' },
  azure:      { url: '' },                                              // Azure 必须填自定义 URL
  groq:       { url: 'https://api.groq.com/openai/v1/chat/completions' },
  ollama:     { url: 'http://localhost:11434/v1/chat/completions' },
  custom:     { url: '' }
};

// 模型清单：标注价格区间 + 速度；用户在前端「⚙️ 模型设置」下拉里直接选
const PROVIDER_MODELS = {
  // ---- 免费 / 公测 ----
  // 清单以 2026-09-17 实测为准：只保留端点确认可调用的模型。
  // 已剔除（/models 列出但调用返回 404 model not found）：sensenova-u1-fast、sensenova-u1.5-lite、sensenova-6.7-flash-lite
  sensenova: [
    { id: 'glm-5.2', name: 'GLM-5.2 (1M 上下文)', price: '免费公测', speed: '快' },
    { id: 'deepseek-v4-flash', name: 'DeepSeek V4 Flash', price: '免费公测', speed: '快' },
    { id: 'deepseek-v4-pro', name: 'DeepSeek V4 Pro', price: '免费公测', speed: '中' },
    { id: 'sensenova-6.8-flash-lite', name: 'SenseNova 6.8 Flash Lite (多模态)', price: '免费公测', speed: '中' },
    { id: 'kimi-k3', name: 'Kimi K3 (当前账号 429 限速)', price: '免费公测', speed: '受限' },
  ],
  zhipu: [
    { id: 'glm-4-flash', name: 'GLM-4 Flash (免费)', price: '免费额度', speed: '快' },
  ],
  groq: [
    { id: 'llama-3.1-70b-versatile', name: 'Llama-3.1-70B (免费)', price: '完全免费', speed: '极快' },
    { id: 'llama-3.1-8b-instant', name: 'Llama-3.1-8B (免费)', price: '完全免费', speed: '极快' },
    { id: 'llama-3.3-70b-versatile', name: 'Llama-3.3-70B (免费)', price: '完全免费', speed: '极快' },
    { id: 'mixtral-8x7b-32768', name: 'Mixtral-8x7B (免费)', price: '完全免费', speed: '极快' },
    { id: 'gemma2-9b-it', name: 'Gemma2-9B (免费)', price: '完全免费', speed: '极快' },
  ],
  google: [
    { id: 'gemini-2.0-flash', name: 'Gemini-2.0-Flash (免费层)', price: '免费层', speed: '快' },
    { id: 'gemini-1.5-pro', name: 'Gemini-1.5-Pro', price: '免费层', speed: '中' },
  ],
  // ---- 国内主流（部分需付费 Key） ----
  deepseek: [
    { id: 'deepseek-v4-flash', name: 'DeepSeek-V4-Flash (便宜)', price: '¥0.001/1K', speed: '快' },
    { id: 'deepseek-v3', name: 'DeepSeek-V3', price: '¥0.002/1K', speed: '中' },
    { id: 'deepseek-r1', name: 'DeepSeek-R1 (推理)', price: '¥0.004/1K', speed: '中' },
  ],
  dashscope: [
    { id: 'qwen-turbo', name: '通义千问-Turbo', price: '¥0.001/1K', speed: '快' },
    { id: 'qwen-plus', name: '通义千问-Plus', price: '¥0.004/1K', speed: '快' },
    { id: 'qwen-max', name: '通义千问-Max', price: '¥0.02/1K', speed: '中' },
  ],
  baidu: [
    { id: 'ernie-4.5-8k', name: '文心一言 4.5 (8K)', price: '付费', speed: '快' },
    { id: 'ernie-3.5-8k', name: '文心一言 3.5 (8K)', price: '便宜', speed: '快' },
  ],
  doubao: [
    { id: 'doubao-pro-32k', name: '豆包-Pro 32K', price: '便宜', speed: '快' },
    { id: 'doubao-lite-32k', name: '豆包-Lite 32K', price: '超便宜', speed: '极快' },
  ],
  moonshot: [
    { id: 'moonshot-v1-8k', name: 'Kimi v1 (8K)', price: '付费', speed: '快' },
    { id: 'moonshot-v1-128k', name: 'Kimi v1 (128K 长文)', price: '付费', speed: '中' },
  ],
  yi: [
    { id: 'yi-large', name: 'Yi-Large', price: '付费', speed: '快' },
    { id: 'yi-medium', name: 'Yi-Medium', price: '便宜', speed: '快' },
  ],
  hunyuan: [
    { id: 'hunyuan-pro', name: '混元-Pro', price: '便宜', speed: '快' },
    { id: 'hunyuan-standard', name: '混元-Standard', price: '超便宜', speed: '极快' },
  ],
  spark: [
    { id: 'generalv3.5', name: '星火 v3.5', price: '付费', speed: '快' },
  ],
  // ---- 海外 ----
  openai: [
    { id: 'gpt-4o-mini', name: 'GPT-4o-mini (便宜)', price: '$0.15/1M', speed: '快' },
    { id: 'gpt-4o', name: 'GPT-4o', price: '$2.5/1M', speed: '中' },
    { id: 'gpt-4-turbo', name: 'GPT-4-Turbo', price: '$10/1M', speed: '中' },
  ],
  anthropic: [
    { id: 'claude-3-5-sonnet-latest', name: 'Claude 3.5 Sonnet', price: '$3/1M', speed: '中' },
    { id: 'claude-3-5-haiku-latest', name: 'Claude 3.5 Haiku (便宜)', price: '$0.8/1M', speed: '快' },
    { id: 'claude-3-opus-latest', name: 'Claude 3 Opus (强大)', price: '$15/1M', speed: '慢' },
  ],
  xai: [
    { id: 'grok-2-latest', name: 'Grok-2', price: '$2/1M', speed: '快' },
  ],
  mistral: [
    { id: 'mistral-large-latest', name: 'Mistral Large', price: '$2/1M', speed: '中' },
    { id: 'mistral-small-latest', name: 'Mistral Small (便宜)', price: '$0.2/1M', speed: '快' },
  ],
  azure: [
    { id: 'gpt-4o', name: 'GPT-4o (Azure 部署)', price: '按部署定价', speed: '中' },
  ],
  // ---- 本地 ----
  ollama: [
    { id: 'qwen2.5:7b', name: 'Qwen2.5-7B (本地)', price: '零成本', speed: '取决于硬件' },
    { id: 'qwen2.5:14b', name: 'Qwen2.5-14B (本地)', price: '零成本', speed: '取决于硬件' },
    { id: 'qwen2.5:72b', name: 'Qwen2.5-72B (本地)', price: '零成本', speed: '取决于硬件' },
    { id: 'llama-3-8b', name: 'Llama-3-8B (本地)', price: '零成本', speed: '取决于硬件' },
    { id: 'deepseek-r1:7b', name: 'DeepSeek-R1-7B (本地)', price: '零成本', speed: '取决于硬件' },
  ],
  custom: [],
};

function onProviderChange() {
  const provider = document.getElementById('llm-provider').value;
  const preset = PROVIDER_PRESETS[provider];
  const urlRow = document.getElementById('custom-url-row');
  const modelSelect = document.getElementById('llm-model');
  
  if (provider === 'custom') {
    urlRow.style.display = 'block';
    modelSelect.innerHTML = '<option value="">输入模型名称...</option>';
  } else {
    urlRow.style.display = 'none';
    if (preset) {
      document.getElementById('llm-api-url').value = preset.url;
    }
    
    // 填充模型列表
    const models = PROVIDER_MODELS[provider] || [];
    if (models.length > 0) {
      modelSelect.innerHTML = models.map(m => 
        '<option value="' + m.id + '">' + m.name + ' — ' + m.price + ' — ' + m.speed + '</option>'
      ).join('');
    } else {
      modelSelect.innerHTML = '<option value="">选择模型...</option>';
    }
  }
}

// ===== 多Agent协作 · 各阶段分模型 =====
// localStorage 键：llmPipelineConfig = {_v: 版本号, enabled: bool, stages: {阶段名: {provider, model}}}

// 多Agent协作 · 各阶段分模型 provider 中文标签
const PROVIDER_LABELS = {
  sensenova: '商汤日日新', deepseek: 'DeepSeek', zhipu: '智谱GLM', dashscope: '通义千问',
  baidu: '文心', doubao: '豆包', moonshot: 'Kimi', yi: 'Yi', hunyuan: '混元', spark: '星火',
  openai: 'OpenAI', anthropic: 'Claude', google: 'Gemini', xai: 'Grok', mistral: 'Mistral',
  azure: 'Azure', groq: 'Groq', ollama: 'Ollama', custom: '自定义',
};

const PIPE_STAGES = [
  // 默认值必须是「当前确有可用凭据」的 provider —— 否则用户勾选多Agent协作但不逐项改配置时，
  // 部分阶段会直接失败。2026-09-17：sensenova 凭据恢复，四阶段默认 sensenova/glm-5.2；
  // 2026-09-18：按用户指定，量化建模师默认 deepseek-v4-pro（同账号实测可用，深度测算更强）；
  // 2026-09-18 晚：阶段按需命中（只跑任务命中的阶段）+ 命中阶段并行、各用不同模型
  // （市场 glm-5.2 / 量化 sensenova-6.8-flash-lite / 风控 deepseek-v4-flash）。
  // 本列表是"可配置阶段"全集（不论本次是否命中），与后端 STAGE_DEFS + 终稿合成对齐。
  // 量化阶段原为 deepseek-v4-pro，实测持续 429（inference exceeds tpm/rpm limit）故替换。
  { key: 'market', name: '市场分析师', defProv: 'sensenova', defModel: 'glm-5.2' },
  { key: 'quant',  name: '量化建模师', defProv: 'sensenova', defModel: 'sensenova-6.8-flash-lite' },
  { key: 'risk',   name: '风控合规官', defProv: 'sensenova', defModel: 'deepseek-v4-flash' },
  { key: 'final',  name: '终稿合成',   defProv: 'sensenova', defModel: 'glm-5.2' },
];

function _pipeProviderOptions() {
  return Object.keys(PROVIDER_PRESETS).map(p =>
    '<option value="' + p + '">' + (PROVIDER_LABELS && PROVIDER_LABELS[p] ? PROVIDER_LABELS[p] : p) + '</option>'
  ).join('');
}

function _fillPipeProvider(provSel, modelSel, prov, model) {
  provSel.innerHTML = '<option value="">（沿用全局）</option>' + _pipeProviderOptions();
  if (prov) provSel.value = prov;
  onPipeProviderChangeFor(modelSel, prov, model);
}

function onPipeProviderChangeFor(modelSel, prov, model) {
  modelSel.innerHTML = '';
  if (!prov) { modelSel.innerHTML = '<option value="">自动</option>'; return; }
  const models = PROVIDER_MODELS[prov] || [];
  if (models.length === 0) {
    modelSel.innerHTML = '<option value="">选择模型...</option>';
    return;
  }
  modelSel.innerHTML = models.map(m =>
    '<option value="' + m.id + '">' + m.name + ' — ' + m.price + '</option>'
  ).join('');
  if (model && models.some(m => m.id === model)) modelSel.value = model;
}

function onPipeProviderChange(kind) {
  const stage = PIPE_STAGES.find(s => s.key === kind);
  if (!stage) return;
  const provSel = document.getElementById('pipe-' + kind + '-provider');
  const modelSel = document.getElementById('pipe-' + kind + '-model');
  onPipeProviderChangeFor(modelSel, provSel ? provSel.value : '', null);
}

function onPipelineEnableChange() {
  const on = document.getElementById('pipeline-enable').checked;
  document.getElementById('ai-pipe-body').style.display = on ? 'block' : 'none';
  saveLLMPipelineConfig();
  if (on && _isPipelineAuto()) refreshAutoPipelineSummary();
}

// 自动选择大模型：勾选后隐藏手动阶段配置，改为展示后端自动分配结果
function _isPipelineAuto() {
  const el = document.getElementById('pipeline-auto');
  return !!(el && el.checked);
}

function onPipelineAutoChange() {
  const auto = _isPipelineAuto();
  const manual = document.getElementById('ai-pipe-manual');
  if (manual) manual.style.display = auto ? 'none' : 'block';
  saveLLMPipelineConfig();
  const box = document.getElementById('ai-pipe-auto-summary');
  if (auto) { refreshAutoPipelineSummary(); }
  else if (box) { box.textContent = ''; }
}

// 拉取后端「自动选择大模型」结果并展示。
// 零额度：后端仅做凭据探测 + 轮换分配，不发起任何模型请求。
function refreshAutoPipelineSummary() {
  const box = document.getElementById('ai-pipe-auto-summary');
  if (!box) return;
  box.textContent = '正在检测可用大模型...';
  const cfg = (typeof loadLLMConfig === 'function' ? loadLLMConfig() : null) || {};
  const qs = '?provider=' + encodeURIComponent(cfg.provider || '') +
             '&model=' + encodeURIComponent(cfg.model || '');
  fetch('/api/assistant/pipeline-auto-models' + qs, { method: 'GET' })
    .then(function (r) { return r.json(); })
    .then(function (d) {
      if (!d || !d.ok) { box.textContent = '⚠️ 自动检测失败，将沿用上方全局模型。'; return; }
      const avail = d.available || [];
      const stages = d.stages || {};
      const names = d.stage_order || [];
      const pinned = d.pinned || {};
      const rows = names.map(function (n) {
        const s = stages[n];
        if (!s) return '';
        const label = PROVIDER_LABELS[s.provider] || s.provider;
        const mark = pinned[n] ? ' 📌' : '';
        return '<div class="ai-pipe-row"><label>' + esc(n) + mark + '</label>' +
               '<span>' + esc(label) + ' · ' + esc(s.model) + '</span></div>';
      }).join('');
      const head = avail.length
        ? '🤖 已自动选择：检测到 ' + avail.length + ' 个可用大模型，按阶段轮换分配（降低单一模型 429 风险）。'
        : '⚠️ 未检测到有凭据的云端大模型，将沿用上方全局模型。';
      const availList = avail.map(function (p) {
        return esc(PROVIDER_LABELS[p.provider] || p.provider) + '/' + esc(p.model);
      }).join('、');
      const hasPinned = names.some(function (n) { return pinned[n]; });
      const legend = hasPinned
        ? '<br>📌 = 该阶段模型已按您的指定固定（优先于轮换分配）' : '';
      box.innerHTML = head + (availList ? '<br>可用：' + availList : '') +
                      (rows ? '<div style="margin-top:6px;">' + rows + '</div>' : '') + legend;
    })
    .catch(function () { box.textContent = '⚠️ 自动检测失败，将沿用上方全局模型。'; });
}

// 阶段配置版本号：默认模型/阶段结构变更时 +1 —— 旧 localStorage 存档会被丢弃、
// 按新默认值重置（否则改了 PIPE_STAGES 默认值，老用户界面永远显示旧值，2026-09-18 实证）。
const PIPE_CFG_VERSION = 4;

function _loadPipeStages() {
  let saved = null;
  try { saved = JSON.parse(localStorage.getItem('llmPipelineConfig') || 'null'); } catch (e) { saved = null; }
  if (!saved || saved._v !== PIPE_CFG_VERSION) {
    // 新用户 / 旧版本存档：按当前默认值重置（启用 + 自动选择），随后保存时写入新版本号
    return { enabled: saved ? !!saved.enabled : true,
             auto: saved ? (typeof saved.auto === 'boolean' ? saved.auto : true) : true,
             stages: {} };
  }
  const stages = (saved.stages && typeof saved.stages === 'object') ? saved.stages : {};
  // 迁移：老配置无 auto 字段时，已手动配过阶段的保持手动，否则默认自动
  const auto = (typeof saved.auto === 'boolean') ? saved.auto : (Object.keys(stages).length === 0);
  return { enabled: !!saved.enabled, auto: auto, stages: stages };
}

function initLLMPipelineUI() {
  const { enabled, auto, stages } = _loadPipeStages();
  const onEl = document.getElementById('pipeline-enable');
  if (onEl) onEl.checked = enabled;
  const autoEl = document.getElementById('pipeline-auto');
  if (autoEl) autoEl.checked = auto;
  if (document.getElementById('ai-pipe-body')) document.getElementById('ai-pipe-body').style.display = enabled ? 'block' : 'none';
  const manual = document.getElementById('ai-pipe-manual');
  if (manual) manual.style.display = auto ? 'none' : 'block';
  PIPE_STAGES.forEach(function(stage) {
    const provSel = document.getElementById('pipe-' + stage.key + '-provider');
    const modelSel = document.getElementById('pipe-' + stage.key + '-model');
    if (!provSel || !modelSel) return;
    const saved = stages[stage.name];
    const prov = saved && saved.provider ? saved.provider : stage.defProv;
    const model = saved && saved.model ? saved.model : stage.defModel;
    _fillPipeProvider(provSel, modelSel, prov, model);
  });
  if (enabled && auto) refreshAutoPipelineSummary();
}

function saveLLMPipelineConfig() {
  const enabled = !!(document.getElementById('pipeline-enable') && document.getElementById('pipeline-enable').checked);
  const auto = _isPipelineAuto();
  const stages = {};
  PIPE_STAGES.forEach(function(stage) {
    const provSel = document.getElementById('pipe-' + stage.key + '-provider');
    const modelSel = document.getElementById('pipe-' + stage.key + '-model');
    if (!provSel || !modelSel) return;
    const prov = provSel.value;
    const model = modelSel.value;
    if (prov && model) stages[stage.name] = { provider: prov, model: model };
  });
  localStorage.setItem('llmPipelineConfig', JSON.stringify({ _v: PIPE_CFG_VERSION, enabled: enabled, auto: auto, stages: stages }));
}

function loadLLMPipelineConfig() {
  const { enabled, auto, stages } = _loadPipeStages();
  if (!enabled) return null;
  // 自动模式：由后端探测可用 provider 并轮换分配，前端不传具体模型
  if (auto) return { auto: true, stages: null };
  // 手动模式：任一阶段缺 provider/model → 该阶段不参与（后端回退全局）
  const out = {};
  Object.keys(stages).forEach(function(name) {
    const s = stages[name];
    if (s && s.provider && s.model) out[name] = { provider: s.provider, model: s.model };
  });
  return { auto: false, stages: Object.keys(out).length ? out : null };
}

// 页面加载时初始化模型列表、多Agent分模型配置，并恢复保存项
document.addEventListener('DOMContentLoaded', function() {
  onProviderChange();
  const cfg = loadLLMConfig();
  initLLMPipelineUI();
  if (cfg) syncAIModelSelect(cfg.provider, cfg.model);
  // 恢复上次浏览的标签页（标签记忆）
  restoreLastTab();
});

function loadLLMConfig() {
  // 免费 provider 白名单：模型必须全部免费（日日新免费公测 / Ollama 本地 / Groq 免费 / 智谱 GLM-4 Flash 免费额度）
  const FREE_PROVIDERS = ['sensenova', 'ollama', 'groq', 'zhipu', 'dashscope', 'custom'];
  let savedConfig = null;
  try {
    const saved = localStorage.getItem('llmConfig');
    if (saved) {
      savedConfig = JSON.parse(saved);
      // 旧配置若指向收费 provider（openai/deepseek/dashscope）→ 丢弃，回退默认免费模型
      if (savedConfig.provider && FREE_PROVIDERS.indexOf(savedConfig.provider) === -1) {
        savedConfig = null;
      }
      const providerEl = document.getElementById('llm-provider');
      const apiKeyEl = document.getElementById('llm-api-key');
      const apiUrlEl = document.getElementById('llm-api-url');
      const modelEl = document.getElementById('llm-model');

      if (providerEl && savedConfig.provider) {
        providerEl.value = savedConfig.provider;
        onProviderChange();
      }
      if (apiKeyEl && savedConfig.apiKey) apiKeyEl.value = savedConfig.apiKey;
      if (apiUrlEl && savedConfig.apiUrl) apiUrlEl.value = savedConfig.apiUrl;
      if (modelEl && savedConfig.model) modelEl.value = savedConfig.model;
    }
  } catch (e) {
    savedConfig = null;
  }
  // 始终返回配置（未保存时返回默认值），确保流式输出可用
  if (!savedConfig) return Object.assign({}, DEFAULT_LLM_CONFIG);
  const merged = Object.assign({}, DEFAULT_LLM_CONFIG, savedConfig);
  const preset = PROVIDER_PRESETS[merged.provider];
  if (!merged.apiUrl && preset) merged.apiUrl = preset.url;
  return merged;
}

function saveLLMConfig() {
  const config = {
    provider: document.getElementById('llm-provider').value,
    apiKey: document.getElementById('llm-api-key').value,
    apiUrl: document.getElementById('llm-api-url').value,
    model: document.getElementById('llm-model').value
  };
  localStorage.setItem('llmConfig', JSON.stringify(config));
  saveLLMPipelineConfig();  // 一并保存多Agent各阶段分模型
  // 同步更新快捷选择器
  syncAIModelSelect(config.provider, config.model);
  showToast('配置已保存，AI 模型已切换', 'success');
}

// 快捷模型选择器：统一切换
function switchAIModel(val) {
  const parts = val.split('_');
  const provider = parts[0];
  const model = parts.slice(1).join('_');
  if (!provider || !model) return;
  const preset = PROVIDER_PRESETS[provider];
  const config = {
    provider: provider,
    apiKey: document.getElementById('llm-api-key').value || '',
    apiUrl: preset ? preset.url : '',
    model: model
  };
  localStorage.setItem('llmConfig', JSON.stringify(config));
  // 同步设置面板
  const providerEl = document.getElementById('llm-provider');
  const modelEl = document.getElementById('llm-model');
  if (providerEl) providerEl.value = provider;
  if (modelEl) { onProviderChange(); modelEl.value = model; }
  showToast('已切换至 ' + (PROVIDER_MODELS[provider] || []).find(function(m) { return m.id === model; })?.name || model, 'success');
}

function syncAIModelSelect(provider, model) {
  const sel = document.getElementById('ai-model-select');
  if (!sel) return;
  const key = provider + '_' + model;
  if (sel.querySelector('option[value="' + key + '"]')) {
    sel.value = key;
  } else {
    // 无匹配选项时，取第一个或自定义
    sel.value = sel.options[0].value;
  }
}

// ===== AI 回答模式（⚡ 快速 / 🧠 深度思考）=====

let aiMode = localStorage.getItem('aiMode') || 'fast';

function toggleAIMode() {
  aiMode = aiMode === 'fast' ? 'think' : 'fast';
  localStorage.setItem('aiMode', aiMode);
  updateAIModeButton();
  showToast(aiMode === 'fast' ? '⚡ 快速：知识秒回 + 轻量分析（关思考，少轮次）' : '🧠 深度思考：联网深挖 + 工具分析', 'success');
}

function updateAIModeButton() {
  const btn = document.getElementById('ai-mode-btn');
  if (btn) {
    btn.textContent = aiMode === 'fast' ? '⚡' : '🧠';
    btn.style.color = aiMode === 'fast' ? '#22c55e' : 'var(--primary)';
    btn.title = aiMode === 'fast' ? '⚡ 快速模式（知识秒回 · 轻量分析）' : '🧠 深度思考模式（联网 + 工具分析）';
  }
}

document.addEventListener('DOMContentLoaded', function() {
  updateAIModeButton();
  loadAiSidebar();
});

function clearLLMConfig() {
  localStorage.removeItem('llmConfig');
  document.getElementById('llm-api-key').value = '';
  showToast('配置已清除', 'info');
}

function toggleAISettings() {
  const el = document.getElementById('ai-settings');
  if (!el) return;
  el.style.display = el.style.display === 'none' ? 'block' : 'none';
  if (el.style.display === 'block') loadEgressPolicy();   // 打开面板即拉取当前出境开关
}

// ==================== 数据出境开关（联网搜索 / 文件样本） ====================
// 默认值、当前生效值、来源（环境变量 / 面板 / 默认）都在面板里显示——回答"默认开着还是关着"。
let _egressPolicy = null;
let _egressLoading = false;

async function loadEgressPolicy() {
  _egressLoading = true;
  try {
    const r = await fetch('/api/assistant/egress-policy', { credentials: 'same-origin' });
    const d = await r.json();
    if (d && d.ok) _egressPolicy = d;
  } catch (e) {
    // 拉取失败不阻塞界面；保持 null → 按默认（开启）提示
  }
  _egressLoading = false;
  renderEgressPolicy();
}

const _EGRESS_SRC = { env: '环境变量（强制）', file: '面板设置', default: '默认值' };

function renderEgressPolicy() {
  const box = document.getElementById('ai-egress-body');
  if (!box || !_egressPolicy || !_egressPolicy.features) return;
  box.innerHTML = _egressPolicy.features.map(f => `
    <label class="ai-pipe-switch" style="margin-top:6px;">
      <input type="checkbox" ${f.enabled ? 'checked' : ''} ${f.locked ? 'disabled' : ''}
             onchange="toggleEgressFeature('${f.key}', this.checked)" />
      ${esc(f.label)}
      <span class="ai-egress-tag">默认：${f.default ? '开启' : '关闭'} · 当前来源：${_EGRESS_SRC[f.source] || f.source}</span>
    </label>
    <div class="ai-pipe-hint">${esc(f.enabled ? ('开启时上行 → ' + f.upstream) : ('已关闭：' + f.when_off))}</div>
  `).join('');
  updateEgressHint();
}

async function toggleEgressFeature(key, enabled) {
  try {
    const r = await fetch('/api/assistant/egress-policy', {
      method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ [key]: enabled })
    });
    const d = await r.json();
    if (!d.ok) showToast(d.error || '设置未生效', 'warning');
    else _egressPolicy = d;
  } catch (e) {
    showToast('设置失败：' + e, 'warning');
  }
  if (_egressPolicy) renderEgressPolicy(); else loadEgressPolicy();
  updateEgressHint();
}

// 上下文栏知情提示：挂了文件时明确告知样本行是否出境（文件样本是最大的一笔出境量）
function updateEgressHint() {
  const el = document.getElementById('ai-egress-hint');
  if (!el) return;
  const feat = ((_egressPolicy || {}).features || []).find(x => x.key === 'file_sample_egress');
  const on = feat ? feat.enabled : true;
  const hasFile = (typeof getCurrentFile === 'function') && !!getCurrentFile();
  if (!hasFile) { el.style.display = 'none'; return; }
  el.style.display = 'inline';
  el.textContent = on ? '📤 文件样本会随提问发往云端模型' : '🔒 仅本地统计：样本行不外发';
  el.title = on
    ? '当前文件的前 6000 字符样本行进入模型 Prompt（清单见 design/DATA_EGRESS.md）。可在 ⚙️ 设置 → 数据出境中关闭。'
    : '当前只上行列名与统计量，样本行不随提问发送。';
}

async function testLLMConnection() {
  const config = {
    provider: document.getElementById('llm-provider').value,
    apiKey: document.getElementById('llm-api-key').value,
    apiUrl: document.getElementById('llm-api-url').value,
    model: document.getElementById('llm-model').value
  };
  
  // API Key 可留空：服务器端 .env 配置的密钥优先（如 SENSENOVA_API_KEY / DEEPSEEK_API_KEY）
  if (!config.apiKey && config.provider === 'custom') {
    showToast('自定义端点需要填写 API Key', 'warning');
    return;
  }
  
  showLoading('测试连接...');
  try {
    const res = await fetch('/api/llm/test', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(config)
    });
    const data = await res.json();
    hideLoading();
    
    if (data.ok) {
      showToast('✅ 连接成功: ' + data.model, 'success');
    } else {
      showToast('❌ 连接失败: ' + data.error, 'error');
    }
  } catch (e) {
    hideLoading();
    showToast('测试失败: ' + e.message, 'error');
  }
}

// ============ 全局 apiFetch：统一 Loading + 错误 Toast + 成功提示 ============
// 用法：apiFetch('/api/xxx', {method:'POST', body:JSON.stringify(data)}, {loading:'处理中', success:'完成', toast:true})
// 自动：请求期间 Loading、响应非 2xx 或含 error 字段时 Toast 错误、可选成功提示
let _loadingCount = 0;
window.apiFetch = async function (url, opts, cfg) {
  cfg = cfg || {};
  if (cfg.loading !== false) {
    _loadingCount++;
    if (_loadingCount === 1) showLoading(cfg.loading || '处理中…');
  }
  try {
    const res = await fetch(url, Object.assign({ headers: { 'Content-Type': 'application/json' } }, opts || {}));
    let data = null;
    try { data = await res.json(); } catch (e) { /* 非 JSON 响应 */ }
    if (!res.ok || (data && data.error && res.status >= 400)) {
      const msg = (data && data.error) || ('请求失败（HTTP ' + res.status + '）');
      if (cfg.toast !== false) showToast(msg, 'error');
      const err = new Error(msg);
      err.status = res.status; err.data = data;
      throw err;
    }
    if (cfg.success) showToast(cfg.success, 'success');
    return data;
  } catch (e) {
    if (e && e.status) throw e;               // 已处理的业务错误
    if (cfg.toast !== false) showToast('网络错误: ' + e.message, 'error');
    throw e;
  } finally {
    if (cfg.loading !== false) {
      _loadingCount = Math.max(0, _loadingCount - 1);
      if (_loadingCount === 0) hideLoading();
    }
  }
};

// 全局未捕获错误：避免静默失败（只弹一次，防刷屏）
let _errToastAt = 0;
window.addEventListener('error', function (ev) {
  const now = Date.now();
  if (now - _errToastAt < 30000) return;
  _errToastAt = now;
  showToast('页面脚本错误: ' + (ev.message || '未知'), 'error');
});

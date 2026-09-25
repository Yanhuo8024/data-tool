// =====================================================================
// finance.js — 金融数据/可视化/建模
// =====================================================================

// ===== finance-data.js =====

// ===== 数据源目录（/api/finance/catalog）=====
// 后端 catalog 是数据源清单的唯一真相源；前端在静态选项基础上再做一次动态刷新，
// 这样后端新增接口无需同步改 HTML。
window._finCatalog = null;

function _finFuncMeta(sourceId, func) {
  const c = window._finCatalog;
  if (!c || !c.sources || !func) return null;
  const s = c.sources.find(x => x.id === sourceId);
  if (!s) return null;
  for (const g of (s.groups || [])) {
    const hit = (g.funcs || []).find(f => f.func === func);
    if (hit) return hit;
  }
  return null;
}

function _finBuildOptions(groups, placeholder) {
  let html = '<option value="">' + placeholder + '</option>';
  (groups || []).forEach(g => {
    html += '<optgroup label="' + esc(g.cat) + '">';
    (g.funcs || []).forEach(f => {
      html += '<option value="' + f.func + '">' + esc(f.label) + '</option>';
    });
    html += '</optgroup>';
  });
  return html;
}

async function loadFinanceCatalog() {
  const selAk = document.getElementById('ak-func');
  const selTs = document.getElementById('ts-func');
  const selYf = document.getElementById('yf-func');
  const selFred = document.getElementById('fred-func');
  if (!selAk && !selTs && !selYf && !selFred) return;
  try {
    const res = await fetch('/api/finance/catalog');
    const data = await res.json();
    if (!data || !data.ok || !data.sources) return;
    window._finCatalog = data;
    const byId = {};
    data.sources.forEach(s => { byId[s.id] = s; });
    if (selAk && byId.akshare) {
      const keep = selAk.value;
      selAk.innerHTML = _finBuildOptions(byId.akshare.groups, '-- 请选择数据类型 --');
      if (keep) selAk.value = keep;
      const cnt = document.getElementById('ak-func-count');
      if (cnt && byId.akshare.count) cnt.textContent = byId.akshare.count;
    }
    if (selTs && byId.tushare) {
      const keep = selTs.value;
      selTs.innerHTML = _finBuildOptions(byId.tushare.groups, '-- 请选择数据类型 --');
      if (keep) selTs.value = keep;
      const cnt = document.getElementById('ts-func-count');
      if (cnt && byId.tushare.count) cnt.textContent = byId.tushare.count;
    }
    if (selYf && byId.yfinance) {
      const keep = selYf.value;
      selYf.innerHTML = _finBuildOptions(byId.yfinance.groups, '-- 选择类型 --');
      if (keep) selYf.value = keep;
      const cnt = document.getElementById('yf-func-count');
      if (cnt && byId.yfinance.count) cnt.textContent = byId.yfinance.count;
    }
    if (selFred && byId.fred) {
      const keep = selFred.value;
      selFred.innerHTML = _finBuildOptions(byId.fred.groups, '-- 选择序列 --');
      if (keep) selFred.value = keep;
      const cnt = document.getElementById('fred-func-count');
      if (cnt && byId.fred.count) cnt.textContent = byId.fred.count + ' 个精选序列';
    }
    onAkFuncChange();
  } catch (e) { /* 静默失败：保留 HTML 中的静态选项 */ }
}

// 目录未就绪时的参数回退表（date=报告期/交易日，start_year=起始年份）
const AK_PARAM_FALLBACK = {
  stock_zcfz_em: ['date'], stock_lrb_em: ['date'], stock_xjll_em: ['date'],
  stock_yjbb_em: ['date'], stock_margin_szse: ['date'], stock_notice_report: ['date'],
  stock_gpzy_pledge_ratio_em: ['date'], stock_zt_pool_em: ['date'],
  futures_spot_price: ['date'], fund_portfolio_hold_em: ['date'],
  stock_margin_sse: ['start_date', 'end_date'],
  stock_financial_analysis_indicator: ['start_year'],
};

// akshare 数据类型变化时，按该接口声明的参数显示/隐藏相关输入行
function onAkFuncChange() {
  const sel = document.getElementById('ak-func');
  const row = document.getElementById('ak-indicator-row');
  const dateRow = document.getElementById('ak-date-row');
  const yearRow = document.getElementById('ak-year-row');
  const func = sel ? sel.value : '';
  if (row) {
    row.style.display = (func === 'fund_open_fund_info_em') ? 'block' : 'none';
  }
  const meta = _finFuncMeta('akshare', func);
  const params = (meta && meta.params && meta.params.length)
    ? meta.params : (AK_PARAM_FALLBACK[func] || []);
  if (dateRow) dateRow.style.display = params.includes('date') ? 'block' : 'none';
  if (yearRow) yearRow.style.display = params.includes('start_year') ? 'block' : 'none';
}

document.addEventListener('DOMContentLoaded', function() {
  const akf = document.getElementById('ak-func');
  if (akf) { akf.addEventListener('change', onAkFuncChange); onAkFuncChange(); }
});

// 切入金融 tab 时刷新目录（tab 为按需渲染，首次可能尚不存在）
document.addEventListener('click', function(e) {
  if (e.target && e.target.dataset && e.target.dataset.t === 'tab-finance') {
    setTimeout(loadFinanceCatalog, 60);
  }
});

async function doAkshare() {
  const func = document.getElementById('ak-func')?.value;
  const symbol = document.getElementById('ak-symbol')?.value;
  const start = document.getElementById('ak-start')?.value;
  const end = document.getElementById('ak-end')?.value;
  const indicator = document.getElementById('ak-indicator')?.value || '';
  const period = document.getElementById('ak-period')?.value || '';
  const adjust = document.getElementById('ak-adjust')?.value || '';
  const date = document.getElementById('ak-date')?.value || '';
  const startYear = document.getElementById('ak-start-year')?.value || '';
  const filename = document.getElementById('ak-name')?.value || '';
  const paramsRaw = (document.getElementById('ak-params')?.value || '').trim();
  let params = {};
  if (paramsRaw) {
    try { params = JSON.parse(paramsRaw); }
    catch (err) { showToast('高级参数不是合法 JSON', 'warning'); return; }
  }
  if (!func) { showToast('请选择数据类型', 'warning'); return; }
  showLoading('正在获取数据...');
  try {
    const res = await fetch('/api/finance/akshare', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ func, symbol, start_date: start, end_date: end,
                             indicator, period, adjust, date, start_year: startYear,
                             params, filename })
    });
    const data = await res.json();
    const el = document.getElementById('ak-result');
    if (!el) return;
    if (data.error) { el.innerHTML = '<span style="color:red">' + data.error + '</span>'; return; }
    if (data.preview) el.innerHTML = '<pre>' + JSON.stringify(data.preview, null, 2) + '</pre>';

    // 同步到其他标签页 + 提示保存位置 + 刷新文件列表
    if (data.file) {
      FinanceSync.update({ file: data.file, symbol, source: 'akshare', rows: data.rows || 0, cols: data.cols || 0, preview: data.preview });
      appendSavedHint(el, data.file, data.rows);
      refreshFinanceFileList();
      showToast('已保存到 output/' + data.file, 'success');
      // 刷新顶部全局文件选择栏并自动选中新文件
      if (typeof refreshGlobalFileSelect === 'function') {
        refreshGlobalFileSelect();
        setTimeout(function() {
          if (typeof window.selectFile === 'function') window.selectFile(data.file);
        }, 400);
      }
    }
  } catch (e) {
    showToast('获取失败', 'error');
  } finally {
    hideLoading();
  }
}

async function doTushare() {
  const func = document.getElementById('ts-func')?.value;
  const code = document.getElementById('ts-code')?.value;
  const start = document.getElementById('ts-start')?.value;
  const end = document.getElementById('ts-end')?.value;
  const token = document.getElementById('ts-token')?.value || '';
  const filename = document.getElementById('ts-name')?.value || '';
  if (!func) { showToast('请选择数据类型', 'warning'); return; }
  showLoading('正在获取数据...');
  try {
    const res = await fetch('/api/finance/tushare', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ func, ts_code: code, start_date: start, end_date: end,
                             token, filename })
    });
    const data = await res.json();
    const el = document.getElementById('ts-result');
    if (!el) return;
    if (data.error) { el.innerHTML = '<span style="color:red">' + data.error + '</span>'; return; }
    if (data.preview) el.innerHTML = '<pre>' + JSON.stringify(data.preview, null, 2) + '</pre>';

    // 同步到其他标签页 + 提示保存位置 + 刷新文件列表
    if (data.file) {
      FinanceSync.update({ file: data.file, symbol: code, source: 'tushare', rows: data.rows || 0, cols: data.cols || 0, preview: data.preview });
      appendSavedHint(el, data.file, data.rows);
      refreshFinanceFileList();
      showToast('已保存到 output/' + data.file, 'success');
      // 刷新顶部全局文件选择栏并自动选中新文件
      if (typeof refreshGlobalFileSelect === 'function') {
        refreshGlobalFileSelect();
        setTimeout(function() {
          if (typeof window.selectFile === 'function') window.selectFile(data.file);
        }, 400);
      }
    }
  } catch (e) {
    showToast('获取失败', 'error');
  } finally {
    hideLoading();
  }
}

async function doBaostock() {
  const func = document.getElementById('bs-func')?.value;
  const symbol = document.getElementById('bs-symbol')?.value;
  const period = document.getElementById('bs-period')?.value;
  const adjust = document.getElementById('bs-adjust')?.value;
  const start = document.getElementById('bs-start')?.value;
  const end = document.getElementById('bs-end')?.value;
  const year = document.getElementById('bs-year')?.value;
  if (!func) { showToast('请选择数据类型', 'warning'); return; }
  showLoading('正在获取数据（baostock）...');
  try {
    const res = await fetch('/api/finance/baostock', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ func, symbol, period, adjust, start_date: start, end_date: end, year })
    });
    const data = await res.json();
    const el = document.getElementById('fi-preview');
    const statEl = document.getElementById('bs-stat');
    const card = document.getElementById('fi-res-card');
    if (data.error) {
      if (el) el.innerHTML = '<span style="color:red">' + data.error + '</span>';
      return;
    }
    if (statEl) statEl.textContent = data.rows + ' 行 × ' + data.cols + ' 列';
    if (card) card.classList.remove('hide');
    if (el) el.innerHTML = '<pre>' + JSON.stringify(data.preview, null, 2) + '</pre>';
    window._lastFiFile = data.file;

    // 同步到其他标签页 + 提示保存位置 + 刷新文件列表
    if (data.file) {
      FinanceSync.update({ file: data.file, symbol, source: 'baostock', rows: data.rows || 0, cols: data.cols || 0, preview: data.preview });
      appendSavedHint(el, data.file, data.rows);
      refreshFinanceFileList();
      showToast('已保存到 output/' + data.file, 'success');

      // 刷新顶部全局文件选择栏，并自动选中刚保存的 baostock 文件（供后续分析直接使用）
      if (typeof refreshGlobalFileSelect === 'function') {
        refreshGlobalFileSelect();
        setTimeout(function() {
          if (typeof window.selectFile === 'function') {
            window.selectFile(data.file);
          }
        }, 400); // 等待文件列表刷新完成后自动选中
      }
    }
  } catch (e) {
    showToast('获取失败: ' + (e.message || ''), 'error');
  } finally {
    hideLoading();
  }
}

async function doYFinance() {
  const func = document.getElementById('yf-func')?.value;
  const symbol = document.getElementById('yf-symbol')?.value;
  const period = document.getElementById('yf-period')?.value;
  const start = document.getElementById('yf-start')?.value;
  const end = document.getElementById('yf-end')?.value;
  const filename = document.getElementById('yf-name')?.value || '';
  if (!func) { showToast('请选择数据类型', 'warning'); return; }
  showLoading('正在获取数据（yfinance）...');
  try {
    const res = await fetch('/api/finance/yfinance', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ func, symbol, period, start_date: start, end_date: end, filename })
    });
    const data = await res.json();
    const el = document.getElementById('fi-preview');
    const statEl = document.getElementById('yf-stat');
    const card = document.getElementById('fi-res-card');
    if (data.error) {
      if (el) el.innerHTML = '<span style="color:red">' + data.error + '</span>';
      showToast(data.error, 'error');
      return;
    }
    if (statEl) statEl.textContent = data.rows + ' 行 × ' + data.cols + ' 列';
    if (card) card.classList.remove('hide');
    if (el) el.innerHTML = '<pre>' + JSON.stringify(data.preview, null, 2) + '</pre>';
    window._lastFiFile = data.file;

    if (data.file) {
      FinanceSync.update({ file: data.file, symbol, source: 'yfinance', rows: data.rows || 0, cols: data.cols || 0, preview: data.preview });
      appendSavedHint(el, data.file, data.rows);
      refreshFinanceFileList();
      showToast('已保存到 output/' + data.file, 'success');
      if (typeof refreshGlobalFileSelect === 'function') {
        refreshGlobalFileSelect();
        setTimeout(function() {
          if (typeof window.selectFile === 'function') window.selectFile(data.file);
        }, 400);
      }
    }
  } catch (e) {
    showToast('获取失败: ' + (e.message || ''), 'error');
  } finally {
    hideLoading();
  }
}

async function doFred() {
  const func = document.getElementById('fred-func')?.value;
  const query = document.getElementById('fred-query')?.value;
  const start = document.getElementById('fred-start')?.value;
  const end = document.getElementById('fred-end')?.value;
  const filename = document.getElementById('fred-name')?.value || '';
  if (!func) { showToast('请选择序列或功能', 'warning'); return; }
  if (func === 'search' && !(query || '').trim()) {
    showToast('检索请先填写关键词', 'warning'); return;
  }
  showLoading('正在获取数据（FRED）...');
  try {
    const res = await fetch('/api/finance/fred', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ func, query, start_date: start, end_date: end, filename })
    });
    const data = await res.json();
    const el = document.getElementById('fi-preview');
    const statEl = document.getElementById('fred-stat');
    const card = document.getElementById('fi-res-card');
    if (data.error) {
      if (el) el.innerHTML = '<span style="color:red">' + data.error + '</span>';
      showToast(data.error, 'error');
      return;
    }
    if (statEl) statEl.textContent = data.rows + ' 行 × ' + data.cols + ' 列';
    if (card) card.classList.remove('hide');
    if (el) el.innerHTML = '<pre>' + JSON.stringify(data.preview, null, 2) + '</pre>';
    window._lastFiFile = data.file;

    if (data.file) {
      FinanceSync.update({ file: data.file, symbol: func, source: 'fred', rows: data.rows || 0, cols: data.cols || 0, preview: data.preview });
      appendSavedHint(el, data.file, data.rows);
      refreshFinanceFileList();
      showToast('已保存到 output/' + data.file, 'success');
      if (typeof refreshGlobalFileSelect === 'function') {
        refreshGlobalFileSelect();
        setTimeout(function() {
          if (typeof window.selectFile === 'function') window.selectFile(data.file);
        }, 400);
      }
    }
  } catch (e) {
    showToast('获取失败: ' + (e.message || ''), 'error');
  } finally {
    hideLoading();
  }
}

// 计算开放基金业绩指标（夏普比率、最大回撤、年化收益等）
async function calcFundMetrics() {
  const file = (typeof getCurrentFile === 'function' && getCurrentFile()) || window._lastFiFile || window._selectedFile;
  if (!file) {
    showToast('请先获取/选择一个基金净值文件', 'warning');
    return;
  }
  const card = document.getElementById('fund-metrics-card');
  if (!card) return;
  card.style.display = 'block';
  card.innerHTML = '<span class="spin"></span> 正在计算基金业绩指标...';
  try {
    const res = await fetch('/api/finance/fund-metrics', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ file: file })
    });
    const d = await res.json();
    if (!d.ok || d.error) {
      card.innerHTML = '<span style="color:red">❌ ' + esc(d.error || '计算失败') + '</span>';
      return;
    }
    const fmt = function(v, suf) { return (v === null || v === undefined) ? '-' : v + (suf || ''); };
    const metric = [
      ['累计收益', fmt(d.total_return, '%')],
      ['年化收益', fmt(d.annual_return, '%')],
      ['年化波动', fmt(d.annual_volatility, '%')],
      ['夏普比率', fmt(d.sharpe)],
      ['索提诺', fmt(d.sortino)],
      ['最大回撤', fmt(d.max_drawdown, '%')],
      ['卡玛比率', fmt(d.calmar)],
      ['胜率', fmt(d.win_rate, '%')],
      ['样本数', fmt(d.data_points)]
    ].map(function(r) {
      return '<div style="display:flex;justify-content:space-between;padding:4px 0;font-size:13px;border-bottom:1px solid rgba(255,255,255,.05);"><span style="color:var(--ink-subtle)">' + r[0] + '</span><b>' + r[1] + '</b></div>';
    }).join('');
    const chartHtml = d.chart ? '<img src="' + d.chart + '" style="max-width:100%;border-radius:8px;margin-top:8px;"/>' : '';
    card.innerHTML = '<div style="font-weight:600;font-size:14px;margin-bottom:6px;">📊 基金业绩指标 — ' + esc(file) + '</div>' +
      '<div style="display:grid;grid-template-columns:repeat(3,1fr);gap:0 16px;">' + metric + '</div>' + chartHtml;
  } catch (e) {
    card.innerHTML = '<span style="color:red">❌ ' + esc(e.message || '计算失败') + '</span>';
  }
}

// 在结果区域追加"已自动保存"提示 + 下载链接
function appendSavedHint(el, fname, rows) {
  if (!el || !fname) return;
  const dl = '/api/download/' + encodeURIComponent(fname);
  el.innerHTML += '<div style="margin-top:8px;font-size:12px;color:var(--ink-subtle);">' +
    '💾 已自动保存到 <code>output/' + fname + '</code>（' + (rows || 0) + ' 行）' +
    '<a href="' + dl + '" style="margin-left:10px;color:var(--accent);">⬇️ 下载</a>' +
    '</div>';
}

// 金融数据获取成功后，同步刷新所有文件列表（仪表盘 / 文件管理 / 顶栏下拉）
function refreshFinanceFileList() {
  if (typeof loadMultiFiles === 'function') loadMultiFiles();
  if (typeof refreshGlobalFileSelect === 'function') refreshGlobalFileSelect();
}

function dlFinance(name) {
  window.location.href = '/api/download/' + (name || window._lastFiFile || '');
}

// ===== finance-viz.js =====

const FV_CHART_MAP = {
  'viz-candle': 'candle', 'viz-ma': 'ma', 'viz-macd': 'macd', 'viz-rsi': 'rsi',
  'viz-boll': 'boll', 'viz-volume': 'volume', 'viz-heatmap': 'heatmap',
  'viz-returns': 'returns', 'viz-bar': 'bar', 'viz-box': 'box', 'viz-density': 'density'
};

function _getFvCharts() {
  return Object.entries(FV_CHART_MAP)
    .filter(([id]) => document.getElementById(id)?.checked)
    .map(([, val]) => val);
}

async function runFinViz() {
  const file = getCurrentFile();
  const charts = _getFvCharts();
  if (!file) { showToast('请先在顶栏选择数据文件', 'warning'); return; }
  if (charts.length === 0) { showToast('请至少选择一种图表', 'warning'); return; }
  showLoading('正在生成图表...');
  try {
    const res = await fetch('/api/finance/viz', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ file, charts })
    });
    const data = await res.json();
    const el = document.getElementById('fin-viz-res');
    const chartBox = document.getElementById('fin-viz-charts');
    if (data.error) { if (el) el.innerHTML = '<span style="color:red">' + data.error + '</span>'; return; }
    if (chartBox && data.figures) {
      chartBox.innerHTML = data.figures.map(f =>
        '<figure style="margin:8px 0;text-align:center;"><img src="data:image/png;base64,' + f.data + '" style="max-width:100%;border-radius:8px;"><figcaption style="color:var(--ink-subtle);font-size:12px;margin-top:4px;">' + f.name + '</figcaption></figure>'
      ).join('');
      if (el) el.innerHTML = '<div style="color:var(--ink-subtle);font-size:12px;">已生成 ' + data.figures.length + ' 张图表</div>';
    }
  } catch (e) {
    showToast('可视化失败', 'error');
  } finally {
    hideLoading();
  }
}

function runFinVizAll() {
  Object.keys(FV_CHART_MAP).forEach(id => { const el = document.getElementById(id); if (el) el.checked = true; });
  runFinViz();
}

async function runAdvViz() {
  const file = getCurrentFile();
  if (!file) { showToast('请先在顶栏选择数据文件', 'warning'); return; }
  const map = { 'adv-moneyflow': 'money_flow', 'adv-chip': 'chip', 'adv-heatmap': 'heatmap', 'adv-return': 'return_curve', 'adv-drawdown': 'drawdown' };
  let charts = Object.entries(map).filter(([id]) => document.getElementById(id)?.checked).map(([, v]) => v);
  if (charts.length === 0) charts = ['money_flow'];
  const styles = {
    theme: document.getElementById('adv-theme')?.value || undefined,
    width: parseInt(document.getElementById('adv-width')?.value) || undefined,
    height: parseInt(document.getElementById('adv-height')?.value) || undefined,
    watermark: document.getElementById('adv-watermark')?.value || undefined
  };
  showLoading('正在生成高级图表...');
  try {
    const res = await fetch('/api/finance/viz-advanced', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ file, charts, styles })
    });
    const data = await res.json();
    const chartBox = document.getElementById('adv-viz-charts');
    if (data.error) { showToast(data.error, 'error'); return; }
    if (chartBox && data.charts) {
      chartBox.innerHTML = data.charts.map(c => {
        if (c.error) return '<div style="color:red;padding:8px;">' + c.name + ': ' + c.error + '</div>';
        return '<div style="margin:12px 0;"><div style="color:var(--ink-subtle);font-size:12px;margin-bottom:4px;">' + c.name + '</div>' + (c.html || '') + '</div>';
      }).join('');
    }
  } catch (e) {
    showToast('高级可视化失败', 'error');
  } finally {
    hideLoading();
  }
}

function runAdvVizAll() {
  Object.keys({ 'adv-moneyflow': 1, 'adv-chip': 1, 'adv-heatmap': 1, 'adv-return': 1, 'adv-drawdown': 1 }).forEach(id => { const el = document.getElementById(id); if (el) el.checked = true; });
  runAdvViz();
}

async function batchExportAdv() {
  const file = getCurrentFile();
  if (!file) { showToast('请先在顶栏选择数据文件', 'warning'); return; }
  showLoading('正在批量导出...');
  try {
    const res = await fetch('/api/finance/viz-batch-export', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ file })
    });
    if (!res.ok) { const d = await res.json().catch(() => ({})); showToast(d.error || '导出失败', 'error'); return; }
    const blob = await res.blob();
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = 'charts_' + new Date().toISOString().slice(0, 10) + '.zip';
    a.click();
    URL.revokeObjectURL(a.href);
    showToast('导出成功', 'success');
  } catch (e) {
    showToast('批量导出失败', 'error');
  } finally {
    hideLoading();
  }
}

async function runCompare() {
  const listEl = document.getElementById('compare-stock-list');
  const metric = document.getElementById('compare-metric')?.value || 'close';
  const stocks = listEl
    ? Array.from(listEl.querySelectorAll('input[type=checkbox]:checked')).map(c => c.value)
    : [];
  if (stocks.length < 2) { showToast('请至少选择两只股票', 'warning'); return; }
  const resEl = document.getElementById('compare-res');
  const chartEl = document.getElementById('compare-chart');
  showLoading('正在生成对比图...');
  try {
    const res = await fetch('/api/finance/viz-compare', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ stocks, metric })
    });
    const data = await res.json();
    if (data.error || data.ok === false) {
      if (resEl) resEl.innerHTML = '<span style="color:red">' + (data.error || '生成失败') + '</span>';
      return;
    }
    if (chartEl && data.html) chartEl.innerHTML = data.html;
    if (resEl) resEl.innerHTML = '';
    showToast('对比图生成成功', 'success');
  } catch (e) {
    showToast('多股对比失败', 'error');
  } finally {
    hideLoading();
  }
}

// 加载可对比的股票文件列表（切换到可视化 tab 时自动填充）
async function loadCompareStocks() {
  const listEl = document.getElementById('compare-stock-list');
  if (!listEl || listEl.childElementCount > 0) return;
  try {
    const res = await fetch('/api/finance/viz-stocks');
    const data = await res.json();
    const stocks = data.stocks || [];
    if (!stocks.length) { listEl.innerHTML = '<span style="color:#888;font-size:12px;">暂无股票数据文件</span>'; return; }
    listEl.innerHTML = stocks.map(s => {
      const name = typeof s === 'string' ? s : (s.name || s.file || '');
      return '<label style="font-size:12px;margin-right:10px;cursor:pointer;"><input type="checkbox" value="' + name + '"> ' + name + '</label>';
    }).join('');
  } catch (e) { /* 静默失败 */ }
}

function runSavedPredict() {
  const sel = document.getElementById('saved-model-list');
  const model_name = sel?.value;
  if (!model_name) { showToast('请先选择模型', 'warning'); return; }
  const predict_days = parseInt(document.getElementById('saved-predict-days')?.value) || 10;
  const close_col = document.getElementById('saved-close')?.value || undefined;
  const resEl = document.getElementById('saved-res');
  const chartEl = document.getElementById('saved-chart');
  showLoading('正在用保存模型预测...');
  fetch('/api/finance/model-predict', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ model_name, predict_days, close_col })
  })
    .then(r => r.json())
    .then(data => {
      hideLoading();
      if (data.error || data.ok === false) {
        if (resEl) resEl.innerHTML = '<span style="color:red">' + (data.error || '预测失败') + '</span>';
        return;
      }
      let html = '';
      if (data.predictions && data.predictions.length) {
        html = '<div style="font-size:13px;">未来 ' + data.predictions.length + ' 天预测：' +
          data.predictions.map(p => (typeof p === 'number' ? p.toFixed(2) : p)).join(' → ') + '</div>';
      }
      if (data.metrics) html += '<pre style="font-size:12px;">' + JSON.stringify(data.metrics, null, 2) + '</pre>';
      if (resEl) resEl.innerHTML = html || '<span style="color:var(--ink-subtle)">无结果</span>';
      if (chartEl && data.chart) chartEl.innerHTML = '<img src="data:image/png;base64,' + data.chart + '" style="max-width:100%;border-radius:8px;">';
      showToast('预测完成', 'success');
    })
    .catch(() => { hideLoading(); showToast('预测失败', 'error'); });
}

function deleteSavedModel() {
  const sel = document.getElementById('saved-model-list');
  const model_name = sel?.value;
  if (!model_name) { showToast('请先选择要删除的模型', 'warning'); return; }
  if (!confirm('确定删除模型「' + model_name + '」？此操作不可恢复。')) return;
  fetch('/api/finance/model-delete', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ model_name })
  })
    .then(r => r.json())
    .then(data => {
      if (data.error || data.ok === false) { showToast(data.error || '删除失败', 'error'); return; }
      showToast('模型已删除', 'success');
      refreshModels();
    })
    .catch(() => showToast('删除失败', 'error'));
}

// ===== finance-model.js =====

async function runFinModel() {
  const file = getCurrentFile();
  const model_type = document.getElementById('fin-model-type')?.value;
  const predict_days = parseInt(document.getElementById('fin-predict-days')?.value) || 5;
  if (!file) { showToast('请先在顶栏选择数据文件', 'warning'); return; }
  if (!model_type) { showToast('请选择建模类型', 'warning'); return; }
  showLoading('正在建模...');
  try {
    const res = await fetch('/api/finance/model', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ file, model_type, predict_days })
    });
    const data = await res.json();
    const el = document.getElementById('fin-model-res');
    const chartBox = document.getElementById('fin-model-charts');
    if (!el) return;
    if (data.error) { el.innerHTML = '<span style="color:red">' + data.error + '</span>'; return; }
    let html = '<div class="stats" style="display:flex;gap:16px;flex-wrap:wrap;">' +
      '<div class="sc"><div class="sn">' + (data.model_name || '-') + '</div><div class="sl">模型</div></div>' +
      '<div class="sc"><div class="sn">' + (data.rmse != null ? data.rmse.toFixed(3) : '-') + '</div><div class="sl">RMSE</div></div>' +
      '<div class="sc"><div class="sn">' + (data.mae != null ? data.mae.toFixed(3) : '-') + '</div><div class="sl">MAE</div></div>' +
      '<div class="sc"><div class="sn">' + (data.r2 != null ? data.r2.toFixed(3) : '-') + '</div><div class="sl">R²</div></div>' +
      '</div>';
    if (data.predictions && data.predictions.length) {
      html += '<div style="margin-top:8px;font-size:12px;color:var(--ink-subtle);">未来 ' + data.predictions.length + ' 天预测：' +
        data.predictions.map(p => p.toFixed(2)).join(' → ') + '</div>';
    }
    el.innerHTML = html;
    if (chartBox && data.figures) {
      chartBox.innerHTML = data.figures.map(f =>
        '<img src="data:image/png;base64,' + f.data + '" style="max-width:100%;border-radius:8px;margin:8px 0;">'
      ).join('');
    }
  } catch (e) {
    showToast('建模失败', 'error');
  } finally {
    hideLoading();
  }
}

async function runCAPM() {
  const file = getCurrentFile();
  if (!file) { showToast('请先在顶栏选择数据文件', 'warning'); return; }
  showLoading('正在计算 CAPM...');
  try {
    const res = await fetch('/api/finance/capm', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        file,
        rf_rate: parseFloat(document.getElementById('capm-rf')?.value) || 0.02,
        close_col: document.getElementById('capm-close')?.value || undefined
      })
    });
    const data = await res.json();
    const el = document.getElementById('capm-res');
    const chartEl = document.getElementById('capm-chart');
    if (data.error) { if (el) el.innerHTML = '<span style="color:red">' + data.error + '</span>'; return; }
    let html = '';
    if (data.beta !== undefined) {
      html = '<table style="font-size:13px;"><tr><th>指标</th><th>值</th></tr>' +
        '<tr><td>Beta</td><td>' + data.beta + '</td></tr>' +
        '<tr><td>Alpha(日)</td><td>' + data.alpha_daily + '</td></tr>' +
        '<tr><td>年化收益</td><td>' + data.annual_return_pct + '%</td></tr>' +
        '<tr><td>年化波动</td><td>' + data.annual_volatility_pct + '%</td></tr>' +
        '<tr><td>夏普比率</td><td>' + data.sharpe_ratio + '</td></tr>' +
        '<tr><td>最大回撤</td><td>' + data.max_drawdown_pct + '%</td></tr>' +
        '<tr><td>预期收益率</td><td>' + data.expected_return_pct + '%</td></tr>' +
        '<tr><td>胜率</td><td>' + data.win_rate_pct + '%</td></tr>' +
        '<tr><td>样本数</td><td>' + data.data_points + '</td></tr></table>';
    }
    if (data.chart) html += '<img src="data:image/png;base64,' + data.chart + '" style="max-width:100%;border-radius:8px;margin-top:12px;">';
    if (el) el.innerHTML = html || '<span style="color:var(--ink-subtle)">无结果</span>';
  } catch (e) {
    showToast('CAPM 失败', 'error');
  } finally {
    hideLoading();
  }
}

async function runFamaFrench() {
  const resEl = document.getElementById('ff-res');
  const chartEl = document.getElementById('ff-chart');
  const corrEl = document.getElementById('ff-corr');
  if (!resEl) return showToast('五因子区域不存在', 'warning');
  showLoading('正在拉取行情与财务数据、构造因子组合，约需 1-2 分钟...');
  resEl.innerHTML = '';
  if (chartEl) chartEl.innerHTML = '';
  if (corrEl) corrEl.innerHTML = '';
  try {
    const res = await fetch('/api/finance/fama-french', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        max_stocks: parseInt(document.getElementById('ff-stocks')?.value) || 30,
        months: parseInt(document.getElementById('ff-months')?.value) || 12,
        rf_annual: parseFloat(document.getElementById('ff-rf')?.value) || 0.02,
      })
    });
    const data = await res.json();
    if (data.error || data.status === 'failed') {
      resEl.innerHTML = '<span style="color:var(--error)">' + (data.error || '运行失败') + '</span>';
      return;
    }
    const info = data.info || {};
    let html = '<div class="stats">' +
      '<div class="sc"><div class="sn">' + (info.stocks || '-') + '</div><div class="sl">样本股票</div></div>' +
      '<div class="sc"><div class="sn">' + (info.groups || '-') + '</div><div class="sl">分组期</div></div>' +
      '<div class="sc"><div class="sn">' + (info.days || '-') + '</div><div class="sl">交易日</div></div>' +
      '<div class="sc"><div class="sn" style="font-size:11px;">' + (info.start || '') + ' ~ ' + (info.end || '') + '</div><div class="sl">区间</div></div>' +
      '</div>';
    // 因子统计表
    let tbl = '<table style="margin-top:12px;font-size:13px;"><tr><th>因子</th><th>日收益</th><th>年化收益</th><th>年化波动</th><th>t 值</th></tr>';
    (data.stats || []).forEach(s => {
      tbl += '<tr><td><b>' + s.factor + '</b></td><td>' + s.mean_daily + '</td><td>' + s.annual + '</td><td>' + s.annual_vol + '</td><td>' + s.t_value + '</td></tr>';
    });
    tbl += '</table>';
    html += tbl;
    resEl.innerHTML = html;
    if (data.chart && chartEl) chartEl.innerHTML = '<img src="data:image/png;base64,' + data.chart + '" style="max-width:100%;border-radius:8px;">';
    if (data.corr_html && corrEl) corrEl.innerHTML = '<div style="margin-top:12px;"><b>因子相关系数矩阵：</b>' + data.corr_html + '</div>';
    showToast('五因子模型完成', 'success');
  } catch (e) {
    resEl.innerHTML = '<span style="color:var(--error)">' + e.message + '</span>';
    showToast('五因子运行失败', 'error');
  } finally {
    hideLoading();
  }
}

async function runBacktest() {
  const file = getCurrentFile();
  const strategy = document.getElementById('bt-strategy')?.value || 'ma';
  const fast = parseInt(document.getElementById('bt-fast')?.value) || 5;
  const slow = parseInt(document.getElementById('bt-slow')?.value) || 20;
  const close_col = document.getElementById('bt-close')?.value || undefined;
  if (!file) { showToast('请先在顶栏选择数据文件', 'warning'); return; }
  showLoading('正在回测...');
  try {
    const res = await fetch('/api/finance/backtest', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ file, strategy, fast, slow, close_col })
    });
    const data = await res.json();
    const el = document.getElementById('bt-res');
    const chartBox = document.getElementById('bt-chart');
    if (!el) return;
    if (data.error) { el.innerHTML = '<span style="color:red">' + data.error + '</span>'; return; }
    let html = '<div class="stats" style="display:flex;gap:16px;flex-wrap:wrap;">' +
      '<div class="sc"><div class="sn">' + (data.total_return_pct != null ? data.total_return_pct.toFixed(2) + '%' : '-') + '</div><div class="sl">策略收益</div></div>' +
      '<div class="sc"><div class="sn">' + (data.benchmark_annual_return_pct != null ? data.benchmark_annual_return_pct.toFixed(2) + '%' : '-') + '</div><div class="sl">基准收益</div></div>' +
      '<div class="sc"><div class="sn">' + (data.win_rate_pct != null ? data.win_rate_pct.toFixed(1) + '%' : '-') + '</div><div class="sl">胜率</div></div>' +
      '<div class="sc"><div class="sn">' + (data.max_drawdown_pct != null ? data.max_drawdown_pct.toFixed(2) + '%' : '-') + '</div><div class="sl">最大回撤</div></div>' +
      '<div class="sc"><div class="sn">' + (data.trade_count != null ? data.trade_count : '-') + '</div><div class="sl">交易次数</div></div>' +
      '</div>';
    el.innerHTML = html;
    if (chartBox && data.chart_html) chartBox.innerHTML = data.chart_html;
  } catch (e) {
    showToast('回测失败', 'error');
  } finally {
    hideLoading();
  }
}

async function runPredictEval() {
  const file = getCurrentFile();
  if (!file) { showToast('请先在顶栏选择数据文件', 'warning'); return; }
  const close_col = document.getElementById('eval-close')?.value || undefined;
  const test_size = parseFloat(document.getElementById('eval-test-size')?.value) || 0.3;
  const predict_days = parseInt(document.getElementById('eval-days')?.value) || 30;
  showLoading('正在评估模型（约1分钟）...');
  try {
    const res = await fetch('/api/finance/predict-eval', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ file, close_col, test_size, predict_days })
    });
    const data = await res.json();
    const el = document.getElementById('eval-res');
    const chartBox = document.getElementById('eval-chart');
    if (!el) return;
    if (data.error) { el.innerHTML = '<span style="color:red">' + data.error + '</span>'; return; }
    let html = '';
    if (data.best_model) html += '<div style="margin-bottom:8px;">最佳模型：<b>' + data.best_model + '</b></div>';
    if (data.results) {
      html += '<table style="font-size:13px;width:100%;"><tr><th>模型</th><th>MAE</th><th>MSE</th><th>RMSE</th><th>R²</th></tr>';
      const entries = Array.isArray(data.results) ? data.results.map((r, i) => [r.name || '模型' + (i + 1), r]) : Object.entries(data.results);
      entries.forEach(([name, m]) => {
        html += '<tr><td>' + name + '</td><td>' + (m.mae ?? '-') + '</td><td>' + (m.mse ?? '-') + '</td><td>' + (m.rmse ?? '-') + '</td><td>' + (m.r2 ?? '-') + '</td></tr>';
      });
      html += '</table>';
    }
    el.innerHTML = html || '<span style="color:var(--ink-subtle)">无结果</span>';
    if (chartBox && data.chart) chartBox.innerHTML = '<img src="data:image/png;base64,' + data.chart + '" style="max-width:100%;border-radius:8px;">';
  } catch (e) {
    showToast('评估失败', 'error');
  } finally {
    hideLoading();
  }
}

function refreshModels() {
  fetch('/api/finance/model-list')
    .then(r => r.json())
    .then(data => {
      const el = document.getElementById('saved-model-list');
      if (!el) return;
      if (data.models && data.models.length) {
        el.innerHTML = '<option value="">-- 选择模型 --</option>' + data.models.map(m => {
          const name = typeof m === 'string' ? m : (m.name || m.model_name || JSON.stringify(m));
          return '<option value="' + name + '">' + name + '</option>';
        }).join('');
      } else {
        el.innerHTML = '<option value="">暂无已保存模型</option>';
      }
    })
    .catch(() => showToast('刷新模型列表失败', 'error'));
}


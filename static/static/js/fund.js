// ======================== 基金分析 ========================
function fundPost(url, body) {
  return fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }).then(r => r.json());
}

function fundTable(headers, rows) {
  let h = '<table class="kb-table" style="width:100%;border-collapse:collapse;font-size:13px">';
  h += '<thead><tr>' + headers.map(x => `<th style="text-align:left;padding:6px 8px;border-bottom:1px solid var(--glass-border)">${x}</th>`).join('') + '</tr></thead><tbody>';
  for (const row of rows) {
    h += '<tr>' + row.map(c => `<td style="padding:6px 8px;border-bottom:1px solid var(--glass-border)">${c}</td>`).join('') + '</tr>';
  }
  return h + '</tbody></table>';
}

function fundMetricsTable(d) {
  const rows = [
    ['夏普比率 Sharpe', d.sharpe_ratio, '>1 良好，>2 优秀'],
    ['年化收益率', d.annual_return_pct + '%', '按 252 交易日年化'],
    ['年化波动率', d.volatility_pct + '%', ''],
    ['最大回撤', d.max_drawdown_pct + '%', ''],
    ['Calmar 卡玛比率', d.calmar, '年化收益 / 最大回撤'],
    ['Sortino 索提诺', d.sortino, '下行波动版夏普'],
  ];
  let h = '<div style="font-size:12px;color:var(--ink-subtle);margin-bottom:8px">';
  h += `数据源: ${d.source} | 样本: ${d.rows} 个净值日</div>`;
  h += fundTable(['指标', '数值', '说明'], rows);
  return h;
}

function fundRenderError(out, d) {
  out.innerHTML = `<span style="color:var(--danger)">${(d && d.error) || '计算失败'}</span>`;
}

function fundCalc() {
  const el = document.getElementById('fund-code');
  const out = document.getElementById('fund-out');
  const code = el ? el.value.trim() : '';
  const source = document.getElementById('fund-source').value;
  const rf = (parseFloat(document.getElementById('fund-rf').value) || 2) / 100;
  if (!code) { out.innerHTML = '<span style="color:var(--danger)">请输入基金代码</span>'; return; }
  out.innerHTML = '拉取净值并计算中…（网络取数可能需要几秒）';
  fundPost('/api/finance/fund-metrics', { code, source, rf }).then(d => {
    if (d.error || d.ok === false) { fundRenderError(out, d); return; }
    out.innerHTML = fundMetricsTable(d);
  }).catch(e => out.innerHTML = `<span style="color:var(--danger)">请求失败: ${e}</span>`);
}

function fundCalcCsv() {
  const el = document.getElementById('fund-csv');
  const out = document.getElementById('fund-csv-out');
  const csv = el ? el.value.trim() : '';
  const rf = (parseFloat(document.getElementById('fund-rf').value) || 2) / 100;
  if (!csv) { out.innerHTML = '<span style="color:var(--danger)">请粘贴净值 CSV</span>'; return; }
  out.innerHTML = '计算中…';
  fundPost('/api/finance/fund-metrics', { csv, rf }).then(d => {
    if (d.error || d.ok === false) { fundRenderError(out, d); return; }
    out.innerHTML = fundMetricsTable(d);
  }).catch(e => out.innerHTML = `<span style="color:var(--danger)">请求失败: ${e}</span>`);
}

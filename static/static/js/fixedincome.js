// ======================== 固定收益分析 ========================
function fiPost(url, body) {
  return fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }).then(r => r.json());
}

function fiTable(headers, rows) {
  let h = '<table class="kb-table" style="width:100%;border-collapse:collapse;font-size:13px">';
  h += '<thead><tr>' + headers.map(x => `<th style="text-align:left;padding:6px 8px;border-bottom:1px solid var(--glass-border)">${x}</th>`).join('') + '</tr></thead><tbody>';
  for (const row of rows) {
    h += '<tr>' + row.map(c => `<td style="padding:6px 8px;border-bottom:1px solid var(--glass-border)">${c}</td>`).join('') + '</tr>';
  }
  return h + '</tbody></table>';
}

function fiCalcBond() {
  const body = {
    coupon: parseFloat(document.getElementById('fi-coupon').value),
    ytm: parseFloat(document.getElementById('fi-ytm').value),
    maturity: parseFloat(document.getElementById('fi-maturity').value),
    freq: parseInt(document.getElementById('fi-freq').value) || 2,
    face: parseFloat(document.getElementById('fi-face').value) || 100,
    accrual_frac: parseFloat(document.getElementById('fi-accrual').value) || 0,
  };
  const out = document.getElementById('fi-bond-out');
  out.innerHTML = '计算中…';
  fiPost('/api/fixedincome/bond', body).then(d => {
    if (d.error) { out.innerHTML = `<span style="color:var(--danger)">${d.error}</span>`; return; }
    const m = d.metrics;
    const rows = [
      ['净价(每百元)', m.price], ['应计利息', m.accrued], ['_clean价格', m.clean_price],
      ['麦考利久期(年)', m.macaulay_duration_years], ['修正久期', m.modified_duration],
      ['凸性', m.convexity], ['DV01(每百元)', m.dv01],
    ];
    out.innerHTML = fiTable(['指标', '数值'], rows);
  }).catch(e => out.innerHTML = `<span style="color:var(--danger)">${e}</span>`);
}

function fiCollectBonds() {
  // 优先 CSV 文本，其次逐行录入
  const csv = document.getElementById('fi-port-csv').value.trim();
  if (csv) return { csv };
  const rows = document.getElementById('fi-port-rows').value.trim();
  if (!rows) return { bonds: [] };
  const bonds = [];
  for (const line of rows.split('\n')) {
    const p = line.split(',').map(s => s.trim());
    if (p.length < 4) continue;
    bonds.push({
      name: p[0], coupon: parseFloat(p[1]), ytm: parseFloat(p[2]),
      maturity: parseFloat(p[3]), freq: parseInt(p[4] || 2),
      face: parseFloat(p[5] || 100), market_value: parseFloat(p[6] || 100),
    });
  }
  return { bonds };
}

function fiRenderPortfolio(d) {
  const port = d.portfolio;
  let h = '<div style="margin-bottom:10px"><b>组合汇总</b>　修正久期 <b>' + port.modified_duration +
    '</b>　凸性 <b>' + port.convexity + '</b>　DV01(货币) <b>' + port.dv01_currency +
    '</b>　市值 <b>' + port.market_value + '</b></div>';
  const rows = [];
  for (const b of d.bonds) {
    if (b.error) { rows.push([b.name, '<span style="color:var(--danger)">' + b.error + '</span>', '', '', '', '']); continue; }
    const m = b.metrics;
    rows.push([b.name, b.market_value, m.price, m.modified_duration, m.convexity, m.dv01]);
  }
  h += fiTable(['债券', '市值', '价格', '修正久期', '凸性', 'DV01'], rows);
  return h;
}

function fiCalcPortfolio() {
  const payload = fiCollectBonds();
  const out = document.getElementById('fi-port-out');
  out.innerHTML = '计算中…';
  fiPost('/api/fixedincome/portfolio', payload).then(d => {
    if (d.error) { out.innerHTML = `<span style="color:var(--danger)">${d.error}</span>`; return; }
    out.innerHTML = fiRenderPortfolio(d);
  }).catch(e => out.innerHTML = `<span style="color:var(--danger)">${e}</span>`);
}

function fiAddCurveRow() {
  const row = document.createElement('div');
  row.className = 'grid fi-curve-row';
  row.style.cssText = 'grid-template-columns:1fr 1fr auto;gap:10px;margin-bottom:6px';
  row.innerHTML = '<input placeholder="期限 如 2Y / 10 / 3M" class="cr-tenor"><input placeholder="收益率 %" class="cr-yield" type="number" step="0.01"><button class="btn ghost" onclick="fiRemoveCurveRow(this)">✕</button>';
  document.getElementById('fi-curve-rows').appendChild(row);
}
function fiRemoveCurveRow(btn) { btn.parentNode.remove(); }

function fiCalcCurve() {
  const points = [];
  document.querySelectorAll('#fi-curve-rows .fi-curve-row').forEach(r => {
    const t = r.querySelector('.cr-tenor').value.trim();
    const y = r.querySelector('.cr-yield').value.trim();
    if (t && y !== '') points.push({ tenor: t, yield: parseFloat(y) });
  });
  const out = document.getElementById('fi-curve-out');
  if (!points.length) { out.innerHTML = '<span style="color:var(--danger)">请至少输入 2 个期限点</span>'; return; }
  out.innerHTML = '分析中…';
  fiPost('/api/fixedincome/curve', { points }).then(d => {
    if (d.error) { out.innerHTML = `<span style="color:var(--danger)">${d.error}</span>`; return; }
    let h = '<div style="margin-bottom:8px"><b>形态：</b>' + d.shape + '　<b>短长利差：</b>' + d.short_long_spread + '%</div>';
    const sp = Object.entries(d.key_spreads);
    if (sp.length) {
      const rows = sp.map(([k, v]) => [k, v + '%']);
      h += '<div style="margin-bottom:8px"><b>关键期限利差</b></div>' + fiTable(['利差', '数值'], rows);
    }
    h += '<p style="font-size:12px;color:var(--ink-subtle);margin-top:8px">' + d.note + '</p>';
    out.innerHTML = h;
  }).catch(e => out.innerHTML = `<span style="color:var(--danger)">${e}</span>`);
}

function fiCalcSpread() {
  const body = {
    credit_ytm: parseFloat(document.getElementById('fi-credit').value),
    benchmark_ytm: parseFloat(document.getElementById('fi-bench').value),
    option_cost: parseFloat(document.getElementById('fi-opt').value) || 0,
  };
  const pd = document.getElementById('fi-pd').value.trim();
  const lgd = document.getElementById('fi-lgd').value.trim();
  if (pd !== '') body.pd = parseFloat(pd);
  if (lgd !== '') body.lgd = parseFloat(lgd);
  const out = document.getElementById('fi-spread-out');
  out.innerHTML = '计算中…';
  fiPost('/api/fixedincome/spread', body).then(d => {
    if (d.error) { out.innerHTML = `<span style="color:var(--danger)">${d.error}</span>`; return; }
    const rows = [
      ['信用利差', d.spread + '%'], ['OAS(期权调整利差)', d.oas + '%'],
    ];
    if (d.expected_loss_pd_x_lgd != null) rows.push(['期望损失 PD×LGD', d.expected_loss_pd_x_lgd]);
    let h = fiTable(['指标', '数值'], rows);
    h += '<p style="font-size:12px;color:var(--ink-subtle);margin-top:8px">' + d.assessment + '</p>';
    out.innerHTML = h;
  }).catch(e => out.innerHTML = `<span style="color:var(--danger)">${e}</span>`);
}

function fiCalcStress() {
  const payload = fiCollectBonds();
  const shocks = document.getElementById('fi-shocks').value.split(',').map(s => parseFloat(s.trim())).filter(x => !isNaN(x));
  payload.shocks = shocks;
  const out = document.getElementById('fi-stress-out');
  out.innerHTML = '计算中…';
  fiPost('/api/fixedincome/stress', payload).then(d => {
    if (d.error) { out.innerHTML = `<span style="color:var(--danger)">${d.error}</span>`; return; }
    let h = '<div style="margin-bottom:8px"><b>组合久期</b> ' + d.portfolio_modified_duration +
      '　<b>凸性</b> ' + d.portfolio_convexity + '　<b>市值</b> ' + d.portfolio_market_value + '</div>';
    const rows = d.scenarios.map(s => [s.shock_bp + ' bp', s.delta_pct + '%', s.new_value]);
    h += fiTable(['冲击', '价值变动%', '冲击后价值'], rows);
    out.innerHTML = h;
  }).catch(e => out.innerHTML = `<span style="color:var(--danger)">${e}</span>`);
}

console.log('[fixedincome.js] loaded v=2026082101');

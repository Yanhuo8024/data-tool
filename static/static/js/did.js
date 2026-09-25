// =====================================================================
// did.js — DID 计量经济学分析前端
// =====================================================================

// 全局变量
let g_didColumns = [];
let g_didFile = '';

// 加载列信息
async function loadDIDColumns() {
  const file = getCurrentFile();
  if (!file) { showToast('请先选择文件', 'warning'); return; }
  
  g_didFile = file;
  
  try {
    const r = await fetch('/api/did/columns?file=' + encodeURIComponent(file));
    const data = await r.json();
    
    if (!data.ok) { showToast(data.error, 'error'); return; }
    
    g_didColumns = data;
    
    // 填充下拉框
    const depSel = document.getElementById('did-dep-var');
    const treatSel = document.getElementById('did-treat-col');
    const timeSel = document.getElementById('did-time-col');
    const unitSel = document.getElementById('did-unit-col');
    const heteroVarSel = document.getElementById('did-hetero-var');
    const mediatorSel = document.getElementById('did-mediator');
    const interactVarSel = document.getElementById('did-interact-var');
    const controlsDiv = document.getElementById('did-controls');
    
    // 数值列
    const numCols = data.numeric_columns || [];
    
    if (depSel) {
      depSel.innerHTML = '<option value="">选择变量...</option>' + 
        numCols.map(c => '<option value="' + c + '">' + c + '</option>').join('');
    }
    
    if (treatSel) {
      treatSel.innerHTML = data.all_columns.map(c => '<option value="' + c + '">' + c + '</option>').join('');
    }
    
    if (timeSel) {
      timeSel.innerHTML = data.all_columns.map(c => '<option value="' + c + '">' + c + '</option>').join('');
    }
    
    if (unitSel) {
      unitSel.innerHTML = data.all_columns.map(c => '<option value="' + c + '">' + c + '</option>').join('');
    }
    
    if (heteroVarSel) {
      heteroVarSel.innerHTML = '<option value="">选择变量...</option>' + 
        numCols.map(c => '<option value="' + c + '">' + c + '</option>').join('');
    }
    
    if (mediatorSel) {
      mediatorSel.innerHTML = '<option value="">选择变量...</option>' + 
        numCols.map(c => '<option value="' + c + '">' + c + '</option>').join('');
    }
    
    if (interactVarSel) {
      interactVarSel.innerHTML = '<option value="">选择变量...</option>' + 
        numCols.map(c => '<option value="' + c + '">' + c + '</option>').join('');
    }
    
    if (controlsDiv) {
      controlsDiv.innerHTML = numCols.map(c => 
        '<label style="display:inline-flex;align-items:center;gap:4px;font-size:12px;"><input type="checkbox" class="did-control-cb" value="' + c + '"> ' + c + '</label>'
      ).join('');
    }
    
    showToast('已加载 ' + data.n_rows + ' 行 × ' + data.n_cols + ' 列', 'success');
  } catch (e) {
    showToast('加载失败: ' + e.message, 'error');
  }
}

// 获取选中的控制变量
function getSelectedControls() {
  const checkboxes = document.querySelectorAll('.did-control-cb:checked');
  return Array.from(checkboxes).map(cb => cb.value);
}

// 调用DID API
async function callDIDAPI(url, body, resultDivId) {
  const file = getCurrentFile();
  if (!file) { showToast('请先选择文件', 'warning'); return; }
  
  body.file = file;
  showLoading('分析中...');
  
  try {
    const r = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    });
    const data = await r.json();
    
    const resultDiv = document.getElementById(resultDivId);
    if (!resultDiv) return;
    
    if (!data.ok) {
      resultDiv.innerHTML = '<span style="color:var(--error)">❌ ' + (data.error || '分析失败') + '</span>';
      return;
    }
    
    return data;
  } catch (e) {
    const resultDiv = document.getElementById(resultDivId);
    if (resultDiv) resultDiv.innerHTML = '<span style="color:var(--error)">❌ ' + e.message + '</span>';
  } finally {
    hideLoading();
  }
}

// 1. 数据清洗
async function runDIDClean() {
  const data = await callDIDAPI('/api/did/clean', {}, 'did-clean-result');
  if (data) {
    document.getElementById('did-clean-result').innerHTML = 
      '<pre style="background:rgba(0,0,0,.06);padding:10px 14px;border-radius:6px;font-size:12px;overflow-x:auto;border:1px solid rgba(0,0,0,.08);margin:6px 0;">' + data.report + '</pre>';
  }
}

// 2. 描述性统计
async function runDIDDescriptive() {
  const data = await callDIDAPI('/api/did/descriptive', {
    group_col: document.getElementById('did-treat-col')?.value || null
  }, 'did-descriptive-result');
  if (data) {
    document.getElementById('did-descriptive-result').innerHTML = data.html || '无结果';
  }
}

// 3. 相关性矩阵
async function runDIDCorrelation() {
  const data = await callDIDAPI('/api/did/correlation', {}, 'did-correlation-result');
  if (data) {
    document.getElementById('did-correlation-result').innerHTML = data.html || '无结果';
  }
}

// 4. 基准回归
async function runDIDBaseline() {
  const depVar = document.getElementById('did-dep-var')?.value;
  if (!depVar) { showToast('请选择被解释变量', 'warning'); return; }
  
  const data = await callDIDAPI('/api/did/regression', {
    dep_var: depVar,
    treat_col: document.getElementById('did-treat-col')?.value || 'treat',
    time_col: document.getElementById('did-time-col')?.value || 'year',
    unit_col: document.getElementById('did-unit-col')?.value || 'province',
    control_vars: getSelectedControls()
  }, 'did-baseline-result');
  
  if (data) {
    let html = '<div style="padding:16px;"><h4>📈 基准回归结果</h4>';
    html += '<p style="font-size:12px;color:var(--ink-subtle);">被解释变量: ' + depVar + ' | 观测数: ' + data.n_obs + ' | R²: ' + (data.r2 || '-') + '</p>';
    html += '<table style="width:100%;border-collapse:collapse;font-size:12px;">';
    html += '<tr style="border-bottom:2px solid #333;"><th style="padding:8px;text-align:left;">变量</th><th style="padding:8px;text-align:center;">系数</th><th style="padding:8px;text-align:center;">标准误</th><th style="padding:8px;text-align:center;">t值</th><th style="padding:8px;text-align:center;">p值</th></tr>';
    
    for (const [varName, coef] of Object.entries(data.params || {})) {
      const se = data.se?.[varName] || '-';
      const t = data.tvalues?.[varName] || '-';
      const p = data.pvalues?.[varName] || '-';
      const sig = p < 0.01 ? '***' : p < 0.05 ? '**' : p < 0.1 ? '*' : '';
      html += '<tr style="border-bottom:1px solid #eee;">';
      html += '<td style="padding:6px 8px;">' + varName + '</td>';
      html += '<td style="padding:6px 8px;text-align:center;font-weight:600;">' + (typeof coef === 'number' ? coef.toFixed(4) : coef) + sig + '</td>';
      html += '<td style="padding:6px 8px;text-align:center;">' + (typeof se === 'number' ? se.toFixed(4) : se) + '</td>';
      html += '<td style="padding:6px 8px;text-align:center;">' + (typeof t === 'number' ? t.toFixed(4) : t) + '</td>';
      html += '<td style="padding:6px 8px;text-align:center;">' + (typeof p === 'number' ? p.toFixed(4) : p) + '</td>';
      html += '</tr>';
    }
    html += '</table>';
    html += '<p style="font-size:10px;color:#666;margin-top:8px;">*** p<0.01, ** p<0.05, * p<0.1</p>';
    html += '</div>';
    
    document.getElementById('did-baseline-result').innerHTML = html;
  }
}

// 5. 逐步回归
async function runDIDStepwise() {
  const depVar = document.getElementById('did-dep-var')?.value;
  if (!depVar) { showToast('请选择被解释变量', 'warning'); return; }
  
  const data = await callDIDAPI('/api/did/stepwise', {
    dep_var: depVar,
    treat_col: document.getElementById('did-treat-col')?.value || 'treat',
    time_col: document.getElementById('did-time-col')?.value || 'year',
    unit_col: document.getElementById('did-unit-col')?.value || 'province',
    control_groups: {}
  }, 'did-stepwise-result');
  
  if (data) {
    document.getElementById('did-stepwise-result').innerHTML = data.table_html || '无结果';
  }
}

// 6. 标准化系数
async function runDIDStandardized() {
  const depVar = document.getElementById('did-dep-var')?.value;
  if (!depVar) { showToast('请选择被解释变量', 'warning'); return; }
  
  const data = await callDIDAPI('/api/did/standardized', {
    dep_var: depVar,
    treat_col: document.getElementById('did-treat-col')?.value || 'treat',
    time_col: document.getElementById('did-time-col')?.value || 'year',
    unit_col: document.getElementById('did-unit-col')?.value || 'province',
    control_vars: getSelectedControls()
  }, 'did-standardized-result');
  
  if (data) {
    let html = '<div style="padding:16px;"><h4>📏 标准化系数回归</h4>';
    html += '<table style="width:100%;border-collapse:collapse;font-size:12px;">';
    html += '<tr style="border-bottom:2px solid #333;"><th style="padding:8px;text-align:left;">变量</th><th style="padding:8px;text-align:center;">标准化系数</th><th style="padding:8px;text-align:center;">p值</th></tr>';
    
    for (const [varName, coef] of Object.entries(data.params || {})) {
      const p = data.pvalues?.[varName] || '-';
      const sig = p < 0.01 ? '***' : p < 0.05 ? '**' : p < 0.1 ? '*' : '';
      html += '<tr style="border-bottom:1px solid #eee;">';
      html += '<td style="padding:6px 8px;">' + varName + '</td>';
      html += '<td style="padding:6px 8px;text-align:center;font-weight:600;">' + (typeof coef === 'number' ? coef.toFixed(4) : coef) + sig + '</td>';
      html += '<td style="padding:6px 8px;text-align:center;">' + (typeof p === 'number' ? p.toFixed(4) : p) + '</td>';
      html += '</tr>';
    }
    html += '</table></div>';
    
    document.getElementById('did-standardized-result').innerHTML = html;
  }
}

// 7. 平行趋势检验
async function runDIDParallelTrends() {
  const depVar = document.getElementById('did-dep-var')?.value;
  if (!depVar) { showToast('请选择被解释变量', 'warning'); return; }
  
  const data = await callDIDAPI('/api/did/parallel-trends', {
    dep_var: depVar,
    treat_col: document.getElementById('did-treat-col')?.value || 'treat',
    time_col: document.getElementById('did-time-col')?.value || 'year',
    unit_col: document.getElementById('did-unit-col')?.value || 'province',
    control_vars: getSelectedControls()
  }, 'did-parallel-result');
  
  if (data) {
    const passed = data.passed ? '✅ 通过' : '❌ 未通过';
    let html = '<div style="padding:16px;"><h4>📈 平行趋势检验</h4>';
    html += '<p style="font-size:12px;">政策年份: ' + data.policy_year + ' | 检验结果: <b>' + passed + '</b></p>';
    html += '<table style="width:100%;border-collapse:collapse;font-size:12px;">';
    html += '<tr style="border-bottom:2px solid #333;"><th style="padding:8px;">相对时间</th><th style="padding:8px;">系数</th><th style="padding:8px;">95% CI下限</th><th style="padding:8px;">95% CI上限</th><th style="padding:8px;">p值</th></tr>';
    
    for (const row of data.coef_data || []) {
      html += '<tr style="border-bottom:1px solid #eee;">';
      html += '<td style="padding:6px 8px;text-align:center;">' + row.time + '</td>';
      html += '<td style="padding:6px 8px;text-align:center;font-weight:600;">' + (typeof row.coef === 'number' ? row.coef.toFixed(4) : '-') + '</td>';
      html += '<td style="padding:6px 8px;text-align:center;">' + (typeof row.ci_lower === 'number' ? row.ci_lower.toFixed(4) : '-') + '</td>';
      html += '<td style="padding:6px 8px;text-align:center;">' + (typeof row.ci_upper === 'number' ? row.ci_upper.toFixed(4) : '-') + '</td>';
      html += '<td style="padding:6px 8px;text-align:center;">' + (typeof row.p_value === 'number' ? row.p_value.toFixed(4) : '-') + '</td>';
      html += '</tr>';
    }
    html += '</table></div>';
    
    document.getElementById('did-parallel-result').innerHTML = html;
  }
}

// 8. 安慰剂检验
async function runDIDPlacebo() {
  const depVar = document.getElementById('did-dep-var')?.value;
  if (!depVar) { showToast('请选择被解释变量', 'warning'); return; }
  
  const data = await callDIDAPI('/api/did/placebo', {
    dep_var: depVar,
    treat_col: document.getElementById('did-treat-col')?.value || 'treat',
    time_col: document.getElementById('did-time-col')?.value || 'year',
    unit_col: document.getElementById('did-unit-col')?.value || 'province',
    control_vars: getSelectedControls(),
    n_placebo: 200
  }, 'did-placebo-result');
  
  if (data) {
    const passed = data.passed ? '✅ 通过' : '❌ 未通过';
    let html = '<div style="padding:16px;"><h4>🎲 安慰剂检验</h4>';
    html += '<p style="font-size:12px;">真实系数: <b>' + (typeof data.true_coef === 'number' ? data.true_coef.toFixed(4) : '-') + '</b></p>';
    html += '<p style="font-size:12px;">p值: <b>' + (typeof data.p_value === 'number' ? data.p_value.toFixed(4) : '-') + '</b> | 检验结果: <b>' + passed + '</b></p>';
    html += '<p style="font-size:11px;color:var(--ink-subtle);">随机次数: ' + data.n_placebo + ' | 成功: ' + data.n_success + '</p>';
    html += '</div>';
    
    document.getElementById('did-placebo-result').innerHTML = html;
  }
}

// 9. 子样本回归
async function runDIDSubsample() {
  const depVar = document.getElementById('did-dep-var')?.value;
  if (!depVar) { showToast('请选择被解释变量', 'warning'); return; }
  
  const data = await callDIDAPI('/api/did/subsample', {
    dep_var: depVar,
    treat_col: document.getElementById('did-treat-col')?.value || 'treat',
    time_col: document.getElementById('did-time-col')?.value || 'year',
    unit_col: document.getElementById('did-unit-col')?.value || 'province',
    control_vars: getSelectedControls()
  }, 'did-subsample-result');
  
  if (data) {
    let html = '<div style="padding:16px;"><h4>🔍 子样本回归</h4>';
    html += '<p style="font-size:12px;">观测数: ' + data.n_obs + ' | 个体数: ' + data.n_units + ' | R²: ' + (data.r2 || '-') + '</p>';
    html += '<table style="width:100%;border-collapse:collapse;font-size:12px;">';
    html += '<tr style="border-bottom:2px solid #333;"><th style="padding:8px;text-align:left;">变量</th><th style="padding:8px;text-align:center;">系数</th><th style="padding:8px;text-align:center;">p值</th></tr>';
    
    for (const [varName, coef] of Object.entries(data.params || {})) {
      const p = data.pvalues?.[varName] || '-';
      const sig = p < 0.01 ? '***' : p < 0.05 ? '**' : p < 0.1 ? '*' : '';
      html += '<tr style="border-bottom:1px solid #eee;">';
      html += '<td style="padding:6px 8px;">' + varName + '</td>';
      html += '<td style="padding:6px 8px;text-align:center;font-weight:600;">' + (typeof coef === 'number' ? coef.toFixed(4) : coef) + sig + '</td>';
      html += '<td style="padding:6px 8px;text-align:center;">' + (typeof p === 'number' ? p.toFixed(4) : p) + '</td>';
      html += '</tr>';
    }
    html += '</table></div>';
    
    document.getElementById('did-subsample-result').innerHTML = html;
  }
}

// 10. 异质性分析
async function runDIDHeterogeneity() {
  const depVar = document.getElementById('did-dep-var')?.value;
  const heteroVar = document.getElementById('did-hetero-var')?.value;
  
  if (!depVar) { showToast('请选择被解释变量', 'warning'); return; }
  if (!heteroVar) { showToast('请选择异质性变量', 'warning'); return; }
  
  const data = await callDIDAPI('/api/did/heterogeneity', {
    dep_var: depVar,
    treat_col: document.getElementById('did-treat-col')?.value || 'treat',
    time_col: document.getElementById('did-time-col')?.value || 'year',
    unit_col: document.getElementById('did-unit-col')?.value || 'province',
    control_vars: getSelectedControls(),
    hetero_var: heteroVar,
    split_method: document.getElementById('did-hetero-method')?.value || 'median'
  }, 'did-hetero-result');
  
  if (data) {
    let html = '<div style="padding:16px;"><h4>🔀 异质性分析</h4>';
    html += '<p style="font-size:12px;">异质性变量: ' + data.hetero_var + ' | 阈值: ' + data.threshold + '</p>';
    
    // 高组
    html += '<h5 style="margin-top:12px;">高组 (n=' + data.n_high + ')</h5>';
    html += '<table style="width:100%;border-collapse:collapse;font-size:12px;">';
    html += '<tr style="border-bottom:2px solid #333;"><th style="padding:8px;">变量</th><th style="padding:8px;">系数</th><th style="padding:8px;">p值</th></tr>';
    for (const [varName, coef] of Object.entries(data.high_group?.params || {})) {
      const p = data.high_group?.pvalues?.[varName] || '-';
      const sig = p < 0.01 ? '***' : p < 0.05 ? '**' : p < 0.1 ? '*' : '';
      html += '<tr style="border-bottom:1px solid #eee;"><td style="padding:6px 8px;">' + varName + '</td><td style="padding:6px 8px;text-align:center;">' + (typeof coef === 'number' ? coef.toFixed(4) : coef) + sig + '</td><td style="padding:6px 8px;text-align:center;">' + (typeof p === 'number' ? p.toFixed(4) : p) + '</td></tr>';
    }
    html += '</table>';
    
    // 低组
    html += '<h5 style="margin-top:12px;">低组 (n=' + data.n_low + ')</h5>';
    html += '<table style="width:100%;border-collapse:collapse;font-size:12px;">';
    html += '<tr style="border-bottom:2px solid #333;"><th style="padding:8px;">变量</th><th style="padding:8px;">系数</th><th style="padding:8px;">p值</th></tr>';
    for (const [varName, coef] of Object.entries(data.low_group?.params || {})) {
      const p = data.low_group?.pvalues?.[varName] || '-';
      const sig = p < 0.01 ? '***' : p < 0.05 ? '**' : p < 0.1 ? '*' : '';
      html += '<tr style="border-bottom:1px solid #eee;"><td style="padding:6px 8px;">' + varName + '</td><td style="padding:6px 8px;text-align:center;">' + (typeof coef === 'number' ? coef.toFixed(4) : coef) + sig + '</td><td style="padding:6px 8px;text-align:center;">' + (typeof p === 'number' ? p.toFixed(4) : p) + '</td></tr>';
    }
    html += '</table>';
    
    html += '</div>';
    document.getElementById('did-hetero-result').innerHTML = html;
  }
}

// 11. 中介效应
async function runDIDMediation() {
  const depVar = document.getElementById('did-dep-var')?.value;
  const mediator = document.getElementById('did-mediator')?.value;
  
  if (!depVar) { showToast('请选择被解释变量', 'warning'); return; }
  if (!mediator) { showToast('请选择中介变量', 'warning'); return; }
  
  const data = await callDIDAPI('/api/did/mediation', {
    dep_var: depVar,
    mediator: mediator,
    treat_col: document.getElementById('did-treat-col')?.value || 'treat',
    time_col: document.getElementById('did-time-col')?.value || 'year',
    unit_col: document.getElementById('did-unit-col')?.value || 'province',
    control_vars: getSelectedControls()
  }, 'did-mediation-result');
  
  if (data) {
    let html = '<div style="padding:16px;"><h4>🔗 中介效应分析</h4>';
    html += '<p style="font-size:12px;">中介变量: ' + data.mediator + '</p>';
    html += '<table style="width:100%;border-collapse:collapse;font-size:12px;">';
    html += '<tr style="border-bottom:2px solid #333;"><th style="padding:8px;">效应</th><th style="padding:8px;">系数</th></tr>';
    html += '<tr style="border-bottom:1px solid #eee;"><td style="padding:6px 8px;">总效应 (c)</td><td style="padding:6px 8px;text-align:center;font-weight:600;">' + (typeof data.total_effect === 'number' ? data.total_effect.toFixed(4) : '-') + '</td></tr>';
    html += '<tr style="border-bottom:1px solid #eee;"><td style="padding:6px 8px;">直接效应 (c\')</td><td style="padding:6px 8px;text-align:center;font-weight:600;">' + (typeof data.direct_effect === 'number' ? data.direct_effect.toFixed(4) : '-') + '</td></tr>';
    html += '<tr style="border-bottom:1px solid #eee;"><td style="padding:6px 8px;">间接效应 (a×b)</td><td style="padding:6px 8px;text-align:center;font-weight:600;">' + (typeof data.indirect_effect === 'number' ? data.indirect_effect.toFixed(4) : '-') + '</td></tr>';
    html += '<tr style="border-bottom:1px solid #eee;"><td style="padding:6px 8px;">中介比例</td><td style="padding:6px 8px;text-align:center;font-weight:600;">' + (typeof data.mediation_ratio === 'number' ? (data.mediation_ratio * 100).toFixed(2) + '%' : '-') + '</td></tr>';
    html += '</table>';
    html += '<p style="font-size:11px;color:var(--ink-subtle);">a = Treat→M, b = M→Y, c = Treat→Y (总效应)</p>';
    html += '</div>';
    
    document.getElementById('did-mediation-result').innerHTML = html;
  }
}

// 12. 交互项分析
async function runDIDInteraction() {
  const depVar = document.getElementById('did-dep-var')?.value;
  const interactVar = document.getElementById('did-interact-var')?.value;
  
  if (!depVar) { showToast('请选择被解释变量', 'warning'); return; }
  if (!interactVar) { showToast('请选择调节变量', 'warning'); return; }
  
  const data = await callDIDAPI('/api/did/interaction', {
    dep_var: depVar,
    interact_var: interactVar,
    treat_col: document.getElementById('did-treat-col')?.value || 'treat',
    time_col: document.getElementById('did-time-col')?.value || 'year',
    unit_col: document.getElementById('did-unit-col')?.value || 'province',
    control_vars: getSelectedControls()
  }, 'did-interaction-result');
  
  if (data) {
    let html = '<div style="padding:16px;"><h4>⚡ 交互项分析</h4>';
    html += '<p style="font-size:12px;">调节变量: ' + data.interact_var + '</p>';
    html += '<table style="width:100%;border-collapse:collapse;font-size:12px;">';
    html += '<tr style="border-bottom:2px solid #333;"><th style="padding:8px;">变量</th><th style="padding:8px;">系数</th><th style="padding:8px;">p值</th></tr>';
    
    for (const [varName, coef] of Object.entries(data.params || {})) {
      const p = data.pvalues?.[varName] || '-';
      const sig = p < 0.01 ? '***' : p < 0.05 ? '**' : p < 0.1 ? '*' : '';
      html += '<tr style="border-bottom:1px solid #eee;"><td style="padding:6px 8px;">' + varName + '</td><td style="padding:6px 8px;text-align:center;">' + (typeof coef === 'number' ? coef.toFixed(4) : coef) + sig + '</td><td style="padding:6px 8px;text-align:center;">' + (typeof p === 'number' ? p.toFixed(4) : p) + '</td></tr>';
    }
    html += '</table></div>';
    
    document.getElementById('did-interaction-result').innerHTML = html;
  }
}

// 13. 综合DID分析（一键运行）
async function runFullDIDAnalysis() {
  const depVar = document.getElementById('did-dep-var')?.value;
  if (!depVar) { showToast('请选择被解释变量', 'warning'); return; }
  
  showLoading('综合DID分析中，请稍候...');
  
  try {
    const file = getCurrentFile();
    const r = await fetch('/api/did/full-analysis', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        file: file,
        dep_var: depVar,
        treat_col: document.getElementById('did-treat-col')?.value || 'treat',
        time_col: document.getElementById('did-time-col')?.value || 'year',
        unit_col: document.getElementById('did-unit-col')?.value || 'province',
        control_vars: getSelectedControls()
      })
    });
    const data = await r.json();
    
    const resultDiv = document.getElementById('did-full-result');
    if (!resultDiv) return;
    
    if (!data.ok) {
      resultDiv.innerHTML = '<span style="color:var(--error)">❌ ' + (data.error || '分析失败') + '</span>';
      return;
    }
    
    let html = '<div style="padding:16px;"><h4>🚀 综合DID分析结果</h4>';
    
    if (data.cleaning_report) {
      html += '<div style="margin-top:12px;"><b>📋 数据清洗</b><pre style="background:rgba(0,0,0,.06);padding:10px 14px;border-radius:6px;font-size:12px;">' + data.cleaning_report + '</pre></div>';
    }
    
    if (data.baseline) {
      html += '<div style="margin-top:12px;"><b>📈 基准回归</b><p style="font-size:12px;">R²: ' + data.baseline.r2 + ' | 观测数: ' + data.baseline.n_obs + '</p>';
      html += '<table style="width:100%;border-collapse:collapse;font-size:12px;">';
      html += '<tr style="border-bottom:2px solid #333;"><th style="padding:8px;">变量</th><th style="padding:8px;">系数</th><th style="padding:8px;">p值</th></tr>';
      for (const [varName, coef] of Object.entries(data.baseline.params || {})) {
        const p = data.baseline.pvalues?.[varName] || '-';
        const sig = p < 0.01 ? '***' : p < 0.05 ? '**' : p < 0.1 ? '*' : '';
        html += '<tr style="border-bottom:1px solid #eee;"><td style="padding:6px 8px;">' + varName + '</td><td style="padding:6px 8px;text-align:center;">' + (typeof coef === 'number' ? coef.toFixed(4) : coef) + sig + '</td><td style="padding:6px 8px;text-align:center;">' + (typeof p === 'number' ? p.toFixed(4) : p) + '</td></tr>';
      }
      html += '</table></div>';
    }
    
    if (data.parallel_trends) {
      const passed = data.parallel_trends.passed ? '✅ 通过' : '❌ 未通过';
      html += '<div style="margin-top:12px;"><b>📈 平行趋势</b><p style="font-size:12px;">' + passed + ' | 政策年份: ' + data.parallel_trends.policy_year + '</p></div>';
    }
    
    if (data.placebo) {
      const passed = data.placebo.passed ? '✅ 通过' : '❌ 未通过';
      html += '<div style="margin-top:12px;"><b>🎲 安慰剂检验</b><p style="font-size:12px;">' + passed + ' | p值: ' + data.placebo.p_value + '</p></div>';
    }
    
    html += '</div>';
    resultDiv.innerHTML = html;
    
  } catch (e) {
    document.getElementById('did-full-result').innerHTML = '<span style="color:var(--error)">❌ ' + e.message + '</span>';
  } finally {
    hideLoading();
  }
}

// 14. 导出结果
function exportDIDResults() {
  showToast('导出功能开发中', 'info');
}

// 初始化
document.addEventListener('DOMContentLoaded', function() {
  // 监听DID标签页切换
  const didBtn = document.querySelector('.nbtn[data-t="tab-did"]');
  if (didBtn) {
    didBtn.addEventListener('click', function() {
      setTimeout(loadDIDColumns, 300);
    });
  }
});

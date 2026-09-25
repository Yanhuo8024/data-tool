// =====================================================================
// ml.js — 机器学习/Pipeline/工作区/监控/特征工程
// =====================================================================

// ===== ML =====

async function loadMLModels() {
  try {
    const res = await fetch('/api/ml/catalog');
    const data = await res.json();
    const sel = document.getElementById('ml-model');
    if (!sel) return;
    
    const task = document.getElementById('ml-task-type')?.value || 'regression';
    const taskData = data.catalog?.[task] || data.catalog?.regression;
    const algorithms = taskData?.models || [];
    
    if (algorithms.length > 0) {
      sel.innerHTML = '<option value="">选择模型...</option>' + algorithms.map(a => 
        '<option value="' + a.key + '">' + a.name + ' — ' + a.desc + '</option>'
      ).join('');
    } else {
      sel.innerHTML = '<option value="">选择模型...</option>';
    }
  } catch (e) {
    console.error('Load ML models error:', e);
  }
}

// 初始化时加载模型列表
document.addEventListener('DOMContentLoaded', loadMLModels);

function onMLTaskChange() {
  loadMLModels();
  const task = document.getElementById('ml-task-type')?.value;
  const targetRow = document.getElementById('ml-target-row');
  const clusterRow = document.getElementById('ml-cluster-row');
  if (targetRow) targetRow.style.display = task === 'clustering' ? 'none' : 'grid';
  if (clusterRow) clusterRow.style.display = task === 'clustering' ? 'grid' : 'none';
}

async function loadMLColumns() {
  const file = getCurrentFile();
  if (!file) return;
  fetch('/api/ml/columns?file=' + encodeURIComponent(file))
    .then(r => r.json())
    .then(data => {
      // 填充目标列下拉框
      const targetSel = document.getElementById('ml-target');
      if (targetSel && data.columns) {
        targetSel.innerHTML = '<option value="">选择目标列...</option>' +
          data.columns.map(c => '<option value="' + c + '">' + c + '</option>').join('');
      }
      // 填充特征列多选
      const featsDiv = document.getElementById('ml-features');
      if (featsDiv && data.numeric) {
        featsDiv.innerHTML = data.numeric.map(c =>
          '<label style="display:inline-flex;align-items:center;gap:4px;font-size:12px;"><input type="checkbox" class="ml-feat-cb" value="' + c + '" checked> ' + c + '</label>'
        ).join('');
      }
      // 填充特征工程目标列下拉框
      const feTarget = document.getElementById('fe-target');
      if (feTarget && data.columns) {
        const cur = feTarget.value;
        feTarget.innerHTML = '<option value="">自动选择</option>' +
          data.columns.map(c => '<option value="' + c + '">' + c + '</option>').join('');
        if (cur) feTarget.value = cur;
      }
      // 填充时序特征相关下拉框
      const feDateCol = document.getElementById('fe-date-col');
      if (feDateCol && data.columns) {
        feDateCol.innerHTML = '<option value="">无日期列</option>' +
          data.columns.map(c => '<option value="' + c + '">' + c + '</option>').join('');
      }
      const feNumCols = document.getElementById('fe-num-cols');
      if (feNumCols && data.numeric) {
        feNumCols.innerHTML = data.numeric.map(c =>
          '<label style="display:inline-flex;align-items:center;gap:4px;font-size:12px;"><input type="checkbox" value="' + c + '" checked> ' + c + '</label>'
        ).join('');
      }
    });
}

// 训练结果全局变量（用于预测）
let g_lastModelPath = '';
let g_lastModelName = '';
let g_lastTaskType = '';
let g_lastFeatureCols = [];

async function trainMLModel() {
  const file = getCurrentFile();
  const task = document.getElementById('ml-task-type')?.value;
  const model = document.getElementById('ml-model')?.value;
  const target = document.getElementById('ml-target')?.value;
  const testSize = document.getElementById('ml-test-size')?.value || '0.2';
  const tune = document.getElementById('ml-tune')?.checked || false;

  // 收集选中的特征列
  const featChecks = document.querySelectorAll('.ml-feat-cb:checked');
  const featureCols = Array.from(featChecks).map(cb => cb.value);

  if (!file || !task || !model) { showToast('请选择任务类型和算法模型', 'warning'); return; }
  if (task !== 'clustering' && !target) { showToast('请选择目标列', 'warning'); return; }
  if (featureCols.length === 0) { showToast('请至少选择一个特征列', 'warning'); return; }

  showLoading('正在训练...');
  try {
    const res = await fetch('/api/ml/train', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        file_name: file,
        task_type: task,
        model_name: model,
        feature_cols: featureCols,
        target_col: target,
        test_size: parseFloat(testSize),
        tune_hyperparams: tune
      })
    });
    const data = await res.json();
    displayMLResults(data);
  } catch (e) {
    showToast('训练失败: ' + e.message, 'error');
  } finally {
    hideLoading();
  }
}

function displayMLResults(data) {
  const resultsCard = document.getElementById('ml-results');
  const modelSub = document.getElementById('ml-result-model');
  const metricsDiv = document.getElementById('ml-metrics');
  const chartsDiv = document.getElementById('ml-charts');

  if (!resultsCard || !metricsDiv) return;

  // 显示结果卡片
  resultsCard.classList.remove('hide');

  if (data.error) {
    modelSub.textContent = '训练失败';
    metricsDiv.innerHTML = '<span style="color:var(--error)">' + (data.error || '未知错误') + '</span>';
    if (chartsDiv) chartsDiv.innerHTML = '';
    return;
  }

  // 保存模型信息用于预测
  g_lastModelPath = data.model_path || '';
  g_lastModelName = data.model_name || '';
  g_lastTaskType = data.task_type || '';
  g_lastFeatureCols = data.feature_cols || [];

  // 模型名称
  modelSub.textContent = (data.model_name || '模型') + ' — ' + (data.task_type || '');

  // 指标
  let html = '<div class="stats">';
  if (data.metrics) {
    for (const [k, v] of Object.entries(data.metrics)) {
      html += '<div class="sc"><div class="sn">' + (typeof v === 'number' ? Number(v).toFixed(4) : v) + '</div><div class="sl">' + k + '</div></div>';
    }
  }
  html += '</div>';

  // 特征重要性
  if (data.feature_importance) {
    html += '<div style="margin-top:12px;"><b>特征重要性</b><div style="display:flex;flex-wrap:wrap;gap:6px;margin-top:6px;">';
    const sorted = Object.entries(data.feature_importance).sort((a, b) => b[1] - a[1]);
    for (const [f, imp] of sorted.slice(0, 15)) {
      html += '<span style="padding:4px 8px;background:rgba(99,102,241,0.1);border-radius:4px;font-size:12px;">' + f + ' ' + Number(imp).toFixed(3) + '</span>';
    }
    html += '</div></div>';
  }

  metricsDiv.innerHTML = html || '<span style="color:var(--ink-subtle)">无结果</span>';

  // 图表
  if (chartsDiv && data.chart) {
    chartsDiv.innerHTML = '<img src="data:image/png;base64,' + data.chart + '" style="max-width:100%;border-radius:8px;margin-top:12px;">';
  } else if (chartsDiv) {
    chartsDiv.innerHTML = '';
  }

  // 更新预测表单
  updatePredictForm();

  // 滚动到结果
  resultsCard.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

function updatePredictForm() {
  const form = document.getElementById('ml-predict-form');
  if (!form) return;
  if (!g_lastModelPath) {
    form.innerHTML = '<p class="tm">请先训练模型</p>';
    return;
  }
  let html = '<div class="fg s2"><label>已训练模型</label><div style="padding:8px;background:rgba(99,102,241,0.1);border-radius:6px;font-size:12px;">' + g_lastModelName + '</div></div>';
  html += '<div class="fg s2"><label>输入特征值 (JSON)</label><textarea id="ml-predict-input" style="width:100%;height:80px;font-family:monospace;font-size:12px;" placeholder=\'{"年份": 2025, "生存型消费占比（%）": 30}\'></textarea></div>';
  html += '<button class="btn btn-p" onclick="doMLPredict()">预测</button>';
  form.innerHTML = html;
}

async function doMLPredict() {
  if (!g_lastModelPath) { showToast('请先训练模型', 'warning'); return; }
  const inputEl = document.getElementById('ml-predict-input');
  let inputData = {};
  try {
    inputData = JSON.parse(inputEl?.value || '{}');
  } catch (e) {
    showToast('JSON 格式错误', 'error');
    return;
  }
  showLoading('预测中...');
  try {
    const res = await fetch('/api/ml/predict', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ model_path: g_lastModelPath, input_data: inputData })
    });
    const data = await res.json();
    if (data.error) { showToast(data.error, 'error'); return; }
    showToast('预测结果: ' + (data.predictions?.[0]?.toFixed?.(2) || data.predictions), 'success');
  } catch (e) {
    showToast('预测失败: ' + e.message, 'error');
  } finally {
    hideLoading();
  }
}

function loadMLCatalog() {
  fetch('/api/ml/catalog')
    .then(r => r.json())
    .then(data => {
      const el = document.getElementById('ml-catalog');
      if (el && data.catalog) el.innerHTML = '<pre>' + JSON.stringify(data.catalog, null, 2) + '</pre>';
    });
}

async function runFeatureEng() {
  const resDiv = document.getElementById('fe-res');
  const chartDiv = document.getElementById('fe-chart');
  if (!resDiv) return showToast('特征工程区域不存在', 'warning');
  resDiv.innerHTML = '<span class="spin" style="display:inline-block;margin-right:6px;"></span>正在执行特征工程...';
  if (chartDiv) chartDiv.innerHTML = '';
  try {
    const r = await fetch('/api/ml/feature-engineering', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({
        target_col: document.getElementById('fe-target').value || null,
        task_type: document.getElementById('fe-task').value,
        select_k: parseInt(document.getElementById('fe-select-k').value) || 10,
        select_method: document.getElementById('fe-method').value || 'f_test',
        use_std: document.getElementById('fe-std').checked,
        use_norm: document.getElementById('fe-norm').checked,
      })
    });
    const d = await r.json();
    if (d.error || d.status === 'failed') {
      resDiv.innerHTML = '<span style="color:var(--error)">' + (d.error || '执行失败') + '</span>';
      return;
    }
    let html = '<div class="stats">';
    html += '<div class="sc"><div class="sn">' + (d.total_features || 0) + '</div><div class="sl">总特征数</div></div>';
    html += '<div class="sc"><div class="sn">' + (d.selected_features || []).length + '</div><div class="sl">选中特征</div></div>';
    html += '<div class="sc"><div class="sn">' + (d.low_variance_removed || []).length + '</div><div class="sl">低方差剔除</div></div>';
    html += '</div>';
    html += '<div style="margin-top:10px;display:flex;flex-wrap:wrap;gap:6px;">';
    (d.selected_features || []).slice(0, 15).forEach(f => {
      const score = d.feature_scores ? (d.feature_scores[f] || 0) : 0;
      html += '<div style="padding:5px 10px;background:rgba(99,102,241,0.1);border-radius:6px;font-size:12px;">' + f + ' <span style="color:var(--ink-muted);">' + Number(score).toFixed(3) + '</span></div>';
    });
    html += '</div>';
    resDiv.innerHTML = html;
    if (d.chart && chartDiv) chartDiv.innerHTML = '<img src="data:image/png;base64,' + d.chart + '" style="max-width:100%;border-radius:8px;">';
    showToast('特征工程完成：选中 ' + (d.selected_features || []).length + ' 个特征', 'success');
  } catch (e) {
    resDiv.innerHTML = '<span style="color:var(--error)">' + e.message + '</span>';
    showToast('特征工程失败', 'error');
  }
}
function loadEvalColumns() {}
function runEvalPanel() { showToast('评估面板开发中', 'info'); }

// ===== Spearman 秩相关 =====

function loadSpearmanColumns() {
  const sel = document.getElementById('spearman-cols');
  if (!sel) return;
  sel.innerHTML = '<option value="">加载中...</option>';
  const file = getCurrentFile();
  if (!file) { sel.innerHTML = '<option value="">请先选择数据文件</option>'; return; }
  fetch('/api/ml/columns?file=' + encodeURIComponent(file)).then(r => r.json()).then(data => {
    const cols = Array.isArray(data) ? data : (data.columns || []);
    sel.innerHTML = cols.length
      ? cols.map(c => '<option value="' + esc(c) + '">' + esc(c) + '</option>').join('')
      : '<option value="">无可用列</option>';
  }).catch(() => { sel.innerHTML = '<option value="">加载失败</option>'; });
}

async function runSpearman() {
  const file = getCurrentFile();
  const sel = document.getElementById('spearman-cols');
  const cols = sel ? Array.from(sel.selectedOptions).map(o => o.value).filter(v => v) : [];
  const resEl = document.getElementById('spearman-res');
  if (!file) { showToast('请先选择数据文件', 'warning'); return; }
  if (cols.length < 2) { showToast('请至少选择 2 个数值列', 'warning'); return; }
  showLoading('正在计算 Spearman 秩相关系数...');
  try {
    const r = await fetch('/api/ml/spearman', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ file: file, cols: cols })
    });
    const d = await r.json();
    hideLoading();
    if (d.error) { showToast(d.error, 'error'); resEl.innerHTML = '<div style="color:var(--error);">' + esc(d.error) + '</div>'; return; }

    var html = '<div style="font-size:12px;color:var(--ink-subtle);margin-bottom:8px;">样本量: ' + d.n_samples + ' · 数值列: ' + d.cols.join(' / ') + '</div>';
    if (d.conclusion && d.conclusion.length) {
      html += '<div style="margin-bottom:10px;padding:10px 12px;background:rgba(94,106,210,.08);border-radius:8px;font-size:13px;line-height:1.9;">📌 ' + d.conclusion.join('<br>') + '</div>';
    }
    if (d.chart) {
      html += '<div style="text-align:center;"><img src="' + d.chart + '" style="max-width:100%;border-radius:8px;border:1px solid var(--glass-border);"></div>';
    }
    if (d.pairs && d.pairs.length) {
      html += '<table class="tb" style="margin-top:10px;font-size:12px;"><thead><tr><th>列 A</th><th>列 B</th><th>ρ (Spearman)</th><th>p 值</th><th>显著性</th></tr></thead><tbody>';
      d.pairs.forEach(function(p) {
        var cls = Math.abs(p.rho) > 0.7 ? 'var(--success)' : Math.abs(p.rho) > 0.4 ? 'var(--warning)' : 'var(--ink-subtle)';
        var sig = p.p < 0.001 ? '***' : p.p < 0.01 ? '**' : p.p < 0.05 ? '*' : '';
        html += '<tr><td class="mono">' + esc(p.col1) + '</td><td class="mono">' + esc(p.col2) + '</td>' +
                '<td style="color:' + cls + ';font-weight:600;">' + p.rho.toFixed(4) + '</td>' +
                '<td class="mono">' + p.p.toFixed(6) + '</td><td>' + sig + '</td></tr>';
      });
      html += '</tbody></table>';
    }
    resEl.innerHTML = html;
    // 同步到会话报告
    saveChartToReport('', 'Spearman 特征相关性', d.n_samples + ' 样本 · ' + d.cols.length + ' 列', '机器学习', '<img src="' + d.chart + '" style="max-width:100%;">');
    if (d.conclusion && d.conclusion.length) saveConclusionToReport('📊 Spearman 相关性', d.conclusion.join('；'), '机器学习');
  } catch (e) {
    hideLoading();
    showToast('计算失败: ' + e.message, 'error');
    console.error('spearman:', e);
  }
}

// ===== Pipeline =====

function loadPipelineList() {
  fetch('/api/pipeline/list').then(r => r.json()).then(data => {
    const el = document.getElementById('pipe-list');
    if (!el) return;
    const pipes = Array.isArray(data) ? data : (data.pipelines || []);
    const cnt = document.getElementById('pipe-count');
    if (cnt) cnt.textContent = pipes.length + ' 个流水线';
    el.innerHTML = pipes.length
      ? pipes.map(p => {
          const nodes = (p.nodes || []).map(n => n.type || n.node_type || '?').join(' → ');
          return '<div style="padding:10px 12px;border:1px solid rgba(128,128,128,.15);border-radius:8px;display:flex;justify-content:space-between;align-items:center;gap:10px;">' +
            '<div style="flex:1;min-width:0;">' +
            '<div style="font-weight:600;">' + esc(p.name || p.id) + '</div>' +
            '<div style="font-size:11px;color:var(--ink-subtle);margin-top:2px;">' + (nodes || '无节点') +
            ' · ' + (p.last_status || '未运行') + '</div></div>' +
            '<div style="display:flex;gap:6px;white-space:nowrap;">' +
            '<button class="btn btn-s btn-sm" onclick="openPipeEditor(\'' + p.id + '\')">编辑</button>' +
            '<button class="btn btn-p btn-sm" onclick="runPipelineById(\'' + p.id + '\')">▶ 运行</button>' +
            '<button class="btn btn-w btn-sm" onclick="deletePipelineById(\'' + p.id + '\')">删除</button>' +
            '</div></div>';
        }).join('')
      : '<div style="color:var(--ink-subtle);padding:8px;">暂无流水线，点击「新建流水线」创建</div>';
  }).catch(e => console.error('pipeline list:', e));
}

function createPipeline() {
  const name = prompt('Pipeline 名称:');
  if (!name) return;
  fetch('/api/pipeline/create', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name }) })
    .then(r => r.json())
    .then(d => {
      if (d && d.id) { showToast('✅ 流水线已创建', 'success'); loadPipelineList(); openPipeEditor(d.id); }
      else { showToast(d && d.error ? d.error : '创建失败', 'error'); }
    })
    .catch(e => showToast('创建失败: ' + e.message, 'error'));
}

let _pipeEditId = null;

async function openPipeEditor(pid) {
  try {
    const r = await fetch('/api/pipeline/get?id=' + encodeURIComponent(pid));
    const p = await r.json();
    if (p.error) { showToast(p.error, 'error'); return; }
    _pipeEditId = pid;
    const idEl = document.getElementById('pipe-edit-id');
    const nameEl = document.getElementById('pipe-edit-name');
    const descEl = document.getElementById('pipe-edit-desc');
    if (idEl) idEl.value = pid;
    if (nameEl) nameEl.value = p.name || '';
    if (descEl) descEl.value = p.description || '';
    const title = document.getElementById('pipe-edit-title');
    if (title) title.textContent = '编辑: ' + (p.name || pid);
    renderPipeNodes(p.nodes || []);
    const editor = document.getElementById('pipe-editor');
    if (editor) editor.style.display = 'block';
  } catch (e) {
    showToast('加载流水线失败', 'error');
  }
}

function renderPipeNodes(nodes) {
  const el = document.getElementById('pipe-nodes');
  if (!el) return;
  el.innerHTML = nodes.length
    ? nodes.map(n => {
        const nid = n.id || n.node_id || '';
        return '<div style="display:flex;align-items:center;gap:8px;padding:6px 8px;border-bottom:1px solid rgba(128,128,128,.12);font-size:13px;">' +
          '<span style="background:rgba(69,183,209,.15);color:var(--primary);padding:2px 8px;border-radius:4px;font-size:11px;">' + esc(n.type || n.node_type || '?') + '</span>' +
          '<span style="flex:1;">' + esc(nid) + '</span>' +
          '<button class="btn btn-w btn-sm" onclick="removePipeNode(\'' + nid + '\')">✕</button></div>';
      }).join('')
    : '<div style="color:var(--ink-subtle);font-size:12px;padding:4px 0;">暂无节点，从下方选择类型添加</div>';
}

async function addNodeToEditor() {
  if (!_pipeEditId) { showToast('请先打开一个流水线', 'warning'); return; }
  const nodeType = document.getElementById('new-node-type')?.value;
  if (!nodeType) { showToast('请选择节点类型', 'warning'); return; }
  try {
    const r = await fetch('/api/pipeline/node/add', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ pipeline_id: _pipeEditId, node_type: nodeType, config: {} })
    });
    const d = await r.json();
    if (d.error) { showToast(d.error, 'error'); return; }
    showToast('节点已添加: ' + nodeType, 'success');
    openPipeEditor(_pipeEditId); // 刷新节点列表
    loadPipelineList();
  } catch (e) {
    showToast('添加节点失败', 'error');
  }
}

async function removePipeNode(nid) {
  if (!_pipeEditId || !nid) return;
  try {
    const r = await fetch('/api/pipeline/node/remove', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ pipeline_id: _pipeEditId, node_id: nid })
    });
    const d = await r.json();
    if (d.error) { showToast(d.error, 'error'); return; }
    openPipeEditor(_pipeEditId);
    loadPipelineList();
  } catch (e) {
    showToast('删除节点失败', 'error');
  }
}

async function savePipeline() {
  if (!_pipeEditId) { showToast('未打开流水线', 'warning'); return; }
  // 后端无 update 名称接口；节点增删已即时生效，此处仅提示并刷新
  showToast('节点改动已即时保存', 'success');
  openPipeEditor(_pipeEditId);
  loadPipelineList();
}

function closePipeEditor() {
  const editor = document.getElementById('pipe-editor');
  if (editor) editor.style.display = 'none';
  _pipeEditId = null;
}

function runPipeline() { if (_pipeEditId) runPipelineById(_pipeEditId); else showToast('请先打开流水线', 'warning'); }
function deletePipeline() { if (_pipeEditId) deletePipelineById(_pipeEditId); else showToast('请先打开流水线', 'warning'); }

async function runPipelineById(pid) {
  showLoading('正在运行 Pipeline...');
  const logsEl = document.getElementById('pipe-run-logs');
  try {
    const r = await fetch('/api/pipeline/run', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id: pid })
    });
    const d = await r.json();
    hideLoading();
    if (d.error) { showToast(d.error, 'error'); return; }
    if (logsEl) {
      logsEl.style.display = 'block';
      logsEl.textContent = (d.logs || []).map(l => typeof l === 'string' ? l : JSON.stringify(l)).join('\n') || '运行完成（无日志）';
    }
    showToast('Pipeline 运行完成', 'success');
    loadPipelineList();
  } catch (e) {
    hideLoading();
    showToast('运行失败', 'error');
  }
}

async function deletePipelineById(pid) {
  if (!confirm('确定删除此流水线？')) return;
  try {
    const r = await fetch('/api/pipeline/delete', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id: pid })
    });
    const d = await r.json();
    if (d.error) { showToast(d.error, 'error'); return; }
    showToast('已删除', 'success');
    if (_pipeEditId === pid) closePipeEditor();
    loadPipelineList();
  } catch (e) {
    showToast('删除失败', 'error');
  }
}

// ===== Feature Engineering =====

// 解析特征工程表单 → 公共参数
function _feFormParams() {
  const file = (typeof getCurrentFile === 'function' ? getCurrentFile() : '') || '';
  const dateCol = document.getElementById('fe-date-col')?.value || undefined;
  const numCols = Array.from(document.querySelectorAll('#fe-num-cols input:checked')).map(c => c.value);
  const catCols = Array.from(document.querySelectorAll('#fe-cat-cols input:checked')).map(c => c.value);
  const parseList = (id, def) => {
    const v = document.getElementById(id)?.value?.trim();
    return v ? v.split(',').map(s => parseInt(s.trim(), 10)).filter(n => !isNaN(n)) : def;
  };
  return {
    file, date_col: dateCol || undefined,
    num_cols: numCols.length ? numCols : undefined,
    cat_cols: catCols.length ? catCols : undefined,
    lags: parseList('fe-lags', [1, 2, 3]),
    windows: parseList('fe-windows', [5, 10]),
    periods: parseList('fe-periods', [1, 4]),
    ops: Array.from(document.getElementById('fe-ops')?.selectedOptions || []).map(o => o.value),
  };
}

async function _genFeatures(url, label, loadingMsg, successFmt) {
  const p = _feFormParams();
  if (!p.file) { showToast('请先选择数据文件', 'warning'); return; }
  showLoading(loadingMsg);
  try {
    const r = await fetch(url, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(p)
    });
    const d = await r.json();
    const el = document.getElementById('fe-new-res');
    if (d.error) { if (el) el.innerHTML = '<span style="color:red">' + d.error + '</span>'; showToast(d.error, 'error'); return; }
    if (el) el.innerHTML = '<div style="font-size:13px;">' + successFmt(d) + '</div>';
    showToast(label + '生成完成', 'success');
  } catch (e) {
    showToast(label + '失败', 'error');
  } finally {
    hideLoading();
  }
}

function genTSFeatures() {
  return _genFeatures('/api/feature/ts', '时序特征', '正在生成时序特征...',
    d => '✅ 时序特征已生成：' + d.file + '（' + d.new_cols + ' 列），可在顶栏切换该文件继续分析');
}

function genCrossFeatures() {
  return _genFeatures('/api/feature/cross', '交叉特征', '正在生成交叉特征...',
    d => '✅ 交叉特征已生成：' + d.file + '（' + d.new_cols + ' 列）');
}

function genAllFeatures() {
  return _genFeatures('/api/feature/auto', '特征工程', '正在一键特征工程（可能需要 1 分钟）...',
    d => '✅ 一键特征工程完成：' + d.file + '（原始 ' + d.orig_cols + ' 列 → ' + d.new_cols + ' 列）');
}

// ===== 模型评估面板 =====

async function loadEvalColumns() {
  const file = (typeof getCurrentFile === 'function' ? getCurrentFile() : '') || '';
  if (!file) { showToast('请先选择数据文件', 'warning'); return; }
  try {
    const r = await fetch('/api/ml/columns?file=' + encodeURIComponent(file));
    const d = await r.json();
    if (d.error) { showToast(d.error, 'error'); return; }
    const all = d.columns || [];
    const numeric = d.numeric || [];
    // 目标列下拉
    const targetSel = document.getElementById('eval-target');
    if (targetSel) {
      targetSel.innerHTML = '<option value="">选择目标列...</option>' +
        all.map(c => '<option value="' + c + '">' + c + '</option>').join('');
    }
    // 特征列复选框
    const featBox = document.getElementById('eval-features');
    if (featBox) {
      featBox.innerHTML = numeric.map(c =>
        '<label class="chk" style="font-size:12px;"><input type="checkbox" class="eval-feat-cb" value="' + c + '"> ' + c + '</label>'
      ).join('') || '<span style="color:var(--ink-subtle);font-size:12px;">无数值列</span>';
    }
    showToast('列已加载（' + numeric.length + ' 个数值列）', 'success');
  } catch (e) {
    showToast('加载列失败', 'error');
  }
}

async function runEvalPanel() {
  const file = (typeof getCurrentFile === 'function' ? getCurrentFile() : '') || '';
  const target = document.getElementById('eval-target')?.value;
  const taskType = document.getElementById('eval-task')?.value || 'classification';
  const modelName = document.getElementById('eval-model')?.value || 'logistic';
  const testSize = parseFloat(document.getElementById('eval-ts')?.value) || 0.3;
  const features = Array.from(document.querySelectorAll('.eval-feat-cb:checked')).map(c => c.value);
  if (!file) { showToast('请先选择数据文件', 'warning'); return; }
  if (!target) { showToast('请选择目标列', 'warning'); return; }
  if (!features.length) { showToast('请选择至少一个特征列', 'warning'); return; }
  showLoading('正在生成评估面板（训练中）...');
  try {
    const r = await fetch('/api/ml/evaluation-panel', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ file, target_col: target, feature_cols: features, task_type: taskType, test_size: testSize, model_name: modelName })
    });
    const d = await r.json();
    const resEl = document.getElementById('eval-panel-res');
    const chartEl = document.getElementById('eval-panel-chart');
    if (d.error) { if (resEl) resEl.innerHTML = '<span style="color:red">' + d.error + '</span>'; showToast(d.error, 'error'); return; }
    // 指标摘要
    if (resEl) {
      let html = '<div style="font-size:13px;">模型: <b>' + esc(modelName) + '</b> · 目标: ' + esc(target) + '</div>';
      const metrics = d.metrics || d;
      html += '<pre style="font-size:12px;background:rgba(0,0,0,.15);padding:10px;border-radius:6px;overflow:auto;">' +
        esc(JSON.stringify(metrics, null, 2).slice(0, 2000)) + '</pre>';
      resEl.innerHTML = html;
    }
    // 图表（base64 列表）
    if (chartEl && d.charts && d.charts.length) {
      chartEl.innerHTML = d.charts.map(c =>
        typeof c === 'string'
          ? '<img src="data:image/png;base64,' + c + '" style="max-width:100%;border-radius:8px;margin:6px 0;">'
          : (c.data ? '<img src="data:image/png;base64,' + c.data + '" style="max-width:100%;border-radius:8px;margin:6px 0;">' : '')
      ).join('');
    }
    showToast('评估面板生成完成', 'success');
  } catch (e) {
    showToast('评估面板失败', 'error');
  } finally {
    hideLoading();
  }
}

// ============ 模型版本管理 ============
function _mlEsc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
    return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
  });
}

async function loadMLVersions() {
  const el = document.getElementById('ml-version-table');
  if (!el) return;
  try {
    const r = await fetch('/api/ml/models/versions');
    const d = await r.json();
    if (!d.ok) { showToast(d.error || '加载失败', 'error'); return; }
    if (!d.models || !d.models.length) {
      el.innerHTML = '<div style="color:#888;font-size:12px;padding:8px;">暂无已保存模型，训练完成后自动生成版本（含指标元数据）。</div>';
      return;
    }
    const rows = d.models.map(function (m) {
      const ms = m.metrics || {};
      const keys = Object.keys(ms).filter(function (k) { return k !== 'feature_importance'; }).slice(0, 4);
      const short = keys.map(function (k) {
        const v = ms[k];
        return k + '=' + (typeof v === 'number' ? v.toFixed(4) : (v == null ? '—' : v));
      }).join(', ');
      return '<tr>' +
        '<td style="padding:6px 8px;"><input type="checkbox" class="ml-ver-cb" value="' + _mlEsc(m.model_path) + '"></td>' +
        '<td style="padding:6px 8px;font-size:12px;">' + (m.current ? '<span style="color:#FF6B6B;font-weight:600;">● 当前</span> ' : '') + _mlEsc(m.model_name) + '</td>' +
        '<td style="padding:6px 8px;font-size:12px;">' + _mlEsc(m.task_type) + '</td>' +
        '<td style="padding:6px 8px;font-size:12px;">' + _mlEsc(m.file_name || '') + '</td>' +
        '<td style="padding:6px 8px;font-size:12px;color:#888;max-width:220px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">' + _mlEsc(short || '—') + '</td>' +
        '<td style="padding:6px 8px;font-size:12px;color:#888;white-space:nowrap;">' + _mlEsc(m.created_at) + '</td>' +
        '<td style="padding:6px 8px;white-space:nowrap;">' +
        '<button class="btn btn-s btn-sm" onclick="setMLCurrent(\'' + _mlEsc(m.model_path) + '\')">设为当前</button>' +
        '<button class="btn btn-g btn-sm" style="margin-left:6px;" onclick="compareMLVersions(\'' + _mlEsc(m.model_path) + '\')">详情</button>' +
        '</td></tr>';
    }).join('');
    el.innerHTML = '<table style="width:100%;border-collapse:collapse;font-size:12px;">' +
      '<thead><tr style="text-align:left;color:#888;border-bottom:1px solid rgba(0,0,0,.08);">' +
      '<th style="padding:6px 8px;width:30px;"></th><th style="padding:6px 8px;">模型</th>' +
      '<th style="padding:6px 8px;">任务</th><th style="padding:6px 8px;">数据文件</th>' +
      '<th style="padding:6px 8px;">指标摘要</th><th style="padding:6px 8px;">训练时间</th>' +
      '<th style="padding:6px 8px;">操作</th></tr></thead><tbody>' + rows + '</tbody></table>';
  } catch (e) { showToast('加载版本失败: ' + e.message, 'error'); }
}

async function setMLCurrent(path) {
  if (!confirm('确定将该版本设为当前活动模型？（预测将默认使用此版本）')) return;
  try {
    const r = await fetch('/api/ml/models/rollback', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ model_path: path })
    });
    const d = await r.json();
    showToast(d.message || d.error || '完成', d.ok ? 'success' : 'error');
    if (d.ok) loadMLVersions();
  } catch (e) { showToast('操作失败: ' + e.message, 'error'); }
}

async function compareMLVersions(singlePath) {
  const el = document.getElementById('ml-compare-res');
  if (!el) return;
  let a = null, b = null;
  if (singlePath) {
    a = singlePath;
  } else {
    const cbs = document.querySelectorAll('.ml-ver-cb:checked');
    if (cbs.length < 2) { showToast('请勾选两个模型版本进行对比', 'info'); return; }
    a = cbs[0].value; b = cbs[1].value;
  }
  try {
    const r = await fetch('/api/ml/models/compare', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ a: a, b: b || a })
    });
    const d = await r.json();
    if (!d.ok) { showToast(d.error || '对比失败', 'error'); return; }
    let html = '<div style="margin-top:10px;font-size:12px;color:#888;">' +
      '对比：<b style="color:var(--ink,#333);">' + _mlEsc(d.a.name) + '</b>（' + _mlEsc(d.a.created_at) + '）' +
      ' vs <b style="color:var(--ink,#333);">' + _mlEsc(d.b.name) + '</b>（' + _mlEsc(d.b.created_at) + '）</div>';
    if (!d.rows || !d.rows.length) {
      html += '<div style="margin-top:8px;color:#aaa;">两个版本无可用指标</div>';
    } else {
      html += '<table style="width:100%;border-collapse:collapse;font-size:12px;margin-top:8px;">' +
        '<thead><tr style="text-align:left;color:#888;border-bottom:1px solid rgba(0,0,0,.08);">' +
        '<th style="padding:6px 8px;">指标</th><th style="padding:6px 8px;">' + _mlEsc(d.a.name) + '</th>' +
        '<th style="padding:6px 8px;">' + _mlEsc(d.b.name) + '</th></tr></thead><tbody>' +
        d.rows.map(function (r2) {
          return '<tr><td style="padding:6px 8px;">' + _mlEsc(r2.metric) + '</td>' +
            '<td style="padding:6px 8px;">' + _mlEsc(r2.a) + '</td>' +
            '<td style="padding:6px 8px;">' + _mlEsc(r2.b) + '</td></tr>';
        }).join('') +
        '</tbody></table>';
    }
    el.innerHTML = html;
  } catch (e) { showToast('对比失败: ' + e.message, 'error'); }
}


// ============ 分析插件（图表/模型扩展） ============
let _selectedPlugin = null;

function togglePluginUpload() {
  const box = document.getElementById('plugin-upload-box');
  if (box) box.classList.toggle('hide');
}

async function loadPlugins() {
  const el = document.getElementById('plugin-list');
  if (!el) return;
  try {
    const r = await fetch('/api/plugins');
    const d = await r.json();
    if (!d.ok) { showToast(d.error || '加载失败', 'error'); return; }
    if (!d.plugins.length) {
      el.innerHTML = '<div style="color:#888;font-size:12px;padding:8px;">暂无插件，点击"上传新插件"安装。</div>';
      return;
    }
    const rows = d.plugins.map(function (p) {
      const err = p.error ? '<span style="color:#FF6B6B;font-size:11px;">⚠ ' + _mlEsc(p.error) + '</span>' : '';
      return '<tr>' +
        '<td style="padding:6px 8px;"><input type="radio" name="plugin-pick" value="' + _mlEsc(p.name) + '" onchange="selectPlugin(this.value)"></td>' +
        '<td style="padding:6px 8px;font-size:12px;font-weight:600;">' + _mlEsc(p.name || p.file) + '</td>' +
        '<td style="padding:6px 8px;font-size:12px;">' + _mlEsc(p.type || '') + '</td>' +
        '<td style="padding:6px 8px;font-size:12px;color:#888;">' + _mlEsc(p.description || '') + '</td>' +
        '<td style="padding:6px 8px;font-size:12px;color:#888;">' + _mlEsc(p.version || '') + '</td>' +
        '<td style="padding:6px 8px;font-size:12px;">' + err + '</td>' +
        '<td style="padding:6px 8px;white-space:nowrap;"><button class="btn btn-s btn-sm" onclick="deletePlugin(\'' + _mlEsc(p.name) + '\')">删除</button></td>' +
        '</tr>';
    }).join('');
    el.innerHTML = '<table style="width:100%;border-collapse:collapse;font-size:12px;">' +
      '<thead><tr style="text-align:left;color:#888;border-bottom:1px solid rgba(0,0,0,.08);">' +
      '<th style="padding:6px 8px;width:30px;"></th><th style="padding:6px 8px;">插件</th>' +
      '<th style="padding:6px 8px;">类型</th><th style="padding:6px 8px;">说明</th>' +
      '<th style="padding:6px 8px;">版本</th><th style="padding:6px 8px;">状态</th>' +
      '<th style="padding:6px 8px;">操作</th></tr></thead><tbody>' + rows + '</tbody></table>';
  } catch (e) { showToast('加载插件失败: ' + e.message, 'error'); }
}

function selectPlugin(name) { _selectedPlugin = name; }

async function uploadPlugin() {
  const filename = (document.getElementById('plugin-file-name').value || '').trim();
  const code = document.getElementById('plugin-code').value;
  if (!filename || !code.trim()) { showToast('请填写文件名与代码', 'info'); return; }
  try {
    const r = await fetch('/api/plugins/upload', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ filename: filename, content: code })
    });
    const d = await r.json();
    showToast(d.message || d.error || '完成', d.ok ? 'success' : 'error');
    if (d.ok) { document.getElementById('plugin-upload-box').classList.add('hide'); loadPlugins(); }
  } catch (e) { showToast('上传失败: ' + e.message, 'error'); }
}

async function deletePlugin(name) {
  if (!confirm('确定删除插件 ' + name + ' ？')) return;
  try {
    const r = await fetch('/api/plugins/delete', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: name })
    });
    const d = await r.json();
    showToast(d.message || d.error || '完成', d.ok ? 'success' : 'error');
    if (d.ok) loadPlugins();
  } catch (e) { showToast('删除失败: ' + e.message, 'error'); }
}

async function runPlugin() {
  const el = document.getElementById('plugin-result');
  if (!el) return;
  const name = _selectedPlugin;
  if (!name) { showToast('请先选择一个插件', 'info'); return; }
  const file = (document.getElementById('plugin-file').value || '').trim();
  let params = {};
  const pv = (document.getElementById('plugin-params').value || '').trim();
  if (pv) { try { params = JSON.parse(pv); } catch (e) { showToast('参数不是合法 JSON', 'error'); return; } }
  if (!file) { showToast('请输入数据文件', 'info'); return; }
  el.innerHTML = '<div style="color:#888;font-size:12px;">运行中…</div>';
  try {
    const r = await fetch('/api/plugins/run', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: name, file: file, params: params })
    });
    const d = await r.json();
    if (!d.ok) { el.innerHTML = '<div style="color:#FF6B6B;font-size:12px;">' + _mlEsc(d.error) + '</div>'; return; }
    const res = d.result || {};
    let html = '<div style="margin-top:10px;font-size:12px;color:#888;">插件 <b>' + _mlEsc(d.name) + '</b>（' + _mlEsc(d.type) + '）执行结果：</div>';
    if (res.chart) html += '<img src="' + res.chart + '" style="max-width:100%;border-radius:8px;margin-top:8px;border:1px solid #eee;">';
    if (res.html) html += '<div style="margin-top:8px;">' + res.html + '</div>';
    const others = {};
    Object.keys(res).forEach(function (k) { if (k !== 'chart' && k !== 'html') others[k] = res[k]; });
    if (Object.keys(others).length) html += '<pre style="background:rgba(94,106,210,.06);border-radius:8px;padding:10px;font-size:12px;margin-top:8px;overflow:auto;">' + _mlEsc(JSON.stringify(others, null, 2)) + '</pre>';
    el.innerHTML = html;
  } catch (e) { el.innerHTML = '<div style="color:#FF6B6B;font-size:12px;">运行失败: ' + e.message + '</div>'; }
}

// =====================================================================
// data-analysis.js — 数据管理/清洗/分析/表格工具/时序/假设检验/财报
// =====================================================================

// ===== screener.js =====
var _screenerData = null;
var _screenResult = null;

async function doScreen() {
  const stat = document.getElementById('screen-stat');
  showLoading('正在加载全市场数据（首次约 10-20 秒）...');
  if (stat) stat.textContent = '加载数据中...';
  try {
    if (!_screenerData) {
      const fetchRes = await fetch('/api/screener/fetch');
      const fetchData = await fetchRes.json();
      if (fetchData.error) { showToast(fetchData.error, 'error'); if (stat) stat.textContent = ''; return; }
      _screenerData = fetchData.data;
      if (stat) stat.textContent = '已加载 ' + _screenerData.length + ' 只，筛选中...';
    }

    const num = (id) => {
      const v = document.getElementById(id)?.value?.trim();
      return v === '' || v == null ? null : parseFloat(v);
    };
    const filters = {};
    const peMin = num('s-pe-min'); if (peMin != null) filters.pe_min = peMin;
    const peMax = num('s-pe-max'); if (peMax != null) filters.pe_max = peMax;
    const roe = num('s-roe-min'); if (roe != null) filters.roe_min = roe;
    const mcapMin = num('s-mcap-min'); if (mcapMin != null) filters.mcap_min = mcapMin;
    const mcapMax = num('s-mcap-max'); if (mcapMax != null) filters.mcap_max = mcapMax;
    const pbMax = num('s-pb-max'); if (pbMax != null) filters.pb_max = pbMax;
    const priceMin = num('s-price-min'); if (priceMin != null) filters.price_min = priceMin;
    const priceMax = num('s-price-max'); if (priceMax != null) filters.price_max = priceMax;
    const changeMin = num('s-change-min'); if (changeMin != null) filters.change_min = changeMin;
    const industry = document.getElementById('s-industry')?.value?.trim();
    if (industry) filters.industry = industry;

    const res = await fetch('/api/screener/screen', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ data: _screenerData, filters })
    });
    const data = await res.json();
    const card = document.getElementById('s-res-card');
    const count = document.getElementById('s-res-count');
    const table = document.getElementById('s-table');
    if (card) card.classList.remove('hide');
    if (data.error) { if (count) count.innerHTML = '<span style="color:red">' + data.error + '</span>'; return; }
    const rows = data.display || data.data || [];
    _screenResult = rows;
    if (count) count.textContent = '筛选结果：' + (data.count ?? rows.length) + ' 只股票';
    if (stat) stat.textContent = '完成，共 ' + (data.count ?? rows.length) + ' 只';
    if (!rows.length) {
      if (table) table.innerHTML = '<span style="color:var(--ink-subtle)">无符合条件的股票</span>';
      return;
    }
    const cols = Object.keys(rows[0]);
    const label = { '股票代码': '代码', '股票名称': '名称', '最新价': '价格', '涨跌幅': '涨跌幅%', '市盈率(动态)': 'PE', '市盈率TTM': 'PE(TTM)', '市净率': 'PB', '总市值': '市值', '行业': '行业', '净资产收益率': 'ROE%' };
    if (table) {
      let html = '<table>';
      html += '<tr>' + cols.map(c => '<th>' + (label[c] || c) + '</th>').join('') + '</tr>';
      for (const r of rows.slice(0, 200)) {
        html += '<tr>' + cols.map(c => {
          let v = r[c];
          if (typeof v === 'number') v = Math.abs(v) >= 1e8 ? (v / 1e8).toFixed(1) + '亿' : (Number.isInteger(v) ? v : v.toFixed(2));
          return '<td>' + (v ?? '-') + '</td>';
        }).join('') + '</tr>';
      }
      html += '</table>';
      if (rows.length > 200) html += '<div style="font-size:11px;color:var(--ink-tertiary);margin-top:8px;padding:4px 2px;">仅显示前 200 条，导出 CSV 查看全部</div>';
      table.innerHTML = html;
    }
  } catch (e) {
    showToast('筛选失败', 'error');
    if (stat) stat.textContent = '';
  } finally {
    hideLoading();
  }
}

function resetScreen() {
  ['s-pe-min', 's-pe-max', 's-roe-min', 's-mcap-min', 's-mcap-max', 's-pb-max', 's-price-min', 's-price-max', 's-change-min', 's-industry'].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.value = '';
  });
  const table = document.getElementById('s-table');
  if (table) table.innerHTML = '';
  const count = document.getElementById('s-res-count');
  if (count) count.textContent = '';
  const stat = document.getElementById('screen-stat');
  if (stat) stat.textContent = '';
  const card = document.getElementById('s-res-card');
  if (card) card.classList.add('hide');
}

function exportScreenCSV() {
  if (!_screenResult || !_screenResult.length) { showToast('暂无数据', 'warning'); return; }
  const cols = Object.keys(_screenResult[0]);
  let csv = '﻿' + cols.join(',') + '\n';
  csv += _screenResult.map(r => cols.map(c => {
    const v = r[c];
    const s = v == null ? '' : String(v);
    return s.includes(',') ? '"' + s + '"' : s;
  }).join(',')).join('\n');
  const blob = new Blob([csv], { type: 'text/csv;charset=utf-8' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'screen_result.csv';
  a.click();
  URL.revokeObjectURL(a.href);
}

// ===== report.js =====

function openReport(filename) {
  window.open('/api/report/file/' + filename, '_blank');
}

// ===== nlp.js =====

async function doNLP() {
  const col = document.getElementById('nlp-col')?.value?.trim();
  const file = getCurrentFile();
  if (!file) { showToast('请先在顶栏选择数据文件', 'warning'); return; }
  if (!col) { showToast('请填写文本列名', 'warning'); return; }
  const opts = {
    seg: document.getElementById('nlp-seg')?.checked || false,
    sentiment: document.getElementById('nlp-sentiment')?.checked || false,
    kw: document.getElementById('nlp-kw')?.checked || false,
    wc: document.getElementById('nlp-wc')?.checked || false,
  };
  const stat = document.getElementById('nlp-stat');
  showLoading('正在分析文本...');
  if (stat) stat.textContent = '分析中...';
  try {
    const res = await fetch('/api/nlp/analyze', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ file, col, options: opts })
    });
    const data = await res.json();

    // 分区显示：每类结果一张卡
    const segCard = document.getElementById('nlp-seg-card');
    const sentCard = document.getElementById('nlp-sent-card');
    const kwCard = document.getElementById('nlp-kw-card');
    const wcCard = document.getElementById('nlp-wc-card');

    if (data.error) {
      if (stat) stat.innerHTML = '<span style="color:red">' + data.error + '</span>';
      showToast('NLP 分析失败', 'error');
      return;
    }

    // 分词统计
    if (segCard && data.seg) {
      segCard.classList.remove('hide');
      const stats = document.getElementById('nlp-seg-stats');
      const table = document.getElementById('nlp-seg-table');
      if (stats) stats.innerHTML = '<span style="font-size:13px;">共 ' + (data.seg.total_words ?? '-') +
        ' 词 · 去重 ' + (data.seg.unique_words ?? '-') + ' 词</span>';
      const words = data.seg.top_words || [];
      if (table && words.length) {
        let html = '<table style="border-collapse:collapse;font-size:12px;width:100%;">';
        html += '<tr><th style="padding:4px 10px;border-bottom:2px solid rgba(128,128,128,.3);text-align:left;">词语</th>' +
          '<th style="padding:4px 10px;border-bottom:2px solid rgba(128,128,128,.3);text-align:right;">频次</th></tr>';
        for (const w of words.slice(0, 50)) {
          html += '<tr><td style="padding:3px 10px;border-bottom:1px solid rgba(128,128,128,.12);">' + w.word + '</td>' +
            '<td style="padding:3px 10px;border-bottom:1px solid rgba(128,128,128,.12);text-align:right;">' + w.count + '</td></tr>';
        }
        html += '</table>';
        table.innerHTML = html;
      }
    }

    // 情感分析
    if (sentCard && data.sentiment) {
      sentCard.classList.remove('hide');
      const info = document.getElementById('nlp-sent-info');
      const s = data.sentiment;
      if (info) info.innerHTML = '<div style="font-size:14px;margin-bottom:6px;">整体情感：<b>' + (s.label || '-') + '</b>（均分 ' + (s.avg ?? '-') + '）</div>' +
        '<div style="font-size:13px;color:var(--ink-subtle);">共 ' + (s.total ?? '-') + ' 条：正面 ' + (s.positive ?? 0) +
        ' · 中性 ' + (s.neutral ?? 0) + ' · 负面 ' + (s.negative ?? 0) + '</div>';
    }

    // 关键词
    if (kwCard && data.keywords) {
      kwCard.classList.remove('hide');
      const info = document.getElementById('nlp-kw-info');
      const kws = Array.isArray(data.keywords) ? data.keywords : (data.keywords.top_words || []);
      if (info) info.innerHTML = '<div style="display:flex;flex-wrap:wrap;gap:6px;">' +
        kws.slice(0, 30).map(k => {
          const word = typeof k === 'string' ? k : (k.word || k.term || '');
          const score = typeof k === 'object' ? (k.score ?? k.weight ?? k.tfidf ?? '') : '';
          return '<span style="padding:3px 10px;border-radius:99px;background:rgba(69,183,209,.12);color:var(--primary);font-size:12px;border:1px solid rgba(69,183,209,.25);">' +
            word + (score !== '' ? ' <small>' + Number(score).toFixed(3) + '</small>' : '') + '</span>';
        }).join('') + '</div>';
    }

    // 词云
    if (wcCard && data.wordcloud) {
      wcCard.classList.remove('hide');
      const img = document.getElementById('nlp-wc-img');
      if (img) img.innerHTML = '<img src="/api/chart/charts/' + data.wordcloud + '" style="max-width:100%;border-radius:8px;">';
    }

    if (stat) stat.textContent = '分析完成';
    showToast('NLP 分析完成', 'success');
  } catch (e) {
    if (stat) stat.textContent = '';
    showToast('NLP 分析失败', 'error');
  } finally {
    hideLoading();
  }
}

// ===== file-manager.js =====

let selectedFile = null;

let _fileListReq = 0;  // 请求序号，防止旧响应覆盖新列表（删除/刷新竞态）

async function loadMultiFiles() {
  const myReq = ++_fileListReq;
  try {
    const res = await fetch('/api/files');
    const data = await res.json();
    if (myReq !== _fileListReq) return;  // 过期响应丢弃
    const files = Array.isArray(data) ? data : (data.files || []);
    renderFL(files);
    renderFileSelector(files);
  } catch (e) {
    console.error('Load files error:', e);
  }
}

function renderFileSelector(files) {
  const container = document.getElementById('file-list');
  const selector = document.getElementById('file-selector');
  if (!container || !selector) return;
  
  if (!files || files.length === 0) {
    container.innerHTML = '';  // 清空旧列表，避免删除后残留
    selector.style.display = 'none';
    return;
  }
  
  selector.style.display = 'block';
  
  // 工具栏：全选 / 批量删除 / 归档
  const toolbar = '<div style="display:flex;align-items:center;gap:10px;margin-bottom:8px;font-size:12px;flex-wrap:wrap;">' +
    '<label style="display:flex;align-items:center;gap:4px;cursor:pointer;"><input type="checkbox" id="file-sel-all" onchange="toggleSelectAllFiles(this)"> 全选</label>' +
    '<span style="color:var(--ink-subtle);" id="file-sel-count">已选 0</span>' +
    '<span style="flex:1"></span>' +
    '<button class="btn btn-s btn-sm" onclick="archiveSelectedFiles()" style="font-size:12px;" title="归档=软删除，可从列表隐藏">📦 归档所选</button>' +
    '<button class="btn btn-w btn-sm" onclick="deleteSelectedFiles()" style="font-size:12px;">🗑 批量删除</button>' +
    '<button class="btn btn-s btn-sm" onclick="toggleRestorePanel()" style="font-size:12px;" title="查看/恢复元数据软删除的文件">♻ 已删除</button>' +
    '<button class="btn btn-s btn-sm" onclick="loadMultiFiles()" style="font-size:12px;">↻ 刷新</button>' +
    '</div>';
  
  const items = files.map(f => {
    const name = typeof f === 'string' ? f : (f.name || f.filename || 'unknown');
    const path = (typeof f === 'string') ? '' : (f.path || '');
    const size = f.size || '';
    const tags = (f.tags && f.tags.length) ? f.tags : [];
    const isSelected = (window._selectedFile === name) ? 'selected' : '';
    const sizeStr = size ? (typeof size === 'number' ? fmtSize(size) : size) : '';
    const tagHtml = tags.length
      ? '<div class="file-item-tags" style="margin-top:2px;">' + tags.map(function (t) { return '<span style="display:inline-block;background:rgba(94,106,210,.12);color:#5E6AD2;border-radius:6px;padding:1px 6px;font-size:10px;margin-right:4px;">#' + esc(t) + '</span>'; }).join('') + '</div>'
      : '';
    return '<div class="file-item ' + isSelected + '" style="position:relative;">' +
      '<input type="checkbox" class="file-del-cb" data-path="' + esc(path) + '" data-name="' + esc(name) + '" onchange="updateFileSelCount()" style="margin:0 6px 0 2px;flex-shrink:0;" title="勾选后可批量删除/归档">' +
      '<span class="file-item-icon" style="pointer-events:none;">📄</span>' +
      '<div class="file-item-info" data-name="' + esc(name) + '" onclick="selectFile(this.dataset.name)" style="cursor:pointer;flex:1;">' +
      '<div class="file-item-name">' + esc(name) + '</div>' +
      (sizeStr ? '<div class="file-item-meta">' + sizeStr + '</div>' : '') +
      tagHtml +
      '</div>' +
      '<button class="btn btn-s btn-sm" data-name="' + esc(name) + '" onclick="snapshotFile(this.dataset.name)" style="font-size:11px;padding:2px 8px;" title="创建版本快照">💾</button>' +
      '<button class="btn btn-s btn-sm" data-name="' + esc(name) + '" onclick="showSnapshots(this.dataset.name)" style="font-size:11px;padding:2px 8px;" title="版本历史/回滚">🕘</button>' +
      '<button class="btn btn-s btn-sm" data-path="' + esc(path) + '" data-name="' + esc(name) + '" onclick="editFileTags(this.dataset.path, this.dataset.name)" style="font-size:11px;padding:2px 8px;" title="编辑标签（个人工作空间分类）">🏷</button>' +
      '<button class="btn btn-w btn-sm" data-path="' + esc(path) + '" data-name="' + esc(name) + '" onclick="deleteSingleFile(this.dataset.path, this.dataset.name)" style="font-size:11px;padding:2px 8px;" title="删除此文件">✕</button>' +
      '</div>';
  }).join('');
  
  container.innerHTML = toolbar + items;
}

function fmtSize(bytes) {
  if (bytes >= 1048576) return (bytes / 1048576).toFixed(1) + ' MB';
  if (bytes >= 1024) return (bytes / 1024).toFixed(1) + ' KB';
  return bytes + ' B';
}

function esc(s) {
  if (!s) return '';
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

// 全选/取消全选
function toggleSelectAllFiles(cb) {
  document.querySelectorAll('#file-list .file-del-cb').forEach(function(c){ c.checked = cb.checked; });
  updateFileSelCount();
}

// 更新已选计数
function updateFileSelCount() {
  const n = document.querySelectorAll('#file-list .file-del-cb:checked').length;
  const el = document.getElementById('file-sel-count');
  if (el) el.textContent = '已选 ' + n;
}

// 查看/恢复元数据软删除的文件（沙箱禁止物理删除时的降级方案，可恢复）
async function toggleRestorePanel() {
  const panel = document.getElementById('restore-panel');
  if (panel) { panel.remove(); return; }
  try {
    const r = await fetch('/api/files');
    const d = await r.json();
    const del = d.deleted || [];
    const div = document.createElement('div');
    div.id = 'restore-panel';
    div.style.cssText = 'margin:8px 0;padding:10px;border:1px dashed rgba(255,107,107,.4);border-radius:10px;background:rgba(255,107,107,.05);font-size:12px;';
    if (!del.length) {
      div.innerHTML = '<div style="color:var(--ink-subtle)">暂无已删除文件（删除为元数据软删除，物理文件仍保留在磁盘）</div>';
    } else {
      div.innerHTML = '<div style="margin-bottom:6px;font-weight:600;">🗑 已删除文件（可恢复，物理文件仍保留在磁盘）</div>' +
        '<label style="display:flex;align-items:center;gap:4px;margin-bottom:6px;cursor:pointer;"><input type="checkbox" onchange="document.querySelectorAll(\'#restore-list .file-del-cb\').forEach(c=>c.checked=this.checked)"> 全选</label>' +
        '<div id="restore-list" style="max-height:220px;overflow-y:auto;">' + del.map(f =>
          '<div style="display:flex;align-items:center;gap:8px;padding:3px 0;">' +
          '<input type="checkbox" class="file-del-cb" data-path="' + esc(f.path) + '" data-name="' + esc(f.name) + '">' +
          '<span style="flex:1;word-break:break-all;">' + esc(f.name) + '</span>' +
          '<span style="color:var(--ink-subtle);font-size:10px;flex-shrink:0;">' + new Date(f.ts * 1000).toLocaleString() + '</span>' +
          '</div>').join('') + '</div>' +
        '<div style="margin-top:8px;"><button class="btn btn-g btn-sm" onclick="restoreSelectedFiles()">♻ 恢复所选</button>' +
        '<button class="btn btn-s btn-sm" style="margin-left:6px;" onclick="document.getElementById(\'restore-panel\').remove()">关闭</button></div>';
    }
    const container = document.getElementById('file-list');
    if (container) container.parentNode.insertBefore(div, container);
  } catch (e) {
    showToast('加载已删除清单失败: ' + e.message, 'error');
  }
}

// 批量删除勾选的文件
async function deleteSelectedFiles() {
  const cbs = Array.from(document.querySelectorAll('#file-list .file-del-cb:checked'));
  if (!cbs.length) { showToast('请先勾选要删除的文件', 'warning'); return; }
  const paths = cbs.map(c => c.dataset.path).filter(p => p);
  const names = cbs.map(c => c.dataset.name);
  if (!paths.length) { showToast('勾选的文件缺少路径信息', 'error'); return; }
  if (!confirm('确认删除 ' + paths.length + ' 个文件？\n' + names.join('、'))) return;
  try {
    const r = await fetch('/api/files/delete', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ paths: paths })
    });
    const d = await r.json();
    const delN = (d.deleted ? d.deleted.length : 0) + (d.soft_deleted ? d.soft_deleted.length : 0);
    if (d.ok) {
      showToast('已删除 ' + delN + ' 个文件', 'success');
    } else {
      showToast('部分删除失败: ' + (d.errors && d.errors.length ? d.errors[0].error : '未知错误'), 'error');
    }
  } catch(e) {
    showToast('删除失败: ' + e.message, 'error');
  }
  loadMultiFiles();
  if (typeof refreshGlobalFileSelect === 'function') refreshGlobalFileSelect();
}

// 恢复元数据软删除的文件
async function restoreSelectedFiles() {
  const cbs = Array.from(document.querySelectorAll('#restore-list .file-del-cb:checked'));
  if (!cbs.length) { showToast('请先勾选要恢复的文件', 'warning'); return; }
  const paths = cbs.map(c => c.dataset.path).filter(p => p);
  if (!paths.length) { showToast('勾选的文件缺少路径信息', 'error'); return; }
  if (!confirm('确认恢复 ' + paths.length + ' 个文件？')) return;
  try {
    const r = await fetch('/api/files/restore', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ paths: paths })
    });
    const d = await r.json();
    if (d.ok || (d.restored && d.restored.length)) {
      showToast('已恢复 ' + (d.restored ? d.restored.length : 0) + ' 个文件', 'success');
    } else {
      showToast('恢复失败: ' + (d.errors && d.errors.length ? d.errors[0].error : '未知错误'), 'error');
    }
  } catch(e) {
    showToast('恢复失败: ' + e.message, 'error');
  }
  loadMultiFiles();
  if (typeof refreshGlobalFileSelect === 'function') refreshGlobalFileSelect();
}

// 删除单个文件
async function deleteSingleFile(path, name) {
  if (!path) { showToast('无法删除：缺少路径（内置文件）', 'warning'); return; }
  if (!confirm('确认删除文件：' + name + '？')) return;
  try {
    const r = await fetch('/api/files/delete', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ path: path })
    });
    const d = await r.json();
    if (d.ok) {
      showToast('已删除 ' + name, 'success');
      if (window._selectedFile === name) window._selectedFile = null;
    } else {
      showToast('删除失败: ' + (d.error || '未知错误'), 'error');
    }
  } catch(e) {
    showToast('删除失败: ' + e.message, 'error');
  }
  loadMultiFiles();
  if (typeof refreshGlobalFileSelect === 'function') refreshGlobalFileSelect();
}

function selectFile(name) {
  if (!name) return;
  window._selectedFile = name;
  // 不持久化：文件选择仅当前会话有效，重启后默认"未选择"
  
  // 更新顶栏下拉框
  var sel = document.getElementById('globalFileSelect');
  if (sel) sel.value = name;
  
  // 更新文件列表选中状态
  var items = document.querySelectorAll('.file-item');
  for (var i = 0; i < items.length; i++) {
    var nm = items[i].querySelector('.file-item-name');
    if (nm && nm.textContent === name) {
      items[i].classList.add('selected');
    } else {
      items[i].classList.remove('selected');
    }
  }
  
  // 更新所有页面文件指示器
  var ids = ['analyzeFileName', 'mlFileName', 'cleanFileName', 'tsFileName', 'htFileName', 'financeFileName'];
  for (var j = 0; j < ids.length; j++) {
    var el = document.getElementById(ids[j]);
    if (el) el.textContent = name;
  }
  
  // 更新状态栏
  var statusEl = document.getElementById('status-bar-tab');
  if (statusEl) statusEl.textContent = '📊 ' + name;
}

// 暴露到全局
if (typeof window !== 'undefined') window.selectFile = selectFile;

function fsChange(input) {
  if (!input || !input.files || input.files.length === 0) return;
  const file = input.files[0];
  const formData = new FormData();
  formData.append('file', file);

  // 「上传 → 列表刷新」之间有秒级空窗（列表接口要扫目录），此前这段期间界面毫无变化，
  // 用户看到的只有一句 3 秒即消失的 toast，故误以为"上传成功了但没显示"。
  // 这里先把结果/进度写进「已导入文件」区，loadMultiFiles 回来后 renderFL 会覆盖为真实列表。
  const setHint = function (h) {
    const box = document.getElementById('fl');
    const title = document.getElementById('fl-title');
    if (box) box.innerHTML = '<span style="font-size:13px;color:var(--ink-subtle);">' + h + '</span>';
    if (title) title.style.display = 'block';
  };
  setHint('⏳ 正在上传 ' + esc(file.name) + ' …');
  showLoading('正在上传 ' + file.name + '…');

  fetch('/api/upload', { method: 'POST', body: formData })
    .then(r => r.json())
    .then(data => {
      hideLoading();
      if (data.error) {
        setHint('❌ 上传失败：' + esc(data.error));
        showToast('上传失败：' + data.error, 'error');
        return;
      }
      const parts = [];
      if (data.rows != null) parts.push(data.rows + ' 行');
      if (data.cols && data.cols.length) parts.push(data.cols.length + ' 列');
      const extra = parts.length ? '（' + parts.join(' · ') + '）' : '';
      setHint('✅ 已导入 ' + esc(data.file || file.name) + extra + '，正在刷新文件列表…');
      showToast('上传成功：' + (data.file || file.name), 'success');
      loadMultiFiles();
      refreshGlobalFileSelect();  // 同步刷新顶栏文件选择器
    })
    .catch(e => {
      hideLoading();
      setHint('❌ 上传失败：' + esc((e && e.message) || e));
      showToast('上传失败', 'error');
    })
    // 清空 input：否则再次选择同一个文件不会触发 change，用户会以为"没反应"
    .finally(function () { try { input.value = ''; } catch (_) { } });
}

async function doScrape() {
  const url = document.getElementById('s-url')?.value?.trim();
  if (!url) { showToast('请输入 URL', 'warning'); return; }
  const taskId = 'scrape_' + Date.now();
  const pages = parseInt(document.getElementById('s-pages')?.value) || 1;
  const selector = document.getElementById('s-sel')?.value?.trim() || '';
  const fields = document.getElementById('s-fields')?.value?.trim() || 'text';
  const next = document.getElementById('s-next')?.value?.trim() || '';
  const detail = !!document.getElementById('s-det')?.checked;

  const logCard = document.getElementById('sc-log-card');
  const logEl = document.getElementById('log');
  const resCard = document.getElementById('sc-res-card');
  const stats = document.getElementById('sc-stats');
  const stat = document.getElementById('s-stat');
  if (logCard) logCard.classList.remove('hide');
  if (logEl) logEl.innerHTML = '';
  if (stat) stat.textContent = '爬取中...';

  // SSE 实时日志
  let es = null;
  const appendLog = (msg) => {
    if (!logEl) return;
    const line = document.createElement('div');
    line.textContent = msg;
    logEl.appendChild(line);
    logEl.scrollTop = logEl.scrollHeight;
  };
  // 注：此前此处有 new EventSource('/api/stream?task=...') 指向不存在的路由（永远 404 静默失败），
  // 属死代码已移除——爬取为同步接口，结果直接在下方 res.json() 处理。

  showLoading('正在爬取（可通过日志观察进度）...');
  try {
    const res = await fetch('/api/scrape', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ task_id: taskId, url, pages, selector, fields, next, detail })
    });
    const data = await res.json();
    if (es) { es.close(); es = null; }
    if (resCard) resCard.classList.remove('hide');
    if (data.error) {
      if (stats) stats.innerHTML = '<span style="color:red">' + data.error + '</span>';
      if (stat) stat.textContent = '失败';
      showToast('爬取失败', 'error');
    } else {
      const st = data.stats || {};
      const parts = [];
      if (st.rows != null) parts.push(st.rows + ' 行');
      if (st.pages != null) parts.push(st.pages + ' 页');
      if (stats) stats.innerHTML = '<div style="font-size:13px;">爬取完成' + (parts.length ? '：' + parts.join(' · ') : '') + '</div>' +
        '<div style="font-size:12px;color:var(--ink-subtle);margin-top:4px;">文件已保存为 raw.csv，可到「上传」页或顶栏选择后分析</div>';
      if (stat) stat.textContent = '完成';
      appendLog('爬取完成，结果已保存为 raw.csv');
      showToast('爬取完成', 'success');
    }
  } catch (e) {
    if (es) { es.close(); es = null; }
    showToast('爬取失败', 'error');
    if (stat) stat.textContent = '';
  } finally {
    hideLoading();
  }
}

function stopScrape() {
  showToast('已发送停止提示（当前后端为同步执行，本次爬完后生效）', 'info');
}

// ===== analyze.js =====

async function doStore() {
  const file = getCurrentFile();
  if (!file) { showToast('请选择数据文件', 'warning'); return; }
  showLoading('正在存储...');
  try {
    const res = await fetch('/api/store', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ file })
    });
    const data = await res.json();
    if (data.error) {
      showToast(data.error, 'error');
    } else {
      showToast('存储成功', 'success');
    }
  } catch (e) {
    showToast('存储失败', 'error');
  } finally {
    hideLoading();
  }
}

function dl(name) {
  window.location.href = '/api/download/' + name;
}

// ===== table-tools.js =====

// POST JSON → 解析后的响应体
async function _postJson(url, body) {
  const res = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body)
  });
  return res.json();
}

// 显示结果卡片：错误 → info 红字；成功 → info 文本 + preview 表格 + stat 行数
// ui: { card, info, prev, stat } 为元素 ID；rowsValue 为可选行数文本
function _showToolCard(ui, data, infoHtml, rowsValue) {
  const card = document.getElementById(ui.card);
  if (card) card.classList.remove('hide');
  if (data.error || data.ok === false) {
    const info = document.getElementById(ui.info);
    if (info) info.innerHTML = '<span style="color:red">' + (data.error || '操作失败') + '</span>';
    return false;
  }
  const info = document.getElementById(ui.info);
  if (info && infoHtml != null) info.innerHTML = infoHtml;
  const prev = document.getElementById(ui.prev);
  if (prev) {
    if (data.html) prev.innerHTML = data.html;
    else {
      const preview = data.preview || data.head || data.records || [];
      if (preview.length) prev.innerHTML = recordsToTableHtml(preview, 50);
    }
  }
  const stat = document.getElementById(ui.stat);
  if (stat && rowsValue != null) {
    stat.textContent = rowsValue !== '' ? '（' + rowsValue + ' 行）' : '';
  }
  return true;
}

// 填充 JOIN 主/从表下拉（切换到表格工具 tab 时调用）
async function loadJoinFiles() {
  const s1 = document.getElementById('join-f1');
  const s2 = document.getElementById('join-f2');
  if (!s1 || !s2 || s1.childElementCount > 0) return;
  try {
    const res = await fetch('/api/files');
    const files = await res.json();
    const list = Array.isArray(files) ? files : (files.files || []);
    const opts = '<option value="">-- 选择文件 --</option>' +
      list.map(f => '<option value="' + f + '">' + f + '</option>').join('');
    s1.innerHTML = opts;
    s2.innerHTML = opts;
    const cur = (typeof getCurrentFile === 'function' ? getCurrentFile() : '') || '';
    if (cur) { s1.value = cur; if (list.length > 1) s2.value = list.find(f => f !== cur) || list[0]; }
  } catch (e) { /* 静默 */ }
}

async function doJoin() {
  const file1 = document.getElementById('join-f1')?.value;
  const file2 = document.getElementById('join-f2')?.value;
  const keys = document.getElementById('join-keys')?.value?.trim();
  const join_type = document.getElementById('join-type')?.value || 'inner';
  if (!file1 || !file2 || !keys) { showToast('请选择主表、从表和关联键', 'warning'); return; }
  const keyList = keys.split(',').map(k => k.trim()).filter(Boolean);
  showLoading('正在连接...');
  try {
    const data = await _postJson('/api/table/join',
      { file1, file2, left_keys: keyList, right_keys: keyList, join_type });
    const rows = data.rows ?? data.rows_after ?? data.row_count ?? '';
    const ok = _showToolCard(
      { card: 'join-res-card', info: 'join-info', prev: 'join-preview', stat: 'join-stat' },
      data,
      '<div style="font-size:12px;color:var(--ink-subtle);margin-bottom:6px;">连接类型: ' + join_type +
      ' · 关联键: ' + keyList.join(', ') + (rows !== '' ? ' · 结果 ' + rows + ' 行' : '') + '</div>',
      rows);
    if (ok) showToast('JOIN 完成', 'success');
  } catch (e) {
    showToast('连接失败', 'error');
  } finally {
    hideLoading();
  }
}

let _formulaResultFile = null;

async function doFormula() {
  const file = getCurrentFile();
  const formula = document.getElementById('form-expr')?.value?.trim();
  const col = document.getElementById('form-col')?.value?.trim();
  if (!file || !formula || !col) { showToast('请填写公式和新列名', 'warning'); return; }
  showLoading('正在计算...');
  try {
    const data = await _postJson('/api/table/formula-apply', { file, formula, new_col: col });
    const ok = _showToolCard(
      { card: 'form-res-card', info: 'form-preview', prev: 'form-preview', stat: 'form-stat' },
      data, null, data.rows ?? '');
    if (ok) {
      _formulaResultFile = data.file;
      showToast('新列已生成: ' + col, 'success');
    }
  } catch (e) {
    showToast('公式计算失败', 'error');
  } finally {
    hideLoading();
  }
}

function dlFormRes() {
  if (_formulaResultFile) dl(_formulaResultFile);
  else showToast('请先执行公式计算', 'warning');
}

async function doPivot() {
  const file = getCurrentFile();
  const index = document.getElementById('piv-index')?.value?.trim();
  const colDim = document.getElementById('piv-col')?.value?.trim();
  const val = document.getElementById('piv-val')?.value?.trim();
  const agg = document.getElementById('piv-agg')?.value || 'mean';
  if (!file || !index || !val) { showToast('请填写行维度和值字段', 'warning'); return; }
  showLoading('正在生成透视表...');
  try {
    const data = await _postJson('/api/table/pivot',
      { file, index_cols: index.split(',').map(s => s.trim()), columns_col: colDim || '', values_col: val, agg_func: agg });
    const ok = _showToolCard(
      { card: 'piv-res-card', info: 'piv-info', prev: 'piv-preview', stat: 'piv-stat' },
      data,
      '<div style="font-size:12px;color:var(--ink-subtle);margin-bottom:6px;">聚合: ' + agg + ' · 值: ' + val + '</div>');
    if (ok) showToast('透视表完成', 'success');
  } catch (e) {
    showToast('透视表失败', 'error');
  } finally {
    hideLoading();
  }
}

async function doCrosstab() {
  const file = getCurrentFile();
  const row = document.getElementById('piv-index')?.value?.trim();
  const col = document.getElementById('piv-col')?.value?.trim();
  if (!file || !row || !col) { showToast('交叉表需要行维度和列维度都填写', 'warning'); return; }
  showLoading('正在生成交叉表...');
  try {
    const data = await _postJson('/api/table/crosstab', { file, row_col: row, col_col: col });
    const ok = _showToolCard(
      { card: 'piv-res-card', info: 'piv-info', prev: 'piv-preview', stat: 'piv-stat' },
      data,
      '<div style="font-size:12px;color:var(--ink-subtle);margin-bottom:6px;">交叉表: ' + row + ' × ' + col + '</div>');
    if (ok) showToast('交叉表完成', 'success');
  } catch (e) {
    showToast('交叉表失败', 'error');
  } finally {
    hideLoading();
  }
}

async function doSample() {
  const file = getCurrentFile();
  const raw = document.getElementById('samp-n')?.value?.trim();
  const type = document.getElementById('samp-type')?.value || 'random';
  if (!file || !raw) { showToast('请填写抽样数量或比例', 'warning'); return; }
  const payload = { file, sample_type: type };
  if (raw.includes('.')) payload.frac = parseFloat(raw);
  else payload.n = parseInt(raw, 10);
  showLoading('正在抽样...');
  try {
    const data = await _postJson('/api/table/sample', payload);
    const rows = data.rows ?? data.sampled ?? data.n ?? '';
    const ok = _showToolCard(
      { card: 'samp-res-card', info: 'samp-info', prev: 'samp-preview', stat: 'samp-stat' },
      data,
      '<div style="font-size:12px;color:var(--ink-subtle);margin-bottom:6px;">方式: ' + type +
      (rows !== '' ? ' · 抽样 ' + rows + ' 行' : '') + '</div>',
      rows);
    if (ok) {
      if (data.file) _sampleResultFile = data.file;
      showToast('抽样完成', 'success');
    }
  } catch (e) {
    showToast('抽样失败', 'error');
  } finally {
    hideLoading();
  }
}

// ===== timeseries.js =====

// 通用渲染辅助
function escHtml(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

// records 数组 → HTML 表格
function recordsToTableHtml(records, maxRows) {
  if (!records || !records.length) return '<span style="color:var(--ink-subtle)">暂无数据</span>';
  const rows = records.slice(0, maxRows || 20);
  const cols = Object.keys(rows[0] || {});
  if (!cols.length) return '<span style="color:var(--ink-subtle)">暂无数据</span>';
  return '<table style="width:100%;border-collapse:collapse;font-size:12px;margin-top:8px;">' +
    '<thead><tr>' + cols.map(c => '<th style="padding:6px 8px;border:1px solid #e5e7eb;background:#f3f4f6;text-align:left;white-space:nowrap;font-weight:600;">' + escHtml(c) + '</th>').join('') + '</tr></thead>' +
    '<tbody>' + rows.map(r => '<tr>' + cols.map(c => '<td style="padding:6px 8px;border:1px solid #e5e7eb;white-space:nowrap;">' + escHtml(r[c]) + '</td>').join('') + '</tr>').join('') + '</tbody></table>';
}

// base64 图片渲染
function b64ImgHtml(b64, style) {
  return '<img src="data:image/png;base64,' + b64 + '" style="' + (style || 'max-width:100%;border-radius:8px;margin-top:10px;') + '">';
}

// 显示结果卡片 + 更新状态文本
function showResCard(cardId, statId, msg) {
  const card = document.getElementById(cardId);
  if (card) card.classList.remove('hide');
  const stat = document.getElementById(statId);
  if (stat) stat.textContent = msg || '';
}

async function doDateFeatures() {
  const file = getCurrentFile();
  const col = document.getElementById('ts-date-col')?.value?.trim();
  if (!file) { showToast('请选择数据文件', 'warning'); return; }
  if (!col) { showToast('请输入日期列', 'warning'); return; }
  showLoading('正在提取日期特征...');
  try {
    const res = await fetch('/api/ts/date-features', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ file, date_col: col })
    });
    const data = await res.json();
    const info = document.getElementById('ts-df-info');
    const preview = document.getElementById('ts-df-preview');
    const card = document.getElementById('ts-df-res-card');
    if (!info || !preview || !card) { showToast('结果区域缺失（ts-df-res-card/ts-df-info/ts-df-preview）', 'error'); return; }
    if (data.error) { showToast(data.error, 'error'); return; }
    info.innerHTML = '新增 <b>' + (data.new_columns || []).length + '</b> 个特征列：<code>' +
      escHtml((data.new_columns || []).join(', ')) + '</code>，共 <b>' + (data.total_rows || 0) + '</b> 行';
    preview.innerHTML = recordsToTableHtml(data.preview);
    card.classList.remove('hide');
    showResCard('ts-df-res-card', 'ts-df-stat', '✓ 已生成 ' + (data.new_columns || []).length + ' 个特征列');
    showToast('日期特征提取完成', 'success');
  } catch (e) {
    showToast('日期特征提取失败: ' + (e.message || ''), 'error');
  } finally {
    hideLoading();
  }
}

async function doResample() {
  const file = getCurrentFile();
  const dateCol = document.getElementById('ts-rs-date')?.value?.trim();
  const valCol = document.getElementById('ts-rs-val')?.value?.trim();
  const freq = document.getElementById('ts-rs-freq')?.value || 'D';
  const agg = document.getElementById('ts-rs-agg')?.value || 'mean';
  if (!file) { showToast('请选择数据文件', 'warning'); return; }
  if (!dateCol || !valCol) { showToast('请填写日期列和数值列', 'warning'); return; }
  showLoading('正在重采样...');
  try {
    const res = await fetch('/api/ts/resample', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ file, date_col: dateCol, value_col: valCol, freq, agg_func: agg })
    });
    const data = await res.json();
    const info = document.getElementById('ts-rs-info');
    const preview = document.getElementById('ts-rs-preview');
    const card = document.getElementById('ts-rs-res-card');
    if (!info || !preview || !card) { showToast('结果区域缺失（ts-rs-res-card/ts-rs-info/ts-rs-preview）', 'error'); return; }
    if (data.error) { showToast(data.error, 'error'); return; }
    info.innerHTML = '重采样完成：<b>' + (data.rows || 0) + '</b> 行（频率 <b>' + escHtml(data.freq || freq) +
      '</b>，聚合方式 <b>' + escHtml(data.agg_func || agg) + '</b>）';
    preview.innerHTML = recordsToTableHtml(data.preview);
    card.classList.remove('hide');
    showResCard('ts-rs-res-card', 'ts-rs-stat', '✓ 重采样完成，共 ' + (data.rows || 0) + ' 行');
    // 收集到本次会话图表
    if (typeof saveChartToReport === 'function') {
      saveChartToReport('', '时序重采样', '频率=' + (data.freq || freq) + ', ' + (data.rows || 0) + '行', '时序分析', preview.innerHTML);
    }
    showToast('重采样完成', 'success');
  } catch (e) {
    showToast('重采样失败: ' + (e.message || ''), 'error');
  } finally {
    hideLoading();
  }
}

async function doProphet() {
  const file = getCurrentFile();
  const dateCol = document.getElementById('ts-pr-date')?.value?.trim();
  const valCol = document.getElementById('ts-pr-val')?.value?.trim();
  const periods = parseInt(document.getElementById('ts-pr-periods')?.value) || 30;
  if (!file) { showToast('请选择数据文件', 'warning'); return; }
  if (!dateCol || !valCol) { showToast('请填写日期列和数值列', 'warning'); return; }
  showLoading('正在预测...');
  try {
    const res = await fetch('/api/ts/prophet', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ file, date_col: dateCol, value_col: valCol, periods })
    });
    const data = await res.json();
    const info = document.getElementById('ts-pr-info');
    const charts = document.getElementById('ts-pr-charts');
    const card = document.getElementById('ts-pr-res-card');
    if (!info || !charts || !card) { showToast('结果区域缺失（ts-pr-res-card/ts-pr-info/ts-pr-charts）', 'error'); return; }
    if (data.error) { showToast(data.error, 'error'); return; }
    info.innerHTML = 'Prophet 预测完成：RMSE=<b>' + escHtml(data.rmse) + '</b>，R²=<b>' + escHtml(data.r2) +
      '</b>，预测 <b>' + (data.predictions ? data.predictions.length : periods) + '</b> 期';
    charts.innerHTML = (data.forecast_chart ? '<div style="margin:8px 0;">' + b64ImgHtml(data.forecast_chart) + '</div>' : '') +
      (data.components_chart ? '<div style="margin:8px 0;">' + b64ImgHtml(data.components_chart) + '</div>' : '') +
      (data.predictions ? '<div style="font-size:12px;color:var(--ink-subtle);margin-top:6px;">' + recordsToTableHtml(data.predictions, 10) + '</div>' : '');
    card.classList.remove('hide');
    showResCard('ts-pr-res-card', 'ts-pr-stat', '✓ 预测完成，RMSE=' + data.rmse + '，R²=' + data.r2);
    // 收集到本次会话图表
    if (typeof saveChartToReport === 'function') {
      saveChartToReport('', 'Prophet时序预测', 'RMSE=' + data.rmse + ', R²=' + data.r2, '时序分析', charts.innerHTML);
    }
    showToast('Prophet 预测完成', 'success');
  } catch (e) {
    showToast('Prophet 预测失败: ' + (e.message || ''), 'error');
  } finally {
    hideLoading();
  }
}

async function doRolling() {
  const file = getCurrentFile();
  const valCol = document.getElementById('ts-rl-val')?.value?.trim();
  const win = parseInt(document.getElementById('ts-rl-window')?.value) || 20;
  const ROLLING_OPS = ['rolling_mean', 'rolling_std', 'ewma', 'rolling_volatility', 'rolling_sharpe'];
  const calcTypes = Array.from(document.querySelectorAll('#tab-ts input[type=checkbox]'))
    .filter(cb => ROLLING_OPS.indexOf(cb.value) !== -1 && cb.checked)
    .map(cb => cb.value);
  if (!file) { showToast('请选择数据文件', 'warning'); return; }
  if (!valCol) { showToast('请输入数值列', 'warning'); return; }
  if (calcTypes.length === 0) { showToast('请至少选择一种计算类型', 'warning'); return; }
  showLoading('正在计算滚动窗口...');
  try {
    const res = await fetch('/api/ts/rolling', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ file, value_col: valCol, window: win, calc_types: calcTypes })
    });
    const data = await res.json();
    const info = document.getElementById('ts-rl-info');
    const chart = document.getElementById('ts-rl-chart');
    const card = document.getElementById('ts-rl-res-card');
    if (!info || !chart || !card) { showToast('结果区域缺失（ts-rl-res-card/ts-rl-info/ts-rl-chart）', 'error'); return; }
    if (data.error) { showToast(data.error, 'error'); return; }
    info.innerHTML = '窗口大小 <b>' + (data.window || win) + '</b>，新增列：<code>' +
      escHtml((data.new_columns || []).join(', ')) + '</code>';
    chart.innerHTML = b64ImgHtml(data.chart) + recordsToTableHtml(data.preview, 10);
    card.classList.remove('hide');
    showResCard('ts-rl-res-card', 'ts-rl-stat', '✓ 已生成 ' + (data.new_columns || []).length + ' 个滚动指标');
    // 收集到本次会话图表
    if (typeof saveChartToReport === 'function') {
      saveChartToReport('', '滚动窗口分析', '窗口=' + (data.window || win), '时序分析', chart.innerHTML);
    }
    showToast('滚动窗口计算完成', 'success');
  } catch (e) {
    showToast('滚动窗口失败: ' + (e.message || ''), 'error');
  } finally {
    hideLoading();
  }
}

async function doDecompose() {
  const file = getCurrentFile();
  const dateCol = document.getElementById('ts-dc-date')?.value?.trim();
  const valCol = document.getElementById('ts-dc-val')?.value?.trim();
  const period = parseInt(document.getElementById('ts-dc-period')?.value) || 7;
  const model = document.getElementById('ts-dc-model')?.value || 'additive';
  if (!file) { showToast('请选择数据文件', 'warning'); return; }
  if (!dateCol || !valCol) { showToast('请填写日期列和数值列', 'warning'); return; }
  showLoading('正在分解...');
  try {
    const res = await fetch('/api/ts/decompose', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ file, date_col: dateCol, value_col: valCol, period, model })
    });
    const data = await res.json();
    const info = document.getElementById('ts-dc-info');
    const chart = document.getElementById('ts-dc-chart');
    const card = document.getElementById('ts-dc-res-card');
    if (!info || !chart || !card) { showToast('结果区域缺失（ts-dc-res-card/ts-dc-info/ts-dc-chart）', 'error'); return; }
    if (data.error) { showToast(data.error, 'error'); return; }
    info.innerHTML = '模型 <b>' + escHtml(data.model || model) + '</b>，周期 <b>' + (data.period || period) +
      '</b>，样本量 <b>' + (data.total_rows || 0) + '</b>，趋势强度 <b>' + data.trend_strength +
      '</b>，季节强度 <b>' + data.seasonal_strength + '</b>';
    chart.innerHTML = b64ImgHtml(data.chart);
    card.classList.remove('hide');
    showResCard('ts-dc-res-card', 'ts-dc-stat', '✓ 分解完成，趋势强度=' + data.trend_strength + '，季节强度=' + data.seasonal_strength);
    // 收集到本次会话图表
    if (typeof saveChartToReport === 'function') {
      saveChartToReport('', '时序分解', '趋势强度=' + data.trend_strength + ', 季节强度=' + data.seasonal_strength, '时序分析', chart.innerHTML);
    }
    showToast('时序分解完成', 'success');
  } catch (e) {
    showToast('时序分解失败: ' + (e.message || ''), 'error');
  } finally {
    hideLoading();
  }
}

async function doADF() {
  const file = getCurrentFile();
  const valCol = document.getElementById('ts-adf-val')?.value?.trim();
  const diff = parseInt(document.getElementById('ts-adf-diff')?.value) || 0;
  const autoDiff = !!document.getElementById('ts-adf-auto')?.checked;
  if (!file) { showToast('请选择数据文件', 'warning'); return; }
  if (!valCol) { showToast('请输入数值列', 'warning'); return; }
  showLoading('正在进行 ADF 检验...');
  try {
    const res = await fetch('/api/ts/adf', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ file, value_col: valCol, diff_order: diff, auto_diff: autoDiff })
    });
    const data = await res.json();
    const resultEl = document.getElementById('ts-adf-result');
    const chartEl = document.getElementById('ts-adf-chart');
    const card = document.getElementById('ts-adf-res-card');
    if (!resultEl || !chartEl || !card) { showToast('结果区域缺失（ts-adf-res-card/ts-adf-result/ts-adf-chart）', 'error'); return; }
    if (data.error) { showToast(data.error, 'error'); return; }
    const conclusion = data.is_stationary
      ? '<span style="color:#16a34a;">✓ 序列平稳（p&lt;0.05，拒绝单位根）</span>'
      : '<span style="color:#45B7D1;">✗ 序列非平稳（p≥0.05，存在单位根）</span>';
    let html = '<table style="width:100%;border-collapse:collapse;font-size:13px;margin:4px 0;">';
    const rows = [
      ['ADF 统计量', data.adf_stat],
      ['p 值', data.adf_pval],
      ['实际差分阶数', data.diff_order],
      ['样本量', data.sample_size],
    ];
    rows.forEach(r => {
      html += '<tr><td style="padding:6px 10px;border:1px solid #e5e7eb;background:#f9fafb;width:140px;">' + r[0] + '</td><td style="padding:6px 10px;border:1px solid #e5e7eb;"><b>' + escHtml(r[1]) + '</b></td></tr>';
    });
    Object.keys(data.adf_crit || {}).forEach(k => {
      html += '<tr><td style="padding:6px 10px;border:1px solid #e5e7eb;background:#f9fafb;">临界值 ' + escHtml(k) + '</td><td style="padding:6px 10px;border:1px solid #e5e7eb;">' + escHtml(data.adf_crit[k]) + '</td></tr>';
    });
    if (data.diff_is_stationary != null) {
      html += '<tr><td style="padding:6px 10px;border:1px solid #e5e7eb;background:#f9fafb;">差分后 ADF p 值</td><td style="padding:6px 10px;border:1px solid #e5e7eb;">' + escHtml(data.diff_adf_pval) + '（' + (data.diff_is_stationary ? '平稳' : '非平稳') + '）</td></tr>';
    }
    html += '<tr><td style="padding:6px 10px;border:1px solid #e5e7eb;background:#f9fafb;">结论</td><td style="padding:6px 10px;border:1px solid #e5e7eb;">' + conclusion + '</td></tr></table>';
    resultEl.innerHTML = html;
    chartEl.innerHTML = data.diff_chart ? b64ImgHtml(data.diff_chart) : '';
    card.classList.remove('hide');
    showResCard('ts-adf-res-card', 'ts-adf-stat', '✓ 检验完成（' + (data.is_stationary ? '平稳' : '非平稳') + '）');
    // 收集到本次会话图表
    if (typeof saveChartToReport === 'function') {
      saveChartToReport('', 'ADF平稳性检验', 'p=' + data.adf_pval + ', ' + (data.is_stationary ? '平稳' : '非平稳'), '时序分析', html + chartEl.innerHTML);
    }
    showToast('ADF 检验完成', 'success');
  } catch (e) {
    showToast('ADF 检验失败: ' + (e.message || ''), 'error');
  } finally {
    hideLoading();
  }
}

async function doARIMA() {
  const file = getCurrentFile();
  const dateCol = document.getElementById('ts-ar-date')?.value?.trim();
  const valCol = document.getElementById('ts-ar-val')?.value?.trim();
  const modelType = document.getElementById('ts-ar-type')?.value || 'auto';
  const p = parseInt(document.getElementById('ts-ar-p')?.value) || 2;
  const d = parseInt(document.getElementById('ts-ar-d')?.value) || 1;
  const q = parseInt(document.getElementById('ts-ar-q')?.value) || 1;
  const P = parseInt(document.getElementById('ts-ar-P')?.value) || 1;
  const D = parseInt(document.getElementById('ts-ar-D')?.value) || 0;
  const Q = parseInt(document.getElementById('ts-ar-Q')?.value) || 1;
  const S = parseInt(document.getElementById('ts-ar-S')?.value) || 7;
  const periods = parseInt(document.getElementById('ts-ar-periods')?.value) || 30;
  if (!file) { showToast('请选择数据文件', 'warning'); return; }
  if (!valCol) { showToast('请输入数值列', 'warning'); return; }
  showLoading('正在建模...');
  try {
    const res = await fetch('/api/ts/arima', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        file, date_col: dateCol || null, value_col: valCol, model_type: modelType,
        order: [p, d, q], seasonal_order: [P, D, Q, S], forecast_periods: periods
      })
    });
    const data = await res.json();
    const info = document.getElementById('ts-ar-info');
    const chart = document.getElementById('ts-ar-chart');
    const card = document.getElementById('ts-ar-res-card');
    if (!info || !chart || !card) { showToast('结果区域缺失（ts-ar-res-card/ts-ar-info/ts-ar-chart）', 'error'); return; }
    if (data.error) { showToast(data.error, 'error'); return; }
    let infoHtml = '模型 <b>' + escHtml(data.model_name || modelType) + '</b>（阶数 (' + (data.order || [p, d, q]).join(',') + ')' +
      (data.seasonal_order ? '，季节 (' + data.seasonal_order.join(',') + ')' : '') + '），AIC=<b>' + escHtml(data.aic) +
      '</b>，BIC=<b>' + escHtml(data.bic) + '</b>';
    if (data.rmse != null) infoHtml += '，RMSE=<b>' + escHtml(data.rmse) + '</b>，MAE=<b>' + escHtml(data.mae) + '</b>';
    if (data.forecast && data.forecast.length) {
      infoHtml += '<div style="font-size:12px;color:var(--ink-muted);margin-top:6px;">未来 ' + (data.forecast_periods || data.forecast.length) +
        ' 期预测值：<code>' + escHtml(data.forecast.join(', ')) + '</code></div>';
    }
    info.innerHTML = infoHtml;
    chart.innerHTML = data.chart ? b64ImgHtml(data.chart) : '';
    card.classList.remove('hide');
    showResCard('ts-ar-res-card', 'ts-ar-stat', '✓ 建模完成（' + (data.model_name || modelType) + '），AIC=' + data.aic);
    // 收集到本次会话图表
    if (typeof saveChartToReport === 'function') {
      saveChartToReport('', 'ARIMA/SARIMA预测', 'AIC=' + data.aic + (data.rmse != null ? ', RMSE=' + data.rmse : ''), '时序分析', chart.innerHTML);
    }
    showToast('ARIMA 建模完成', 'success');
  } catch (e) {
    showToast('ARIMA 建模失败: ' + (e.message || ''), 'error');
  } finally {
    hideLoading();
  }
}

// ===== hypothesis-test.js =====

// 根据检验类型生成对应的参数字段（ID 与 tab-ht.html 一致）
function htTypeChange() {
  const type = document.getElementById('ht-type')?.value;
  const area = document.getElementById('ht-params');
  if (!area) return;
  const fg = (label, id, ph, extra) =>
    '<div class="fg"><label>' + label + '</label><input id="' + id + '"' + (extra ? ' ' + extra : '') + ' placeholder="' + ph + '"></div>';
  let html = '';
  if (type === 'ttest_ind' || type === 'mannwhitneyu') {
    html = fg('数值列', 'ht-val1', 'value') +
      fg('分组列', 'ht-group', 'group') +
      fg('分组A值', 'ht-ga', '如 A') +
      fg('分组B值', 'ht-gb', '如 B');
  } else if (type === 'anova' || type === 'levene') {
    html = fg('数值列', 'ht-val1', 'value') + fg('分组列', 'ht-group', 'group');
  } else if (type === 'ttest_paired' || type === 'chi2' || type === 'pearsonr' || type === 'spearmanr') {
    html = fg('数值列 A', 'ht-val1', 'value') + fg('数值列 B', 'ht-val2', 'value2');
  } else if (type === 'ttest_1samp') {
    html = fg('数值列', 'ht-val1', 'value') + fg('假设均值', 'ht-popmean', '0', 'type="number" step="any"');
  } else {
    html = fg('数值列', 'ht-val1', 'value');
  }
  area.innerHTML = html;
}

// 渲染检验结果表格
function renderHtResult(type, r) {
  const conclusion = r.p_value != null
    ? (r.p_value < 0.05 ? '<span style="color:#16a34a;">显著（p&lt;0.05）</span>' : '<span style="color:#45B7D1;">不显著（p≥0.05）</span>')
    : '';
  const rows = [
    ['检验类型', type],
    ['统计量', r.statistic],
    ['p 值', r.p_value],
    ['自由度', r.dof],
    ['相关系数', r.correlation],
    ['z 值', r.z_score],
    ['结论', conclusion],
  ].filter(x => x[1] != null && x[1] !== '');
  let html = '<table style="width:100%;border-collapse:collapse;font-size:13px;margin:4px 0;">' +
    rows.map(x => '<tr><td style="padding:6px 10px;border:1px solid #e5e7eb;background:#f9fafb;width:130px;">' +
      escHtml(x[0]) + '</td><td style="padding:6px 10px;border:1px solid #e5e7eb;"><b>' +
      (x[1] === conclusion ? conclusion : escHtml(x[1])) + '</b></td></tr>').join('') + '</table>';
  if (r.sample_stats && Object.keys(r.sample_stats).length) {
    html += '<div style="font-size:13px;margin-top:10px;">样本统计：</div>' +
      '<table style="width:100%;border-collapse:collapse;font-size:12px;margin-top:4px;">' +
      Object.keys(r.sample_stats).map(k =>
        '<tr><td style="padding:4px 8px;border:1px solid #e5e7eb;">' + escHtml(k) +
        '</td><td style="padding:4px 8px;border:1px solid #e5e7eb;text-align:right;">' + escHtml(r.sample_stats[k]) + '</td></tr>'
      ).join('') + '</table>';
  }
  return html;
}

async function doHT() {
  const file = getCurrentFile();
  if (!file) { showToast('请选择数据文件', 'warning'); return; }
  const type = document.getElementById('ht-type')?.value;
  const val = id => document.getElementById(id)?.value?.trim();
  const body = { file, test_type: type };
  let need = [];

  if (type === 'ttest_ind' || type === 'mannwhitneyu') {
    need = ['ht-val1', 'ht-group', 'ht-ga', 'ht-gb'];
    body.value_col = val('ht-val1'); body.group_col = val('ht-group');
    body.group_a = val('ht-ga'); body.group_b = val('ht-gb');
  } else if (type === 'anova' || type === 'levene') {
    need = ['ht-val1', 'ht-group'];
    body.value_col = val('ht-val1'); body.group_col = val('ht-group');
  } else if (type === 'ttest_paired' || type === 'chi2' || type === 'pearsonr' || type === 'spearmanr') {
    need = ['ht-val1', 'ht-val2'];
    body.value_col = val('ht-val1'); body.value_col2 = val('ht-val2');
  } else if (type === 'ttest_1samp') {
    need = ['ht-val1'];
    body.value_col = val('ht-val1');
    body.popmean = parseFloat(val('ht-popmean')) || 0;
  } else {
    need = ['ht-val1'];
    body.value_col = val('ht-val1');
  }

  const missing = need.filter(id => !val(id));
  if (missing.length) { showToast('请填写必填字段：' + missing.map(escHtml).join('、'), 'warning'); return; }

  showLoading('正在执行检验...');
  try {
    const res = await fetch('/api/ht/run', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    });
    const data = await res.json();
    const el = document.getElementById('ht-result');
    const card = document.getElementById('ht-res-card');
    if (!el || !card) { showToast('结果区域缺失（ht-result/ht-res-card）', 'error'); return; }
    if (data.error) {
      el.innerHTML = '<span style="color:red">' + escHtml(data.error) + '</span>';
      card.classList.remove('hide');
      showToast(data.error, 'error');
      return;
    }
    el.innerHTML = renderHtResult(type, data);
    card.classList.remove('hide');
    window._htLastResult = { test: type, result: data, file };
    const stat = document.getElementById('ht-stat');
    if (stat) stat.textContent = '✓ 检验完成';
    // 收集到本次会话图表
    if (typeof saveChartToReport === 'function') {
      saveChartToReport('', '假设检验: ' + type, 'p=' + data.p_value, '假设检验', el.innerHTML);
    }
    showToast('假设检验完成', 'success');
  } catch (e) {
    showToast('假设检验失败: ' + (e.message || ''), 'error');
  } finally {
    hideLoading();
  }
}

// 假设检验可视化（POST /api/ht/viz）
async function htViz(chartType) {
  const file = getCurrentFile();
  if (!file) { showToast('请选择数据文件', 'warning'); return; }
  const valueCol = document.getElementById('ht-val1')?.value?.trim();
  const groupCol = document.getElementById('ht-group')?.value?.trim();
  if (chartType !== 'norm_fit' && !valueCol) { showToast('请填写数值列', 'warning'); return; }
  if ((chartType === 'mean_compare' || chartType === 'groups_box') && !groupCol) { showToast('此图表需要填写分组列', 'warning'); return; }
  showLoading('正在生成图表...');
  try {
    const res = await fetch('/api/ht/viz', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ file, chart_type: chartType, value_col: valueCol || null, group_col: groupCol || null })
    });
    const data = await res.json();
    const card = document.getElementById('ht-viz-card');
    const chart = document.getElementById('ht-viz-chart');
    if (!card || !chart) { showToast('图表区域缺失（ht-viz-card/ht-viz-chart）', 'error'); return; }
    if (data.error) { showToast(data.error === 'params' ? '图表参数不完整，请填写数值列/分组列' : data.error, 'error'); return; }
    chart.innerHTML = b64ImgHtml(data.chart, 'width:100%;border-radius:8px;');
    card.classList.remove('hide');
    showToast('图表已生成', 'success');
  } catch (e) {
    showToast('图表生成失败: ' + (e.message || ''), 'error');
  } finally {
    hideLoading();
  }
}

// 导出上次检验结果为 CSV
function htExportReport() {
  const last = window._htLastResult;
  if (!last) { showToast('请先执行一次检验再导出报告', 'warning'); return; }
  const r = last.result || {};
  const conclusion = r.p_value != null ? (r.p_value < 0.05 ? '显著 (p<0.05)' : '不显著 (p>=0.05)') : '';
  const rows = [
    ['检验类型', last.test],
    ['统计量', r.statistic],
    ['p 值', r.p_value],
    ['自由度', r.dof],
    ['相关系数', r.correlation],
    ['z 值', r.z_score],
    ['结论', conclusion],
  ].filter(x => x[1] != null && x[1] !== '');
  if (r.sample_stats && Object.keys(r.sample_stats).length) {
    rows.push(['--- 样本统计 ---', '']);
    Object.keys(r.sample_stats).forEach(k => rows.push([k, r.sample_stats[k]]));
  }
  const csv = rows.map(row => row.map(c => '"' + String(c == null ? '' : c).replace(/"/g, '""') + '"').join(',')).join('\r\n');
  const blob = new Blob(['\ufeff' + csv], { type: 'text/csv;charset=utf-8' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'hypothesis_test_report_' + last.test + '.csv';
  a.click();
  URL.revokeObjectURL(a.href);
  showToast('报告已导出为 CSV', 'success');
}

// ===== financial-statement.js =====

let _fsData = null;       // 三大报表数据 {income, balance, cashflow}
let _fsRatios = null;     // 财务比率
let _fsCurrentReport = 'income';
let _fsMarket = 'a';      // 市场：a= A股 / hk= 港股（影响比率计算路由）

// 渲染报表数据为表格（records）
function renderRecordsTable(records) {
  return recordsToTableHtml(records, 20);
}

// 财务比率中文标签映射
const _FS_RATIO_LABELS = {
  // 盈利能力
  revenue: '营业收入', net_profit: '净利润', net_margin: '净利率', gross_margin: '毛利率',
  roe: 'ROE(净资产收益率)', roe_diluted: '净资产收益率(摊薄)', eps: '每股收益',
  // 偿债能力
  current_ratio: '流动比率', quick_ratio: '速动比率', debt_ratio: '资产负债率',
  equity_ratio: '股东权益比率',
  // 成长能力
  revenue_growth: '营收同比增长', profit_growth: '净利润同比增长',
  // 营运能力
  inventory_turnover: '存货周转率', inventory_days: '存货周转天数', receivable_days: '应收账款周转天数',
  // 杜邦
  asset_turnover: '总资产周转率', equity_multiplier: '权益乘数',
};

// 渲染财务比率分区（全中文标签）
function renderRatiosHtml(ratios) {
  const catNames = { profitability: '盈利能力', solvency: '偿债能力', growth: '成长能力', operational: '营运能力' };
  let html = '';
  Object.keys(catNames).forEach(cat => {
    const items = ratios[cat] || {};
    const keys = Object.keys(items);
    if (!keys.length) return;
    html += '<div style="font-size:13px;color:var(--ink-muted);margin:12px 0 6px;"><b>' + catNames[cat] + '</b></div>';
    html += '<table style="width:100%;border-collapse:collapse;font-size:12px;">' +
      keys.map(k => {
        const label = _FS_RATIO_LABELS[k] || k;
        let val = items[k];
        // 比率类数值补百分号（营收/净利为金额单位已由后端带"亿"）
        if (['net_margin', 'gross_margin', 'roe', 'roe_diluted', 'debt_ratio', 'equity_ratio',
             'revenue_growth', 'profit_growth'].includes(k) && !/[%％]/.test(String(val))) {
          val = val + '%';
        }
        return '<tr><td style="padding:6px 10px;border:1px solid #e5e7eb;">' + escHtml(label) +
          '</td><td style="padding:6px 10px;border:1px solid #e5e7eb;text-align:right;"><b>' + escHtml(val) + '</b></td></tr>';
      }).join('') +
      '</table>';
  });
  return html || '<span style="color:var(--ink-subtle)">无比率数据</span>';
}

// 从比率值中提取数值（兼容 "21.13%"、"2248.42亿" 等）
function _fsNum(v) {
  if (v == null) return null;
  const m = String(v).match(/-?\d+(\.\d+)?/);
  return m ? parseFloat(m[0]) : null;
}

// 渲染杜邦分析（全中文）
function renderDupontHtml(d) {
  return '<div style="font-size:13px;line-height:2;">ROE = 净利率 × 总资产周转率 × 权益乘数<br>' +
    'ROE = <b>' + escHtml(d.roe) + '</b>% = <b>' + escHtml(d.net_margin) + '</b>% × <b>' + escHtml(d.asset_turnover) +
    '</b> × <b>' + escHtml(d.equity_multiplier) + '</b></div>';
}

// 根据算出的比率自动生成中文结论
function renderRatiosConclusion(ratios) {
  const p = ratios.profitability || {};
  const s = ratios.solvency || {};
  const g = ratios.growth || {};
  const du = ratios.dupont || {};
  const pts = [];

  // 盈利能力
  const roe = _fsNum(p.roe);
  const nm = _fsNum(p.net_margin);
  const gm = _fsNum(p.gross_margin);
  if (roe != null) {
    const lvl = roe >= 20 ? '优秀（≥20%）' : roe >= 15 ? '良好（15%~20%）' : roe >= 10 ? '一般（10%~15%）' : '偏弱（<10%）';
    pts.push('① <b>盈利能力</b>：ROE 为 ' + roe + '%，' + lvl + '，' +
      (nm != null ? '净利率 ' + nm + '%、' : '') + (gm != null ? '毛利率 ' + gm + '%，' : '') +
      (gm != null && nm != null ? (gm - nm > 20 ? '毛利与净利差距较大，说明费用端占比较高。' : '毛利转化为净利效率较高。') : '整体盈利质量' + (gm != null && gm >= 40 ? '较好。' : '一般。')));
  } else if (nm != null || gm != null) {
    pts.push('① <b>盈利能力</b>：' + (nm != null ? '净利率 ' + nm + '%、' : '') + (gm != null ? '毛利率 ' + gm + '%。' : ''));
  }

  // 偿债能力
  const dr = _fsNum(s.debt_ratio);
  const cr = _fsNum(s.current_ratio);
  if (dr != null) {
    const risk = dr < 40 ? '财务结构稳健，偿债压力小' : dr <= 60 ? '财务杠杆适中' : dr <= 70 ? '杠杆偏高，需关注偿债安全边际' : '杠杆过高，存在一定偿债风险';
    pts.push('② <b>偿债能力</b>：资产负债率 ' + dr + '%，' + risk + '。' +
      (cr != null ? ('流动比率 ' + cr + (cr >= 2 ? '，短期流动性充裕。' : cr >= 1 ? '，短期偿债能力正常。' : '，短期流动性偏紧，需关注。')) : ''));
  } else if (cr != null) {
    pts.push('② <b>偿债能力</b>：流动比率 ' + cr + (cr >= 2 ? '，短期流动性充裕。' : cr >= 1 ? '，短期偿债能力正常。' : '，短期流动性偏紧。'));
  }

  // 成长能力
  const rg = _fsNum(g.revenue_growth);
  const pg = _fsNum(g.profit_growth);
  if (rg != null || pg != null) {
    const rgt = rg == null ? '未知' : (rg >= 0 ? '正增长（+' + rg + '%）' : '下滑（' + rg + '%）');
    const pgt = pg == null ? '未知' : (pg >= 0 ? '正增长（+' + pg + '%）' : '下滑（' + pg + '%）');
    let trend = '营收与利润' + (rg != null && pg != null ? (rg >= 0 && pg >= 0 ? '同步向上，成长性良好。' : (rg < 0 && pg < 0 ? '同步承压，成长性偏弱。' : '出现背离，需关注利润质量。')) : '增速见上。');
    pts.push('③ <b>成长能力</b>：营收同比 ' + rgt + '，净利润同比 ' + pgt + '，' + trend);
  }

  // 杜邦拆解
  if (du && du.roe != null) {
    const em = _fsNum(du.equity_multiplier), at = _fsNum(du.asset_turnover), nm2 = _fsNum(du.net_margin);
    let driver = '驱动因素较均衡';
    if (em != null && em >= 2.5) driver = '主要依赖权益乘数（高杠杆）驱动，需关注财务风险';
    else if (nm2 != null && nm2 >= 15) driver = '主要依靠高净利率驱动，盈利质量较优';
    else if (at != null && at >= 0.8) driver = '依靠较高的资产周转效率驱动';
    pts.push('④ <b>杜邦拆解</b>：ROE ' + du.roe + '% = 净利率 ' + du.net_margin + '% × 总资产周转率 ' + du.asset_turnover + ' × 权益乘数 ' + du.equity_multiplier + '，' + driver + '。');
  }

  // 综合结论
  let overall = [];
  if (roe != null && roe >= 15 && dr != null && dr <= 60) overall.push('盈利与财务结构均较健康');
  if (rg != null && rg >= 0 && pg != null && pg >= 0) overall.push('保持增长');
  if (dr != null && dr > 70) overall.push('但杠杆偏高需警惕');
  if (roe != null && roe < 10) overall.push('但盈利能力偏弱');
  const summary = overall.length ? overall.join('，') + '。' : '整体表现中性，建议结合行业对比进一步判断。';
  pts.push('⑤ <b>综合结论</b>：' + summary);

  return '<div style="margin-top:14px;padding:14px 16px;border:1px solid var(--glass-border);border-radius:10px;background:rgba(34,211,238,.04);font-size:13px;line-height:2;color:var(--ink);">' +
    '<div style="font-weight:600;margin-bottom:8px;color:var(--ink);">📋 分析结论</div>' +
    pts.map(t => '<div style="margin:4px 0;">' + t + '</div>').join('') +
    '<div style="margin-top:10px;padding:8px 12px;border:1px dashed rgba(148,163,184,.45);border-radius:8px;font-size:12px;color:var(--ink-subtle);">⚠️ 本结论为客观数据分析，仅供参考，不构成任何投资建议。</div>' +
    '</div>';
}

// ===== 估值分析：DCF 绝对估值 + 可比公司相对估值 =====

function _fsErrHtml(msg) {
  return '<div style="padding:12px 14px;border:1px solid rgba(239,68,68,.3);border-radius:10px;background:rgba(239,68,68,.06);color:#f87171;font-size:13px;">⚠️ ' + escHtml(msg) + '</div>';
}

// 渲染 DCF 结果
function renderDcfHtml(d) {
  const color = d.verdict === 'undervalued' ? '#34d399' : d.verdict === 'overvalued' ? '#f87171' : '#fbbf24';
  const badge = d.verdict === 'undervalued' ? '📈 低估' : d.verdict === 'overvalued' ? '📉 高估' : '⚖️ 合理';
  let html = '<div style="padding:14px 16px;border:1px solid var(--glass-border);border-radius:10px;background:rgba(34,211,238,.03);">' +
    '<div style="font-weight:600;margin-bottom:10px;color:var(--ink);">💎 绝对估值 · DCF 现金流折现（' + escHtml(d.symbol) + ' ' + escHtml(d.name || '') + ' · ' + escHtml(d.market) + '）</div>' +
    '<div style="display:flex;gap:12px;flex-wrap:wrap;margin-bottom:12px;">' +
    '<div style="flex:1;min-width:120px;padding:10px 12px;border:1px solid var(--glass-border);border-radius:8px;background:rgba(255,255,255,.02);"><div style="font-size:12px;color:var(--ink-muted);">现价</div><div style="font-size:18px;font-weight:700;">' + escHtml(d.price) + '</div></div>' +
    '<div style="flex:1;min-width:120px;padding:10px 12px;border:1px solid var(--glass-border);border-radius:8px;background:rgba(255,255,255,.02);"><div style="font-size:12px;color:var(--ink-muted);">每股内在价值</div><div style="font-size:18px;font-weight:700;color:' + color + ';">' + escHtml(d.per_share) + '</div></div>' +
    '<div style="flex:1;min-width:120px;padding:10px 12px;border:1px solid var(--glass-border);border-radius:8px;background:rgba(255,255,255,.02);"><div style="font-size:12px;color:var(--ink-muted);">偏差</div><div style="font-size:18px;font-weight:700;color:' + color + ';">' + (d.spread_pct >= 0 ? '+' : '') + escHtml(d.spread_pct) + '%</div></div>' +
    '<div style="flex:1;min-width:120px;padding:10px 12px;border:1px solid ' + color + ';border-radius:8px;background:rgba(34,211,238,.05);"><div style="font-size:12px;color:var(--ink-muted);">结论</div><div style="font-size:16px;font-weight:700;color:' + color + ';">' + badge + '</div></div>' +
    '</div>' +
    '<div style="font-size:13px;color:var(--ink);line-height:1.9;margin-bottom:10px;">' + escHtml(d.verdict_cn) + '</div>';

  // 关键假设与中间值
  html += '<div style="font-size:13px;color:var(--ink);margin-bottom:10px;">' +
    '盈利基数：<b>' + escHtml(d.base_fcf_yi) + '</b> 亿（' + (d.base_mode === 'ocf' ? '经营现金流' : '净利润') + '）' +
    '　·　高增长率 <b>' + escHtml(d.g1) + '%</b>' +
    '　·　折现率 <b>' + escHtml(d.r) + '%</b>' +
    '　·　永续增长 <b>' + escHtml(d.g2) + '%</b>' +
    '　·　年限 <b>' + escHtml(d.years) + '</b> 年</div>';

  // 现金流预测表
  html += '<div style="font-size:12px;color:var(--ink-muted);margin:8px 0 4px;"><b>现金流预测</b>（FCF 单位：亿元）</div>' +
    '<table style="width:100%;border-collapse:collapse;font-size:12px;">' +
    '<thead><tr><th style="padding:6px 8px;border:1px solid var(--glass-border);background:#000;color:#fff;text-align:left;">年份</th>' +
    '<th style="padding:6px 8px;border:1px solid var(--glass-border);background:#000;color:#fff;text-align:right;">自由现金流(FCF)</th>' +
    '<th style="padding:6px 8px;border:1px solid var(--glass-border);background:#000;color:#fff;text-align:right;">折现值(PV)</th></tr></thead><tbody>' +
    (d.flows || []).map(f => '<tr><td style="padding:6px 8px;border:1px solid var(--glass-border);">第 ' + f.year + ' 年</td>' +
      '<td style="padding:6px 8px;border:1px solid var(--glass-border);text-align:right;">' + escHtml((f.fcf / 1e8).toFixed(2)) + '</td>' +
      '<td style="padding:6px 8px;border:1px solid var(--glass-border);text-align:right;">' + escHtml((f.pv / 1e8).toFixed(2)) + '</td></tr>').join('') +
    '</tbody></table>';
  html += '<div style="font-size:12px;color:var(--ink-muted);margin-top:6px;">终值(TV)：<b>' + escHtml(d.tv_yi) + '</b> 亿　·　企业价值(EV)：<b>' + escHtml(d.ev_yi) +
    '</b> 亿　·　净负债：<b>' + escHtml(d.net_debt_yi == null ? '0' : d.net_debt_yi) + '</b> 亿　·　股权价值：<b>' + escHtml(d.equity_yi) + '</b> 亿</div>';

  // 敏感性矩阵
  if (d.sensitivity) {
    const s = d.sensitivity;
    html += '<div style="font-size:12px;color:var(--ink-muted);margin:12px 0 4px;"><b>敏感性分析</b>（每股内在价值，行=增长率%，列=折现率%）</div>' +
      '<table style="width:100%;border-collapse:collapse;font-size:12px;">' +
      '<thead><tr><th style="padding:6px 8px;border:1px solid var(--glass-border);background:#000;color:#fff;text-align:left;">增长率 \\ 折现率</th>' +
      s.r_grid.map(rr => '<th style="padding:6px 8px;border:1px solid var(--glass-border);background:#000;color:#fff;text-align:right;">' + rr + '%</th>').join('') + '</tr></thead><tbody>' +
      s.matrix.map(row => '<tr><td style="padding:6px 8px;border:1px solid var(--glass-border);">' + row.g + '%</td>' +
        s.r_grid.map(rr => {
          const v = row[rr];
          const hl = (row.g === d.g1 && rr === d.r) ? ' style="color:#22d3ee;font-weight:700;"' : '';
          return '<td style="padding:6px 8px;border:1px solid var(--glass-border);text-align:right;"' + hl + '>' + (v == null ? '—' : escHtml(v)) + '</td>';
        }).join('') + '</tr>').join('') +
      '</tbody></table>';
  }
  html += '<div style="margin-top:12px;padding:8px 12px;border:1px dashed rgba(148,163,184,.45);border-radius:8px;font-size:12px;color:var(--ink-subtle);">⚠️ 本结论为客观数据分析，仅供参考，不构成任何投资建议。</div>';
  html += '</div>';
  return html;
}

// 渲染可比公司结果
function renderPeerHtml(d) {
  const rows = [d.target].concat(d.peers || []);
  const cols = [
    ['code', '代码'], ['name', '名称'], ['market', '市场'], ['price', '现价'],
    ['eps', 'EPS'], ['pe', 'PE'], ['bps', '每股净资产'], ['pb', 'PB'],
    ['roe', 'ROE%'], ['gross_margin', '毛利率%'], ['debt_ratio', '负债率%'],
    ['revenue_yi', '营收(亿)'], ['net_profit_yi', '净利(亿)'],
  ];
  let html = '<div style="padding:14px 16px;border:1px solid var(--glass-border);border-radius:10px;background:rgba(34,211,238,.03);">' +
    '<div style="font-weight:600;margin-bottom:10px;color:var(--ink);">📊 相对估值 · 可比公司分析（目标：' + escHtml(d.symbol) + '）</div>';
  if (d.verdict) {
    const color = d.verdict.pe_gap_pct != null ? (d.verdict.pe_gap_pct > 20 ? '#f87171' : d.verdict.pe_gap_pct < -20 ? '#34d399' : '#fbbf24') : '#a0a5b8';
    html += '<div style="font-size:13px;color:' + color + ';margin-bottom:10px;font-weight:600;">' + escHtml(d.verdict.label) + '</div>';
  }
  html += '<div style="overflow-x:auto;"><table style="width:100%;border-collapse:collapse;font-size:12px;white-space:nowrap;">' +
    '<thead><tr>' + cols.map(c => '<th style="padding:6px 8px;border:1px solid var(--glass-border);background:#000;color:#fff;text-align:left;">' + c[1] + '</th>').join('') + '</tr></thead><tbody>' +
    rows.map((r, i) => {
      const hl = i === 0 ? ' style="background:rgba(34,211,238,.06);font-weight:600;"' : '';
      return '<tr' + hl + '>' + cols.map(c => '<td style="padding:6px 8px;border:1px solid var(--glass-border);text-align:' + (['price', 'eps', 'pe', 'bps', 'pb', 'roe', 'gross_margin', 'debt_ratio', 'revenue_yi', 'net_profit_yi'].includes(c[0]) ? 'right' : 'left') + ';">' +
        escHtml(r[c[0]] == null ? '—' : r[c[0]]) + '</td>').join('') + '</tr>';
    }).join('') +
    '</tbody></table></div>';
  if (d.med_pe != null || d.med_pb != null) {
    html += '<div style="font-size:12px;color:var(--ink-muted);margin-top:8px;">行业中位数：PE <b>' + escHtml(d.med_pe == null ? '—' : d.med_pe) +
      '</b>　PB <b>' + escHtml(d.med_pb == null ? '—' : d.med_pb) + '</b></div>';
  }
  html += '<div style="font-size:12px;color:var(--ink-subtle);margin-top:6px;">提示：PE = 现价 ÷ 每股收益；PB = 现价 ÷ 每股净资产。相对估值需结合行业属性判断。</div>';
  html += '<div style="margin-top:10px;padding:8px 12px;border:1px dashed rgba(148,163,184,.45);border-radius:8px;font-size:12px;color:var(--ink-subtle);">⚠️ 本结论为客观数据分析，仅供参考，不构成任何投资建议。</div>';
  html += '</div>';
  return html;
}

async function doDCF() {
  const symbol = (document.getElementById('fs-symbol')?.value || document.getElementById('fs-peer-target')?.value || '').trim();
  if (!symbol) { showToast('请输入股票代码', 'warning'); return; }
  showLoading('正在计算 DCF 估值...');
  try {
    const res = await fetch('/api/fs/dcf', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        symbol,
        params: {
          base: document.getElementById('fs-dcf-base')?.value || 'net_profit',
          g1: (document.getElementById('fs-dcf-g1')?.value || '').trim(),
          r: document.getElementById('fs-dcf-r')?.value || 9,
          g2: document.getElementById('fs-dcf-g2')?.value || 2.5,
          years: document.getElementById('fs-dcf-years')?.value || 5,
        }
      })
    });
    const data = await res.json();
    const el = document.getElementById('fs-dcf-result');
    if (!el) { showToast('结果区域缺失（fs-dcf-result）', 'error'); return; }
    if (data.error) { el.innerHTML = _fsErrHtml(data.error); showToast(data.error, 'error'); return; }
    el.innerHTML = renderDcfHtml(data);
    showToast('DCF 估值完成', 'success');
  } catch (e) {
    showToast('DCF 计算失败: ' + (e.message || ''), 'error');
  } finally {
    hideLoading();
  }
}

async function doPeerCompare() {
  const symbol = (document.getElementById('fs-peer-target')?.value || document.getElementById('fs-symbol')?.value || '').trim();
  const peers = (document.getElementById('fs-peer-list')?.value || '').trim();
  if (!symbol) { showToast('请输入目标公司代码', 'warning'); return; }
  if (!peers) { showToast('请输入至少一家可比公司代码', 'warning'); return; }
  showLoading('正在获取可比公司数据...');
  try {
    const res = await fetch('/api/fs/compare', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ symbol, peers })
    });
    const data = await res.json();
    const el = document.getElementById('fs-peer-result');
    if (!el) { showToast('结果区域缺失（fs-peer-result）', 'error'); return; }
    if (data.error) { el.innerHTML = _fsErrHtml(data.error); showToast(data.error, 'error'); return; }
    el.innerHTML = renderPeerHtml(data);
    showToast('可比公司分析完成', 'success');
  } catch (e) {
    showToast('可比公司分析失败: ' + (e.message || ''), 'error');
  } finally {
    hideLoading();
  }
}

async function doFetchFS() {
  const symbol = document.getElementById('fs-symbol')?.value?.trim();
  if (!symbol) { showToast('请输入股票代码', 'warning'); return; }
  showLoading('正在获取财报...');
  try {
    const res = await fetch('/api/fs/fetch', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ symbol })
    });
    const data = await res.json();
    const card = document.getElementById('fs-data-card');
    if (!card) { showToast('结果区域缺失（fs-data-card）', 'error'); return; }
    if (data.error) { showToast(data.error, 'error'); return; }
    if (!data.data) { showToast('未获取到财报数据', 'error'); return; }
    _fsData = data.data;
    _fsMarket = data.market || 'a';
    card.classList.remove('hide');
    switchFSReport('income');
    const nameEl = document.getElementById('fs-stock-name');
    if (nameEl) nameEl.textContent = data.stock_name ? ('· ' + data.stock_name + '（' + data.symbol + '）') : '';
    const stat = document.getElementById('fs-stat');
    if (stat) {
      const nm = data.stock_name ? (data.stock_name + ' ') : '';
      stat.textContent = '✓ ' + nm + data.symbol + '（' + data.sina_code + '）已获取：利润表 ' + ((_fsData.income || []).length) +
        ' 期，资产负债表 ' + ((_fsData.balance || []).length) + ' 期，现金流量表 ' + ((_fsData.cashflow || []).length) + ' 期';
    }
    showToast('财报数据获取成功', 'success');
  } catch (e) {
    showToast('获取财报失败: ' + (e.message || ''), 'error');
  } finally {
    hideLoading();
  }
}

// 切换三大报表 Tab
function switchFSReport(type) {
  _fsCurrentReport = type;
  ['income', 'balance', 'cashflow'].forEach(t => {
    const btn = document.getElementById('fs-btn-' + t);
    if (btn) btn.classList.toggle('btn-p', t === type);
  });
  const content = document.getElementById('fs-report-content');
  if (!content) { showToast('结果区域缺失（fs-report-content）', 'error'); return; }
  const records = _fsData && _fsData[type] ? _fsData[type] : [];
  const names = { income: '利润表', balance: '资产负债表', cashflow: '现金流量表' };
  content.innerHTML = '<div style="font-size:13px;color:var(--ink-muted);margin-bottom:8px;"><b>' + (names[type] || type) +
    '</b>（' + records.length + ' 期）</div>' + renderRecordsTable(records);
}

async function doCalcFS() {
  const symbol = document.getElementById('fs-symbol')?.value?.trim();
  if (!symbol) { showToast('请输入股票代码', 'warning'); return; }
  showLoading('正在计算财务比率...');
  try {
    const res = await fetch('/api/fs/ratios', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ symbol, market: _fsMarket })
    });
    const data = await res.json();
    const el = document.getElementById('fs-ratios');
    const card = document.getElementById('fs-ratio-card');
    if (!el || !card) { showToast('结果区域缺失（fs-ratio-card/fs-ratios）', 'error'); return; }
    if (data.error) { showToast(data.error, 'error'); return; }
    if (!data.ratios) { showToast('未获取到财务比率', 'error'); return; }
    _fsRatios = data.ratios;
    el.innerHTML = renderRatiosHtml(data.ratios);
    const conclEl = document.getElementById('fs-conclusion');
    if (conclEl) conclEl.innerHTML = renderRatiosConclusion(data.ratios);
    card.classList.remove('hide');
    const dupontCard = document.getElementById('fs-dupont-card');
    const dupontInfo = document.getElementById('fs-dupont-info');
    if (dupontCard && dupontInfo && data.ratios.dupont) {
      dupontInfo.innerHTML = renderDupontHtml(data.ratios.dupont);
      dupontCard.classList.remove('hide');
    }
    const stat = document.getElementById('fs-stat');
    if (stat) stat.textContent = '✓ 财务比率计算完成（' + symbol + '）';
    // 收集到本次会话图表
    if (typeof saveChartToReport === 'function') {
      saveChartToReport('', '财务比率分析: ' + symbol, '', '财务报表', el.innerHTML);
    }
    showToast('财务比率计算完成', 'success');
  } catch (e) {
    showToast('比率计算失败: ' + (e.message || ''), 'error');
  } finally {
    hideLoading();
  }
}

let cleanLogEntries = [];

function updateCleanLog(msg) {
  const el = document.getElementById('ca-log');
  if (!el) return;
  const time = new Date().toLocaleTimeString('zh-CN', { hour12: false });
  cleanLogEntries.push('[' + time + '] ' + msg);
  el.innerHTML = cleanLogEntries.slice(-50).join('<br>');
  el.scrollTop = el.scrollHeight;
}

// 文本清洗
async function doCleanText() {
  const file = getCurrentFile();
  if (!file) return showToast('请选择数据文件', 'warning');
  showLoading('正在清洗文本...');
  try {
    const col = document.getElementById('ca-text-col')?.value?.trim();
    if (!col) return showToast('请输入目标列名', 'warning');
    const res = await fetch('/api/clean/text', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({
        file, column: col,
        trim_spaces: document.getElementById('ca-trim')?.checked,
        to_lower: document.getElementById('ca-lower')?.checked,
        remove_special: document.getElementById('ca-special')?.checked,
        tokenize: document.getElementById('ca-tokenize')?.checked,
      })
    });
    const data = await res.json();
    if (data.error) return showToast(data.error, 'error');
    showToast('清洗完成: ' + data.rows + ' 行', 'success');
    updateCleanLog('文本清洗: ' + col);
  } catch(e) { showToast('清洗失败', 'error'); }
  finally { hideLoading(); }
}

// 分类编码
async function doCleanEncode() {
  const file = getCurrentFile();
  if (!file) return showToast('请选择数据文件', 'warning');
  showLoading('正在编码...');
  try {
    const col = document.getElementById('ca-enc-col')?.value?.trim();
    if (!col) return showToast('请输入目标列名', 'warning');
    const res = await fetch('/api/clean/encode', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({
        file, col,
        enc_type: document.getElementById('ca-enc-type')?.value,
        drop_first: document.getElementById('ca-enc-drop')?.checked,
      })
    });
    const data = await res.json();
    if (data.error) return showToast(data.error, 'error');
    showToast('编码完成: ' + data.rows + ' 行, 新增 ' + (data.new_cols?.length || 0) + ' 列', 'success');
    updateCleanLog('编码: ' + col);
  } catch(e) { showToast('编码失败', 'error'); }
  finally { hideLoading(); }
}

// 日期标准化
async function doCleanDate() {
  const file = getCurrentFile();
  if (!file) return showToast('请选择数据文件', 'warning');
  showLoading('正在标准化日期...');
  try {
    const col = document.getElementById('ca-date-col')?.value?.trim();
    if (!col) return showToast('请输入目标列名', 'warning');
    const res = await fetch('/api/clean/date', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({
        file, col,
        date_fmt: document.getElementById('ca-date-fmt')?.value,
        remove_invalid: document.getElementById('ca-date-rm')?.checked,
      })
    });
    const data = await res.json();
    if (data.error) return showToast(data.error, 'error');
    showToast('标准化完成: ' + data.rows + ' 行', 'success');
    updateCleanLog('日期标准化: ' + col);
  } catch(e) { showToast('日期标准化失败', 'error'); }
  finally { hideLoading(); }
}

// 批量替换
async function doCleanReplace() {
  const file = getCurrentFile();
  if (!file) return showToast('请选择数据文件', 'warning');
  showLoading('正在替换...');
  try {
    const col = document.getElementById('ca-rep-col')?.value?.trim();
    const oldVal = document.getElementById('ca-rep-old')?.value;
    const newVal = document.getElementById('ca-rep-new')?.value;
    if (!col || !oldVal) return showToast('请输入目标列名和值', 'warning');
    const res = await fetch('/api/clean/replace', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({
        file, col, old_val: oldVal, new_val: newVal || null,
        is_regex: document.getElementById('ca-rep-regex')?.checked,
      })
    });
    const data = await res.json();
    if (data.error) return showToast(data.error, 'error');
    showToast('替换完成: ' + (data.replaced_count || 0) + ' 处', 'success');
    updateCleanLog('替换: ' + col + ' ' + oldVal + ' → ' + newVal);
  } catch(e) { showToast('替换失败', 'error'); }
  finally { hideLoading(); }
}

// 条件过滤
async function doCleanFilter() {
  const file = getCurrentFile();
  if (!file) return showToast('请选择数据文件', 'warning');
  showLoading('正在过滤...');
  try {
    const col = document.getElementById('ca-filt-col')?.value?.trim();
    if (!col) return showToast('请输入目标列名', 'warning');
    const res = await fetch('/api/clean/filter', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({
        file, col,
        op: document.getElementById('ca-filt-op')?.value,
        val: document.getElementById('ca-filt-val')?.value,
      })
    });
    const data = await res.json();
    if (data.error) return showToast(data.error, 'error');
    showToast('过滤完成: ' + (data.filtered_count || 0) + ' 行匹配', 'success');
    updateCleanLog('过滤: ' + col + ' ' + document.getElementById('ca-filt-op')?.value + ' ' + document.getElementById('ca-filt-val')?.value);
  } catch(e) { showToast('过滤失败', 'error'); }
  finally { hideLoading(); }
}

// 缺失值热力图
async function showMissingHeatmap() {
  const file = getCurrentFile();
  if (!file) return showToast('请选择数据文件', 'warning');
  showLoading('正在生成热力图...');
  try {
    const res = await fetch('/api/clean/missing-heatmap', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({ file })
    });
    const data = await res.json();
    if (data.error) return showToast(data.error, 'error');
    const chartEl = document.getElementById('ca-miss-chart');
    if (chartEl && data.chart) {
      chartEl.innerHTML = '<img src="data:image/png;base64,' + data.chart + '" style="max-width:100%;border-radius:8px;">';
    }
    showToast('热力图已生成', 'success');
  } catch(e) { showToast('热力图生成失败', 'error'); }
  finally { hideLoading(); }
}

// 初始化清洗日志
function initCleanAdv() {
  cleanLogEntries = [];
  updateCleanLog('日志已初始化');
  showToast('日志已初始化', 'success');
}

// 撤销上一步（仅前端提示）
function undoCleanAdv() {
  if (cleanLogEntries.length > 1) {
    cleanLogEntries.pop();
    const el = document.getElementById('ca-log');
    if (el) {
      el.innerHTML = cleanLogEntries.slice(-50).join('<br>');
      el.scrollTop = el.scrollHeight;
    }
    showToast('已撤销上一步', 'info');
  } else {
    showToast('没有可撤销的操作', 'warning');
  }
}

// 保存结果
function saveCleanAdv() {
  showToast('结果已保存', 'success');
}

// ============ 归档（软删除，可恢复） ============
async function archiveSelectedFiles() {
  const cbs = Array.from(document.querySelectorAll('#file-list .file-del-cb:checked'));
  if (!cbs.length) { showToast('请先勾选要归档的文件', 'warning'); return; }
  const paths = cbs.map(c => c.dataset.path).filter(p => p);
  if (!paths.length) { showToast('勾选的文件缺少路径信息', 'warning'); return; }
  if (!confirm('归档 ' + paths.length + ' 个文件？（归档=软删除，从列表隐藏，可恢复）')) return;
  try {
    const r = await fetch('/api/files/delete', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ paths: paths, archive: true })
    });
    const d = await r.json();
    if (d.ok || d.soft_deleted.length) {
      showToast('已归档 ' + (d.soft_deleted.length || d.deleted.length) + ' 个文件', 'success');
    } else {
      showToast('归档失败: ' + ((d.errors && d.errors[0] && d.errors[0].error) || '未知'), 'error');
    }
  } catch(e) { showToast('归档失败: ' + e.message, 'error'); }
  loadMultiFiles();
  if (typeof refreshGlobalFileSelect === 'function') refreshGlobalFileSelect();
}

// ============ 版本快照（数据版本控制） ============
async function snapshotFile(name) {
  if (!name) return;
  try {
    const r = await fetch('/api/snapshot/create', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ file: name })
    });
    const d = await r.json();
    if (d.ok) showToast('已创建快照: ' + d.sha.slice(0, 8), 'success');
    else showToast('快照失败: ' + (d.error || '未知'), 'error');
  } catch(e) { showToast('快照失败: ' + e.message, 'error'); }
}

async function showSnapshots(name) {
  if (!name) return;
  try {
    const r = await fetch('/api/snapshot/list?file=' + encodeURIComponent(name));
    const d = await r.json();
    if (!d.ok) { showToast(d.error || '加载失败', 'error'); return; }
    const snaps = d.snapshots || [];
    if (!snaps.length) { showToast('该文件暂无快照', 'info'); return; }
    const lines = snaps.slice(0, 10).map(function (s) {
      const t = new Date(s.timestamp * 1000);
      const ts = t.getFullYear() + '-' + String(t.getMonth()+1).padStart(2,'0') + '-' + String(t.getDate()).padStart(2,'0') + ' ' + String(t.getHours()).padStart(2,'0') + ':' + String(t.getMinutes()).padStart(2,'0');
      return ts + '  ' + (s.operation || '') + '  [' + s.id.slice(0,8) + ']  ' + (s.description || '');
    });
    const choice = prompt('「' + name + '」快照列表（输入序号回滚到对应版本）：\n\n' +
      lines.map(function (l, i) { return (i+1) + '. ' + l; }).join('\n') +
      '\n\n输入 0 取消');
    if (!choice) return;
    const idx = parseInt(choice, 10);
    if (isNaN(idx) || idx < 1 || idx > snaps.length) return;
    const s = snaps[idx - 1];
    if (!confirm('回滚到快照 ' + s.id.slice(0,8) + '？（将覆盖当前文件内容）')) return;
    const r2 = await fetch('/api/snapshot/rollback', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ file: name, sha: s.id })
    });
    const d2 = await r2.json();
    showToast(d2.ok ? '已回滚到快照 ' + s.id.slice(0,8) : (d2.error || '回滚失败'), d2.ok ? 'success' : 'error');
    loadMultiFiles();
  } catch(e) { showToast('快照加载失败: ' + e.message, 'error'); }
}

// ============ 文件标签（个人工作空间） ============
async function editFileTags(path, name) {
  if (!path) { showToast('内置文件不支持标签', 'warning'); return; }
  const cur = prompt('为「' + name + '」设置标签（逗号分隔，如：项目A, 财报, 2026）：');
  if (cur === null) return;
  const tags = cur.split(/[,，]/).map(function (t) { return t.trim(); }).filter(Boolean);
  try {
    const r = await fetch('/api/files/tags', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ path: path, tags: tags })
    });
    const d = await r.json();
    if (d.ok) { showToast('标签已更新', 'success'); loadMultiFiles(); }
    else showToast(d.error || '标签更新失败', 'error');
  } catch(e) { showToast('标签更新失败: ' + e.message, 'error'); }
}

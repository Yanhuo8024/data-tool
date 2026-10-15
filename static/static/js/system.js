// =====================================================================
// system.js — Text-to-SQL/Snapshot/RAG/NLQ
// =====================================================================

// ===== text-to-sql.js =====

async function sqlConnect() {
  const db_type = document.getElementById('sql-db-type')?.value;
  if (!db_type) { showToast('请选择数据库类型', 'warning'); return; }
  const payload = {
    db_type,
    host: document.getElementById('sql-host')?.value || 'localhost',
    port: parseInt(document.getElementById('sql-port')?.value) || undefined,
    database: document.getElementById('sql-database')?.value || '',
    username: document.getElementById('sql-user')?.value || '',
    password: document.getElementById('sql-password')?.value || '',
  };
  if (db_type === 'sqlite') {
    payload.database = document.getElementById('sql-sqlite-path')?.value || '';
  }
  showLoading('正在连接...');
  try {
    const res = await fetch('/api/sql/connect', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });
    const data = await res.json();
    const el = document.getElementById('sql-result');
    if (data.error || data.ok === false) {
      const msg = data.error || '连接失败';
      if (el) el.innerHTML = '<span style="color:red">' + msg + '</span>';
      showToast(msg, 'error');
      return;
    }
    // 连接成功：显示 schema 与查询区
    const schemaBox = document.getElementById('sql-schema-box');
    const queryBox = document.getElementById('sql-query-box');
    const schemaContent = document.getElementById('sql-schema-content');
    if (schemaBox) schemaBox.style.display = 'block';
    if (queryBox) queryBox.style.display = 'block';
    if (schemaContent && data.tables) schemaContent.textContent = data.tables.join(', ');
    if (el) el.innerHTML = '<span style="color:var(--success,#4caf50)">' + (data.message || '连接成功') + '</span>';
    showToast(data.message || '连接成功', 'success');
  } catch (e) {
    showToast('连接失败', 'error');
  } finally {
    hideLoading();
  }
}

function nlToSQL() {
  const q = document.getElementById('sql-nl-query')?.value?.trim();
  if (!q) { showToast('请输入问题', 'warning'); return; }
  showLoading('正在生成 SQL...');
  fetch('/api/sql/nl_to_sql', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ query: q, sqlite_path: (document.getElementById('sql-sqlite-path') || {}).value || '' })
  })
    .then(r => r.json())
    .then(data => {
      hideLoading();
      const el = document.getElementById('sql-result');
      if (!el) return;
      if (data.ok === false || data.error || (data.errors && data.errors.length)) {
        const m = (data.errors && data.errors.length) ? data.errors.join('；') : (data.error || '生成失败');
        el.innerHTML = '<span style="color:red">' + m + '</span>';
        return;
      }
      let html = '';
      if (data.sql) html += '<pre style="background:rgba(0,0,0,0.25);padding:10px;border-radius:6px;font-size:12px;">SQL: ' + data.sql + '</pre>';
      if (data.source) {
        html += '<div style="font-size:12px;color:var(--ink-subtle);margin-top:4px;">数据源: ' + data.source + '（本面板只生成 SQL，不执行）</div>';
      }
      el.innerHTML = html || '<span style="color:var(--ink-subtle)">无结果</span>';
    })
    .catch(e => { hideLoading(); showToast('生成失败', 'error'); });
}

function showManualSQL() {
  showToast('手动 SQL 执行暂未开放，请使用自然语言查询', 'info');
}

function takeSnapshot() {
  const file = (typeof getCurrentFile === 'function' ? getCurrentFile() : null) || window._selectedFile || '';
  if (!file) { showToast('请先选择要快照的文件', 'warning'); return; }
  showLoading('正在创建快照...');
  fetch('/api/snapshot/create', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ file })
  })
    .then(r => r.json())
    .then(data => {
      hideLoading();
      if (data.error || data.ok === false) { showToast(data.error || '创建失败', 'error'); return; }
      showToast('快照创建成功', 'success');
      loadSnapshots();
    })
    .catch(e => { hideLoading(); showToast('创建失败', 'error'); });
}

function loadSnapshots() {
  const file = (typeof getCurrentFile === 'function' ? getCurrentFile() : null) || window._selectedFile || '';
  const url = file ? '/api/snapshot/list?file=' + encodeURIComponent(file) : '/api/snapshot/list';
  fetch(url)
    .then(r => r.json())
    .then(data => {
      const el = document.getElementById('snapshot-list');
      const cnt = document.getElementById('snap-count');
      const snaps = data.snapshots || [];
      if (cnt) cnt.textContent = snaps.length + ' 个快照';
      if (el) {
        if (!snaps.length) {
          el.innerHTML = '<div style="color:#666;text-align:center;padding:20px;">暂无快照</div>';
          return;
        }
        el.innerHTML = snaps.map(s =>
          '<div class="snap-item" style="display:flex;justify-content:space-between;align-items:center;padding:8px 12px;border-bottom:1px solid rgba(255,255,255,0.06);font-size:13px;">' +
          '<span>' + (s.time || s.created_at || '') + ' — ' + (s.description || s.note || s.operation || '快照') + '</span>' +
          '<button class="btn btn-w btn-sm" onclick="rollbackTo(\'' + (s.sha || s.id) + '\')">回滚</button></div>'
        ).join('');
      }
    })
    .catch(() => {});
}

function rollbackTo(sha) {
  const file = (typeof getCurrentFile === 'function' ? getCurrentFile() : null) || window._selectedFile || '';
  if (!file) { showToast('请先选择文件', 'warning'); return; }
  if (!confirm('确定回滚到此快照?')) return;
  fetch('/api/snapshot/rollback', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ file, sha })
  })
    .then(r => r.json())
    .then(data => {
      if (data.error || data.ok === false) { showToast(data.error || '回滚失败', 'error'); return; }
      showToast('回滚成功', 'success');
      loadSnapshots();
    });
}

// ===== rag.js =====
// askRAG 由 tab-rag.html / tab-nlp.html 内联提供；文档上传统一在「知识库管理」页（uploadRAG 已移除）

function loadRAGDocs() {
  fetch('/api/rag/docs')
    .then(r => r.json())
    .then(data => {
      const docs = data.documents || [];
      const el = document.getElementById('rag-doc-list');
      if (el) {
        el.innerHTML = docs.map(d =>
          '<div class="rag-doc"><span>' + d.name + '</span>' +
          '<button type="button" onclick="askKnowledgeRAG()">问</button></div>'
        ).join('') || '<div class="rag-doc">暂无文档</div>';
      }
      const sel = document.getElementById('nlp-rag-docs');
      if (sel) {
        sel.innerHTML = '<option value="">（全部文档）</option>' + docs.map(d =>
          '<option value="' + String(d.name || '').replace(/"/g, '&quot;') + '">' +
          String(d.name || d.id || '') + '</option>'
        ).join('');
      }
    })
    .catch(() => {});
}

// ===== NLQ (Natural Language Query) =====
// runNLQ 由 tab-nlq.html 的 inline 完整版提供，此处不再覆盖

// ===== linked-charts.js =====

function loadLinkedCharts() {
  showToast('联动图表开发中', 'info');
}

/* lineage.js — 数据血缘追踪前端
 * 展示：来源统计 / 单文件溯源（上游+下游）/ 最近事件 / 链路图 */
(function () {
  'use strict';
  window.LG_LABELS = {};  // op -> 中文标签

  function fmtTs(ts) {
    if (!ts) return '';
    var d = new Date(ts * 1000);
    function p(n) { return n < 10 ? '0' + n : n; }
    return p(d.getMonth() + 1) + '-' + p(d.getDate()) + ' ' + p(d.getHours()) + ':' + p(d.getMinutes());
  }

  function opBadge(op) {
    var label = window.LG_LABELS[op] || op || '操作';
    return '<span style="display:inline-block;padding:2px 8px;border-radius:6px;font-size:11px;' +
      'background:rgba(94,106,210,.12);color:#5E6AD2;margin-right:6px;">' + label + '</span>';
  }

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  function eventsTable(events) {
    if (!events || !events.length) {
      return '<div style="padding:10px;color:var(--ink-subtle,#888);font-size:12px;">暂无事件记录</div>';
    }
    var rows = events.map(function (e) {
      return '<tr>' +
        '<td style="padding:6px 8px;white-space:nowrap;font-size:12px;color:var(--ink-subtle,#888);">' + fmtTs(e.ts) + '</td>' +
        '<td style="padding:6px 8px;white-space:nowrap;">' + opBadge(e.op) + '</td>' +
        '<td style="padding:6px 8px;font-size:12px;">' + (e.src ? esc(e.src) : '<span style="color:#aaa">—</span>') + '</td>' +
        '<td style="padding:6px 8px;font-size:12px;">' + (e.dst ? esc(e.dst) : '<span style="color:#aaa">—</span>') + '</td>' +
        '<td style="padding:6px 8px;font-size:12px;color:var(--ink-subtle,#888);">' + esc(e.detail) + '</td>' +
        '</tr>';
    }).join('');
    return '<table style="width:100%;border-collapse:collapse;font-size:12px;">' +
      '<thead><tr style="text-align:left;color:var(--ink-subtle,#888);border-bottom:1px solid rgba(0,0,0,.08);">' +
      '<th style="padding:6px 8px;">时间</th><th style="padding:6px 8px;">操作</th>' +
      '<th style="padding:6px 8px;">来源</th><th style="padding:6px 8px;">产出</th>' +
      '<th style="padding:6px 8px;">说明</th></tr></thead><tbody>' + rows + '</tbody></table>';
  }

  // ---- 统计 ----
  window.loadLineageStats = function () {
    var el = document.getElementById('lg-stats');
    if (!el) return;
    fetch('/api/lineage/stats').then(function (r) { return r.json(); }).then(function (d) {
      var s = d.stats || {};
      el.innerHTML = '<div style="display:flex;gap:10px;flex-wrap:wrap;">' +
        statChip('事件总数', s.total_events) + statChip('涉及文件', s.files) +
        statChip('生成报告', s.reports) + '</div>';
    }).catch(function () {});
  };
  function statChip(label, val) {
    return '<div style="flex:1;min-width:110px;padding:10px 14px;border-radius:10px;' +
      'background:rgba(94,106,210,.08);text-align:center;">' +
      '<div style="font-size:20px;font-weight:700;color:#FF6B6B;">' + (val == null ? '—' : val) + '</div>' +
      '<div style="font-size:11px;color:var(--ink-subtle,#888);">' + label + '</div></div>';
  }

  // ---- 最近事件 ----
  window.loadLineageEvents = function () {
    var el = document.getElementById('lg-events');
    if (!el) return;
    fetch('/api/lineage/events?limit=30').then(function (r) { return r.json(); }).then(function (d) {
      if (d.op_labels) window.LG_LABELS = d.op_labels;
      el.innerHTML = eventsTable(d.events || []);
    }).catch(function () { el.innerHTML = '<div style="color:#aaa;font-size:12px;">加载失败</div>'; });
  };

  // ---- 单文件溯源 ----
  window.queryLineage = function () {
    var inp = document.getElementById('lg-query');
    var el = document.getElementById('lg-updown');
    if (!inp || !el) return;
    var file = inp.value.trim();
    if (!file) { el.innerHTML = '<div style="color:#FF6B6B;font-size:12px;">请输入文件名</div>'; return; }
    el.innerHTML = '<div style="color:#888;font-size:12px;">查询中…</div>';
    fetch('/api/lineage/file?file=' + encodeURIComponent(file))
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (!d.ok) { el.innerHTML = '<div style="color:#FF6B6B;font-size:12px;">' + esc(d.error) + '</div>'; return; }
        var html = '<div style="display:grid;gap:12px;margin-top:10px;">';
        html += '<div><div style="font-size:12px;font-weight:600;margin-bottom:6px;">⬆️ 上游来源（' + (d.upstream.length) + '）</div>' +
          eventsTable(d.upstream) + '</div>';
        html += '<div><div style="font-size:12px;font-weight:600;margin-bottom:6px;">⬇️ 下游引用（' + (d.downstream.length) + '）</div>' +
          eventsTable(d.downstream) + '</div>';
        html += '</div>';
        el.innerHTML = html;
      })
      .catch(function () { el.innerHTML = '<div style="color:#FF6B6B;font-size:12px;">查询失败</div>'; });
  };

  // ---- 页面就绪 ----
  function init() {
    if (document.getElementById('tab-store')) {
      loadLineageStats();
      loadLineageEvents();
    }
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();

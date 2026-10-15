/* company-deep.js — 企业深度分析（主站 Tab） */
/* 启动兜底：即使主脚本尚未执行完，也先挂上全局入口，避免 ReferenceError */
(function () {
  'use strict';
  if (!window.__cdBridge) {
    window.__cdBridge = {};
    function _pending(name) {
      return function () {
        var impl = window.__cdBridge[name];
        if (typeof impl === 'function') return impl.apply(null, arguments);
        var el = document.getElementById('cd-fetch-stat');
        if (el) el.textContent = '企业深析脚本仍在加载，请稍候或强制刷新（Ctrl+F5）';
        console.warn('[company-deep] ' + name + ' 尚未就绪');
      };
    }
    window.cdAutoPipeline = _pending('cdAutoPipeline');
    window.cdQuickStart = _pending('cdQuickStart');
    window.cdExportReport = _pending('cdExportReport');
    window.cdFetchCompany = _pending('cdFetchCompany');
    window.cdSave = _pending('cdSave');
    window.cdExport = _pending('cdExport');
    window.cdFillDemo = _pending('cdFillDemo');
    window.cdBuildVerdict = _pending('cdBuildVerdict');
    window.cdAddRisk = _pending('cdAddRisk');
    window.cdAddPbStep = _pending('cdAddPbStep');
    window.cdCalcDcf = _pending('cdCalcDcf');
    window.cdCalcFinance = _pending('cdCalcFinance');
    window.cdSaveConclusions = _pending('cdSaveConclusions');
    window.cdInit = _pending('cdInit');
  }
})();

(function () {
  'use strict';

  var KEY = 'cd-state-v1';
  var state = load();

  var PRIMARY = [
    { id: 'ipo', t: '招股说明书', d: '行业格局 / 商业模式 / 竞争优劣势 / 募投 / 风险因素', tag: '必读',
      url: 'http://www.cninfo.com.cn', q: 'http://www.cninfo.com.cn/new/fulltextSearch?notautosubmit=&keyWord={name} 招股说明书' },
    { id: 'ar', t: '年报 · 财务指标', d: '三张表核心数据与趋势', tag: '必读',
      url: 'http://www.cninfo.com.cn', q: 'http://www.cninfo.com.cn/new/fulltextSearch?notautosubmit=&keyWord={name} 年度报告' },
    { id: 'mda', t: '年报 · MD&A', d: '收入拆分、毛利率变化原因、管理层展望', tag: '必读',
      url: 'http://www.cninfo.com.cn', q: 'http://www.cninfo.com.cn/new/fulltextSearch?notautosubmit=&keyWord={name} 年度报告 经营情况讨论' },
    { id: 'cninfo', t: '巨潮资讯网', d: '招股书 / 年报 / 问询函 / 临时公告', tag: '渠道',
      url: 'http://www.cninfo.com.cn', q: 'http://www.cninfo.com.cn/new/fulltextSearch?notautosubmit=&keyWord={name}' },
    { id: 'exchange', t: '交易所官网', d: '上市文件、监管信息、退市风险', tag: '渠道',
      url: 'http://www.sse.com.cn',
      qf: function (v) {
        var code = String(v.code || '');
        if (/^6\d{5}$/.test(code)) return 'https://www.sse.com.cn/assortment/stock/list/info/company/index.shtml?COMPANY_CODE=' + code;
        if (/^[023]\d{5}$/.test(code)) return 'https://www.szse.cn/certificate/individual/index.html?code=' + code;
        if (/^[48]\d{5}$/.test(code)) return 'https://www.neeq.com.cn';
        return 'http://www.sse.com.cn';
      } },
    { id: 'ir', t: '投资者关系页', d: '业绩说明会、路演材料', tag: '渠道',
      url: 'http://www.cninfo.com.cn', q: 'https://cn.bing.com/search?q={name} 投资者关系 业绩说明会' },
    { id: 'edgar', t: 'SEC EDGAR', d: '10-K / 10-Q / S-1（海外）', tag: '海外',
      url: 'https://www.sec.gov/edgar/search/', q: 'https://www.sec.gov/cgi-bin/browse-edgar?company={code}&CIK=&type=10-K&action=getcompany' }
  ];
  var SECONDARY = [
    { id: 'qcc', t: '企查查 / 天眼查', d: '工商股权穿透、对外投资', tag: '交叉',
      url: 'https://www.qcc.com', q: 'https://www.qcc.com/web/search?key={name}' },
    { id: 'court', t: '裁判文书 / 执行信息', d: '诉讼、被执行、失信', tag: '排雷',
      url: 'https://wenshu.court.gov.cn', q: 'https://cn.bing.com/search?q={name} 裁判文书 被执行 失信' },
    { id: 'ip', t: '知识产权局', d: '专利与商标布局', tag: '壁垒',
      url: 'https://pss-system.cponline.cnipa.gov.cn', q: 'https://cn.bing.com/search?q={name} 专利' },
    { id: 'report', t: '券商 / 行业研报', d: '第三方视角（仅辅助）', tag: '辅助',
      url: 'https://data.eastmoney.com/report/',
      qf: function (v) {
        var code = String(v.code || '');
        if (/^[0236]\d{5}$/.test(code)) return 'https://data.eastmoney.com/report/stock.jshtml?stockcode=' + code;
        return 'https://cn.bing.com/search?q=' + encodeURIComponent(String(v.name || '').trim() + ' 研究报告');
      } },
    { id: 'wind', t: 'Wind / 同花顺', d: '估值分位、同业数据', tag: '数据',
      url: 'https://www.10jqka.com.cn',
      qf: function (v) {
        var code = String(v.code || '');
        if (/^[0236]\d{5}$/.test(code)) return 'https://stockpage.10jqka.com.cn/' + code + '/';
        return 'https://cn.bing.com/search?q=' + encodeURIComponent(String(v.name || '').trim() + ' 估值 同业对比');
      } },
    { id: 'assoc', t: '行业协会 / 统计局', d: '行业规模增速官方口径', tag: '数据',
      url: 'https://data.stats.gov.cn', q: 'https://cn.bing.com/search?q={industry} 行业协会 统计数据' }
  ];
  var SRC_MAP = {};
  PRIMARY.concat(SECONDARY).forEach(function (it) { SRC_MAP[it.id] = it; });
  var FIN_RED = [
    { id: 'ocf', t: '经营现金流长期低于净利润', d: '利润与现金流背离是最强排雷信号之一' },
    { id: 'sd', t: '存贷双高', d: '账上大量现金仍高息举债' },
    { id: 'ar', t: '应收增速远超营收', d: '放宽信用刺激收入' },
    { id: 'inv', t: '存货异常膨胀', d: '滞销或减值风险' },
    { id: 'gw', t: '商誉占净资产过高', d: '并购减值地雷' },
    { id: 'audit', t: '审计非标 / 频繁换所', d: '会计师不敢背书' },
    { id: 'acc', t: '频繁变更会计政策', d: '可能调节利润' },
    { id: 'deduct', t: '扣非远低于净利润', d: '主业不赚钱靠补贴/投资收益' },
    { id: 'capex', t: '资本开支长期大于折旧', d: 'FCF 被吞噬' },
    { id: 'tax', t: '实际税率异常', d: '优惠依赖或递延操纵' }
  ];
  var BIZ_RED = [
    { id: 'cust', t: '客户高度集中', d: '大客户流失伤筋动骨' },
    { id: 'supp', t: '供应商高度集中', d: '关键原料/渠道被卡' },
    { id: 'pledge', t: '实控人高质押', d: '平仓与控制权风险' },
    { id: 'rel', t: '关联交易规模大', d: '利益输送/收入真实性' },
    { id: 'exec', t: '核心高管频繁离职', d: '经营不稳或内部分歧' },
    { id: 'guar', t: '对外担保规模大', d: '或有负债变实亏' },
    { id: 'tech', t: '技术路线面临替代', d: '行业被颠覆前夜' },
    { id: 'policy', t: '政策强监管收紧', d: '牌照/价格/数据风险' },
    { id: 'promise', t: '历史承诺多次落空', d: '管理层诚信执行力' },
    { id: 'suit', t: '重大未决诉讼/处罚', d: '持续经营与声誉' }
  ];

  function $(id) { return document.getElementById(id); }
  function val(id) { var el = $(id); return el ? el.value : ''; }
  function set(id, v) { var el = $(id); if (el) el.value = v == null ? '' : v; }
  function num(v) {
    if (v === '' || v == null) return null;
    var n = Number(v);
    return isFinite(n) ? n : null;
  }
  function fmt(n, d) {
    if (n == null || !isFinite(Number(n))) return '—';
    return Number(n).toFixed(d == null ? 1 : d);
  }
  function pct(n, d) {
    if (n == null || !isFinite(Number(n))) return '—';
    return Number(n).toFixed(d == null ? 1 : d) + '%';
  }
  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  function toast(msg) {
    if (typeof showToast === 'function') { showToast(msg, 'info'); return; }
    if (typeof window.showToast === 'function') { window.showToast(msg, 'info'); return; }
    console.log('[cd]', msg);
  }

  function defState() {
    return {
      company: { name: '', code: '', industry: '', market: 'A股', date: '', analyst: '', oneliner: '' },
      sources: { primary: {}, secondary: {}, ipo: '', mda: '', audit: '', research: '', web: '', fetch: {} },
      business: {
        size: '', growth: '', share: '', lifecycle: '', cr3: '', cr5: '', policy: '', score: '',
        what: '', ceiling: '', kfs: '', position: ''
      },
      model: {
        customer: '', value: '', channel: '', revenue: '', cost: '', cash: '', weakest: '',
        revMix: '', cust1: '', cust5: '', payment: '', pricing: '',
        formula: '', gm: '', nm: '', bizType: '', modelScore: ''
      },
      moat: {
        tech: 5, brand: 5, channel: 5, cost: 5, network: 5, license: 5,
        shareTrend: '', marginPeers: '', whyChoose: '', whyBlocked: '', dur: ''
      },
      finance: {
        rev: '', np: '', gp: '', ocf: '', fcf: '', assets: '', equity: '',
        cash: '', idebt: '', arG: '', invG: '', revG: '', roe: '', gw: '', payout: '', npG: '', gmD: ''
      },
      gov: {
        controller: '', hold: '', pledge: '', structure: '', stable: '', related: '',
        dividend: '', promise: '', incentive: '', score: ''
      },
      risks: [],
      val: {
        pe: '', pePct: '', pb: '', pbPct: '', ps: '', peerPe: '',
        dcfFcf: '', dcfG: '2', dcfWacc: '9', dcfShares: '', dcfNetcash: '0',
        verdict: '', reason: ''
      },
      red: { fin: {}, biz: {}, verdict: '', worst: '' },
      pb: {
        strength: '', weak: '', next: '', rival: '', move: '', impact: '',
        steps: [], conclusion: '', invest: ''
      },
      conclusions: { a1: '', a2: '', a3: '', a4: '', a5: '' }
    };
  }

  function load() {
    try {
      var raw = localStorage.getItem(KEY);
      if (!raw) return defState();
      return merge(defState(), JSON.parse(raw));
    } catch (e) { return defState(); }
  }
  function merge(base, extra) {
    if (!extra || typeof extra !== 'object') return base;
    Object.keys(extra).forEach(function (k) {
      if (extra[k] && typeof extra[k] === 'object' && !Array.isArray(extra[k]) &&
        base[k] && typeof base[k] === 'object' && !Array.isArray(base[k])) {
        merge(base[k], extra[k]);
      } else {
        base[k] = extra[k];
      }
    });
    return base;
  }
  function save(silent) {
    try { localStorage.setItem(KEY, JSON.stringify(state)); } catch (e) {}
    if (!silent) toast('企业分析已保存');
    renderFunnel();
  }

  /* ── 绑定 ── */
  function bindText(id, getter, setter) {
    var el = $(id);
    if (!el) return;
    el.value = getter() || '';
    el.addEventListener('input', function () {
      setter(el.value);
      save(true);
    });
    el.addEventListener('change', function () {
      setter(el.value);
      save(true);
    });
  }
  function bindChipGroup(id, path) {
    var box = $(id);
    if (!box) return;
    function get() { return path.split('.').reduce(function (o, k) { return o == null ? o : o[k]; }, state); }
    function apply() {
      var cur = get();
      Array.prototype.forEach.call(box.querySelectorAll('.cd-chip'), function (b) {
        b.classList.toggle('on', b.getAttribute('data-value') === cur);
      });
    }
    apply();
    box.addEventListener('click', function (e) {
      var btn = e.target.closest ? e.target.closest('.cd-chip') : null;
      if (!btn) return;
      var v = btn.getAttribute('data-value');
      setPath(path, get() === v ? '' : v);
      apply();
      save(true);
    });
  }
  function setPath(path, v) {
    var keys = path.split('.');
    var o = state;
    for (var i = 0; i < keys.length - 1; i++) {
      if (o[keys[i]] == null) o[keys[i]] = {};
      o = o[keys[i]];
    }
    o[keys[keys.length - 1]] = v;
  }
  function bindAll(map) {
    Object.keys(map).forEach(function (id) {
      bindText(id, function () {
        return map[id].split('.').reduce(function (o, k) { return o == null ? o : o[k]; }, state);
      }, function (v) { setPath(map[id], v); });
    });
  }

  /* ── 信源跳转 / 获取 ── */
  function cdSourceUrl(sid) {
    var it = SRC_MAP[sid];
    if (!it) return '';
    var c = state.company || {};
    var name = String(c.name || '').trim();
    var code = String(c.code || '').trim();
    var hk = String(c.market || '') === '港股' || /^HK/i.test(code) || (code.length === 5 && /^\d{5}$/.test(code));
    if (hk && ['ipo', 'ar', 'mda', 'cninfo', 'exchange'].indexOf(sid) >= 0) {
      return 'https://www1.hkexnews.hk/search/titlesearch.xhtml?lang=zh';
    }
    if (typeof it.qf === 'function') return it.qf(c);
    if (it.q && (name || code)) {
      return it.q.replace(/\{(name|code|industry)\}/g, function (_, k) {
        var v = k === 'industry' ? (String(c.industry || '').trim() || name) : (k === 'code' ? code : name);
        return encodeURIComponent(String(v || '').trim());
      }).replace(/ /g, '%20');
    }
    return it.url || '';
  }
  function renderFetchedHtml(sid) {
    var arr = (state.sources.fetch || {})[sid] || [];
    if (!arr.length) {
      return '<div class="cd-src-empty">尚未获取：点「获取信息」自动检索公开资料，或「跳转」到信源官网手动查阅。</div>';
    }
    return arr.map(function (r, i) {
      return '<div class="cd-src-item">' +
        '<a href="' + esc(r.url) + '" target="_blank" rel="noopener noreferrer">' + esc(r.title || '(无标题)') + '</a>' +
        (r.snippet ? '<div class="snip">' + esc(r.snippet) + '</div>' : '') +
        '<div class="flex" style="margin-top:4px;"><button type="button" class="cd-src-btn tiny" data-append="' + sid + ':' + i + '">＋摘录</button></div>' +
        '</div>';
    }).join('');
  }
  function renderFetchedInto(sid) {
    var box = document.querySelector('[data-res="' + sid + '"]');
    if (box) box.innerHTML = renderFetchedHtml(sid);
  }
  function cdFetchSource(sid, btn) {
    var it = SRC_MAP[sid];
    var c = state.company || {};
    if (!it) return;
    if (!String(c.name || '').trim() && !String(c.code || '').trim()) {
      toast('请先填写公司名称或代码，再获取信息');
      return;
    }
    var box = document.querySelector('[data-res="' + sid + '"]');
    if (btn) { btn.disabled = true; btn.textContent = '检索中…'; }
    if (box) box.innerHTML = '<div class="cd-src-loading">正在检索「' + esc(it.t) + '」相关公开信息…</div>';
    fetch('/api/company-deep/source', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        source_id: sid, name: c.name, code: c.code,
        industry: c.industry, market: c.market, top_k: 6
      })
    }).then(function (r) {
      return r.json().catch(function () { return { ok: false, error: 'HTTP ' + r.status }; });
    }).then(function (data) {
      if (btn) { btn.disabled = false; btn.textContent = '获取信息'; }
      if (!data || !data.ok) {
        if (box) box.innerHTML = '<div class="cd-src-empty">检索失败：' + esc((data && data.error) || '未知错误') + '</div>';
        toast('「' + it.t + '」检索失败：' + ((data && data.error) || ''));
        return;
      }
      state.sources.fetch = state.sources.fetch || {};
      state.sources.fetch[sid] = data.items || [];
      save(true);
      renderFetchedInto(sid);
      toast('「' + it.t + '」获取到 ' + (data.items || []).length + ' 条公开资料');
    }).catch(function (e) {
      if (btn) { btn.disabled = false; btn.textContent = '获取信息'; }
      if (box) box.innerHTML = '<div class="cd-src-empty">检索异常：' + esc(e.message || e) + '</div>';
      toast('「' + it.t + '」检索异常：' + (e.message || e));
    });
  }
  function cdAppendToNotes(spec) {
    var parts = String(spec || '').split(':');
    var sid = parts[0], idx = Number(parts[1]);
    var r = ((state.sources.fetch || {})[sid] || [])[idx];
    if (!r) return;
    var line = '【' + (SRC_MAP[sid] ? SRC_MAP[sid].t : sid) + '】' + (r.title || '') +
      (r.snippet ? ' — ' + r.snippet : '') + '（' + (r.url || '') + '）';
    var cur = String(state.sources.web || '').trim();
    state.sources.web = cur ? cur + '\n' + line : line;
    var el = $('cd-web-notes');
    if (el) el.value = state.sources.web;
    save(true);
    toast('已写入信源检索摘录');
  }

  /* ── 清单 ── */
  function renderChecks(boxId, items, store, onDone) {
    var box = $(boxId);
    if (!box) return;
    box.innerHTML = items.map(function (it) {
      var done = !!(store[it.id] && store[it.id].done);
      var src = !!(it.url || it.q || it.qf);
      var actions = '';
      if (src) {
        actions = '<div class="cd-src-actions">' +
          '<button type="button" class="cd-src-btn" data-go="' + it.id + '">跳转 ↗</button>' +
          '<button type="button" class="cd-src-btn alt" data-fetch="' + it.id + '">获取信息</button>' +
          '</div>';
      }
      return '<div class="cd-check ' + (done ? 'done' : '') + '" data-id="' + it.id + '">' +
        '<input type="checkbox" ' + (done ? 'checked' : '') + ' />' +
        '<div class="grow"><div class="t" data-toggle="1">' + esc(it.t) + ' <span style="font-size:10px;opacity:.7">' + esc(it.tag) + '</span></div>' +
        '<div class="d" data-toggle="1">' + esc(it.d) + '</div>' + actions + '</div></div>' +
        (src ? '<div class="cd-src-res" data-res="' + it.id + '">' + renderFetchedHtml(it.id) + '</div>' : '');
    }).join('');
    box.addEventListener('change', function (e) {
      var item = e.target.closest('.cd-check');
      if (!item) return;
      var id = item.getAttribute('data-id');
      store[id] = { done: !!e.target.checked };
      item.classList.toggle('done', !!e.target.checked);
      save(true);
      if (onDone) onDone();
    });
    box.addEventListener('click', function (e) {
      if (!e.target.closest) return;
      var go = e.target.closest('[data-go]');
      if (go) {
        var u = cdSourceUrl(go.getAttribute('data-go'));
        if (u) window.open(u, '_blank', 'noopener');
        return;
      }
      var fb = e.target.closest('[data-fetch]');
      if (fb) { cdFetchSource(fb.getAttribute('data-fetch'), fb); return; }
      var ap = e.target.closest('[data-append]');
      if (ap) { cdAppendToNotes(ap.getAttribute('data-append')); return; }
      var tg = e.target.closest('[data-toggle]');
      if (tg) {
        var item = tg.closest('.cd-check');
        var cb = item ? item.querySelector('input[type=checkbox]') : null;
        if (cb) {
          cb.checked = !cb.checked;
          cb.dispatchEvent(new Event('change', { bubbles: true }));
        }
      }
    });
  }

  /* ── 漏斗 ── */
  function stageProgress() {
    function filled(obj, keys) {
      var n = 0;
      keys.forEach(function (k) {
        var v = k.split('.').reduce(function (o, kk) { return o == null ? o : o[kk]; }, obj);
        if (String(v == null ? '' : v).trim()) n++;
      });
      return Math.round(n / keys.length * 100);
    }
    var p = {};
    var prim = PRIMARY.filter(function (x) { return state.sources.primary[x.id] && state.sources.primary[x.id].done; }).length;
    var sec = SECONDARY.filter(function (x) { return state.sources.secondary[x.id] && state.sources.secondary[x.id].done; }).length;
    var notes = ['ipo', 'mda', 'audit', 'research', 'web'].filter(function (k) { return String(state.sources[k] || '').trim(); }).length;
    p.sources = Math.round((prim / PRIMARY.length) * 50 + (sec / SECONDARY.length) * 20 + (notes / 5) * 30);
    p.business = filled(state.business, ['size', 'growth', 'share', 'lifecycle', 'cr3', 'cr5', 'policy', 'what', 'ceiling', 'kfs', 'position', 'score']);
    p.model = filled(state.model, ['customer', 'value', 'channel', 'revenue', 'cost', 'cash', 'weakest', 'revMix', 'cust1', 'cust5', 'payment', 'pricing', 'formula', 'gm', 'nm', 'bizType', 'modelScore']);
    p.moat = filled(state.moat, ['shareTrend', 'marginPeers', 'whyChoose', 'whyBlocked', 'dur']);
    p.finance = filled(state.finance, ['rev', 'np', 'gp', 'ocf', 'fcf', 'assets', 'equity', 'cash', 'idebt', 'arG', 'invG', 'revG', 'roe', 'gw', 'payout', 'npG', 'gmD']);
    p.governance = Math.round(filled(state.gov, ['controller', 'hold', 'pledge', 'structure', 'stable', 'related', 'dividend', 'promise', 'incentive', 'score']) * 0.7 +
      Math.min(state.risks.length / 3, 1) * 30);
    p.valuation = filled(state.val, ['pe', 'pePct', 'pb', 'pbPct', 'verdict', 'reason']);
    p.overall = Math.round(p.sources * 0.12 + p.business * 0.16 + p.model * 0.16 + p.moat * 0.12 +
      p.finance * 0.18 + p.governance * 0.12 + p.valuation * 0.14);
    return p;
  }

  function renderFunnel() {
    var p = stageProgress();
    var steps = [
      { id: 'sources', n: '01', t: '信息收集', sec: 'cd-sec-sources' },
      { id: 'business', n: '02', t: '业务行业', sec: 'cd-sec-business' },
      { id: 'model', n: '03', t: '商业模式', sec: 'cd-sec-model' },
      { id: 'moat', n: '04', t: '竞争壁垒', sec: 'cd-sec-moat' },
      { id: 'finance', n: '05', t: '财务验证', sec: 'cd-sec-finance' },
      { id: 'governance', n: '06', t: '治理风险', sec: 'cd-sec-gov' },
      { id: 'valuation', n: '07', t: '估值判断', sec: 'cd-sec-val' }
    ];
    var strip = $('cd-funnel');
    if (strip) {
      strip.innerHTML = steps.map(function (s) {
        return '<div class="cd-step" data-sec="' + s.sec + '">' +
          '<div class="n">' + s.n + '</div><div class="t">' + s.t + '</div>' +
          '<div class="bar"><i style="width:' + (p[s.id] || 0) + '%"></i></div></div>';
      }).join('');
      Array.prototype.forEach.call(strip.querySelectorAll('.cd-step'), function (el) {
        el.onclick = function () {
          var t = $(el.getAttribute('data-sec'));
          if (t) t.scrollIntoView({ behavior: 'smooth', block: 'start' });
        };
      });
    }
    var bizScore = ((num(state.business.score) || 0) / 5) * 60 + ((num(state.model.modelScore) || 0) / 5) * 40;
    var fin = finHealth();
    var riskDensity = state.risks.length
      ? Math.round((state.risks.filter(function (r) { return r.level === '高'; }).length * 2 +
        state.risks.filter(function (r) { return r.level === '中'; }).length) / state.risks.length * 100)
      : null;
    if ($('cd-meter-biz')) $('cd-meter-biz').textContent = (state.business.score || state.model.modelScore) ? Math.round(bizScore) : '—';
    if ($('cd-meter-fin')) $('cd-meter-fin').textContent = fin == null ? '—' : fin;
    if ($('cd-meter-risk')) $('cd-meter-risk').textContent = riskDensity == null ? '—' : riskDensity + '%';
  }

  function finHealth() {
    var f = state.finance;
    var ocf = num(f.ocf), np = num(f.np), equity = num(f.equity), assets = num(f.assets);
    var gp = num(f.gp), rev = num(f.rev);
    if (!np && !equity && !ocf && !rev) return null;
    var score = 50;
    var gm = rev && gp != null ? gp / rev * 100 : num(f.gm);
    var nm = rev && np != null ? np / rev * 100 : num(state.model.nm);
    var roe = num(f.roe);
    var cr = ocf != null && np ? ocf / np : null;
    var dr = assets && equity != null ? (assets - equity) / assets * 100 : null;
    if (gm != null) score += Math.min(15, (gm - 20) * 0.3);
    if (nm != null) score += Math.min(15, (nm - 5) * 0.4);
    if (roe != null) score += Math.min(15, (roe - 8) * 0.5);
    if (cr != null) score += cr >= 1 ? 12 : cr >= 0.7 ? 4 : -15;
    if (dr != null) score += dr < 40 ? 10 : dr < 60 ? 3 : -10;
    return Math.max(0, Math.min(100, Math.round(score)));
  }

  /* ── 财务 ── */
  function calcFinance() {
    var f = state.finance;
    var rev = num(f.rev), np = num(f.np), gp = num(f.gp);
    var ocf = num(f.ocf), assets = num(f.assets), equity = num(f.equity);
    var cash = num(f.cash), idebt = num(f.idebt);
    var gm = rev && gp != null ? gp / rev * 100 : null;
    var nm = rev && np != null ? np / rev * 100 : null;
    var roeIn = num(f.roe);
    var cr = ocf != null && np ? ocf / np : null;
    var dr = assets && equity != null ? (assets - equity) / assets * 100 : null;
    var cd = cash != null && idebt != null ? (idebt > 0 ? cash / idebt : (cash > 0 ? Infinity : null)) : null;

    if ($('cd-r-gm')) $('cd-r-gm').textContent = gm != null ? pct(gm) : '—';
    if ($('cd-r-nm')) $('cd-r-nm').textContent = nm != null ? pct(nm) : '—';
    if ($('cd-r-roe')) $('cd-r-roe').textContent = roeIn != null ? pct(roeIn) : '—';
    if ($('cd-r-ocfnp')) $('cd-r-ocfnp').textContent = cr != null ? fmt(cr, 2) : '—';
    set('cd-fin-cashratio', cr != null ? fmt(cr, 2) : '');
    set('cd-fin-debt-ratio', dr != null ? fmt(dr, 1) + '%' : '');
    set('cd-fin-cash-debt', cd === Infinity ? '∞（无有息负债）' : (cd != null ? fmt(cd, 2) : ''));

    var npm = nm, tat = rev && assets ? rev / assets : null, em = assets && equity ? assets / equity : null;
    var duRoe = npm != null && tat != null && em != null ? (npm / 100) * tat * em * 100 : roeIn;
    if ($('cd-d-npm')) $('cd-d-npm').textContent = npm != null ? '净利率 ' + fmt(npm) + '%' : '净利率 —';
    if ($('cd-d-tat')) $('cd-d-tat').textContent = tat != null ? '周转率 ' + fmt(tat, 2) : '周转率 —';
    if ($('cd-d-em')) $('cd-d-em').textContent = em != null ? '权益乘数 ' + fmt(em, 2) : '权益乘数 —';
    if ($('cd-d-roe')) $('cd-d-roe').textContent = duRoe != null ? 'ROE ' + fmt(duRoe) + '%' : '—';

    var ins = $('cd-dupont-insight');
    if (ins) {
      if (npm == null || tat == null || em == null) {
        ins.textContent = '填入收入、净利、总资产、净资产后解释 ROE 驱动源。';
      } else {
        var drivers = [
          { k: '高净利率（暴利/品牌溢价）', v: npm },
          { k: '高周转（薄利多销）', v: tat * 25 },
          { k: '高杠杆（负债驱动）', v: (em - 1) * 20 }
        ].sort(function (a, b) { return b.v - a.v; });
        var text = 'ROE ≈ ' + fmt(duRoe) + '%，主要驱动：' + drivers[0].k + '。';
        if (em >= 2.5) text += ' 杠杆偏高，关注偿债风险。';
        if (npm >= 20 && tat < 0.6) text += ' 典型「高毛利低周转」。';
        if (npm < 8 && tat >= 1.2) text += ' 典型「薄利多销」。';
        ins.textContent = text;
      }
    }

    // redflags
    var alerts = [];
    var arG = num(f.arG), invG = num(f.invG), revG = num(f.revG);
    var gw = num(f.gw), npG = num(f.npG);
    if (cr != null && cr < 1) {
      alerts.push({ t: '利润与现金流背离', d: 'OCF/净利润 = ' + fmt(cr, 2) + '，关注应收与存货占用。', type: cr < 0.5 ? 'danger' : 'warn' });
    } else if (cr != null) {
      alerts.push({ t: '现金含量健康', d: 'OCF/净利润 = ' + fmt(cr, 2) + ' ≥ 1。', type: 'ok' });
    }
    if (cash != null && idebt != null && assets && cash > assets * 0.15 && idebt > assets * 0.1) {
      alerts.push({ t: '疑似存贷双高', d: '大量货币资金与有息负债并存，需解释资金安排。', type: 'danger' });
    }
    if (arG != null && revG != null && arG > revG + 15) {
      alerts.push({ t: '应收增速超营收', d: '应收 +' + fmt(arG) + '% vs 营收 +' + fmt(revG) + '%。', type: 'warn' });
    }
    if (invG != null && revG != null && invG > revG + 20) {
      alerts.push({ t: '存货异常膨胀', d: '存货 +' + fmt(invG) + '% vs 营收 +' + fmt(revG) + '%。', type: 'warn' });
    }
    if (gw != null && equity && gw / equity > 0.3) {
      alerts.push({ t: '商誉占净资产过高', d: '商誉/净资产 = ' + fmt(gw / equity * 100, 1) + '%。', type: 'danger' });
    }
    if (dr != null && dr > 70) {
      alerts.push({ t: '资产负债率偏高', d: pct(dr) + '，关注有息负债结构。', type: 'warn' });
    }
    if (npG != null && num(f.gmD) != null && npG > 0 && num(f.gmD) < -3) {
      alerts.push({ t: '利润增长与毛利率背离', d: '增长可能靠放量或压费用，可持续性存疑。', type: 'warn' });
    }
    var box = $('cd-redflag-live');
    if (box) {
      box.innerHTML = alerts.length
        ? alerts.map(function (a) {
          return '<div class="cd-alert ' + a.type + '"><div>' + (a.type === 'ok' ? '✓' : '!') +
            '</div><div><b>' + esc(a.t) + '</b><br/>' + esc(a.d) + '</div></div>';
        }).join('')
        : '<div class="cd-alert info"><div>i</div><div>填写更多财务数据后自动扫描排雷信号。</div></div>';
    }
    renderFunnel();
    save(true);
  }

  /* ── 风险 / 推演 ── */
  function renderRisks() {
    var box = $('cd-risk-list');
    if (!box) return;
    if (!state.risks.length) {
      box.innerHTML = '<div class="cd-alert info"><div>i</div><div>尚未登记风险点。建议从招股书「风险因素」逐条提炼。</div></div>';
      return;
    }
    box.innerHTML = state.risks.map(function (r, i) {
      var cls = r.level === '高' ? 'danger' : r.level === '中' ? 'warn' : 'ok';
      return '<div class="cd-alert ' + cls + '"><div>' + (r.level === '高' ? '!' : 'i') + '</div>' +
        '<div class="grow"><b>' + esc(r.type) + ' · ' + esc(r.level) + '</b><br/>' + esc(r.text) + '</div>' +
        '<button type="button" class="btn btn-sm" data-del="' + i + '">删除</button></div>';
    }).join('');
    Array.prototype.forEach.call(box.querySelectorAll('[data-del]'), function (btn) {
      btn.onclick = function () {
        state.risks.splice(Number(btn.getAttribute('data-del')), 1);
        save(true);
        renderRisks();
      };
    });
  }

  function renderPb() {
    var tl = $('cd-pb-timeline');
    if (!tl) return;
    if (!state.pb.steps.length) {
      tl.innerHTML = '<div class="cd-tl-item"><div><b>尚未开始推演</b><br/><span style="font-size:12px;color:var(--ink-subtle)">从对手资源、动机与约束出发，假设「全力进攻」时的组合拳。</span></div></div>';
      return;
    }
    tl.innerHTML = state.pb.steps.map(function (s, i) {
      return '<div class="cd-tl-item"><div class="flex" style="justify-content:space-between"><b>Step ' + (i + 1) + '</b>' +
        '<button type="button" class="btn btn-sm" data-del="' + i + '">删除</button></div>' +
        '<div style="margin-top:6px;font-size:12px;line-height:1.6">' + esc(s) + '</div></div>';
    }).join('');
    Array.prototype.forEach.call(tl.querySelectorAll('[data-del]'), function (btn) {
      btn.onclick = function () {
        state.pb.steps.splice(Number(btn.getAttribute('data-del')), 1);
        save(true);
        renderPb();
      };
    });
  }

  /* ── 判断卡 ── */
  function autoA1() {
    var b = state.business, score = num(b.score);
    var label = score >= 4 ? '好赛道' : score >= 3 ? '一般赛道' : score ? '偏弱赛道' : '赛道未评估';
    var bits = [(state.company.name || '该公司') + '处于' + (b.lifecycle || '未标注') + '阶段的' + (state.company.industry || '所属行业') + '行业'];
    if (b.size && b.growth) bits.push('行业规模约 ' + b.size + ' 亿元、增速约 ' + b.growth + '%');
    if (b.policy) bits.push('政策：' + b.policy);
    if (state.model.bizType) bits.push('模式：' + state.model.bizType);
    return label + '。' + bits.join('；') + '。' + (b.what ? '本质：' + b.what : '');
  }
  function autoA2(avg) {
    var m = state.moat;
    var grade = avg >= 7 ? '护城河较深' : avg >= 5 ? '有一定壁垒' : avg >= 3 ? '壁垒偏薄' : '几乎无壁垒';
    var bits = ['六维护城河均分 ' + fmt(avg) + '/10（' + grade + '）'];
    if (state.business.share) bits.push('市占率 ' + state.business.share + '%');
    if (m.marginPeers) bits.push(m.marginPeers.slice(0, 50));
    return bits.join('；') + '。';
  }
  function autoA3(fin) {
    var f = state.finance, bits = [];
    bits.push(fin != null ? '财务健康分 ' + fin + '/100' : '财务数据尚未填全');
    if (f.rev && f.np) bits.push('净利率 ' + fmt(num(f.np) / num(f.rev) * 100) + '%');
    if (f.roe) bits.push('ROE ' + f.roe + '%');
    if (f.ocf && f.np) {
      var r = num(f.ocf) / num(f.np);
      bits.push(r >= 1 ? '经营现金流覆盖净利润' : '经营现金流未覆盖净利润，警惕应收/存货占用');
    }
    return bits.join('；') + '。';
  }
  function autoA4() {
    var g = state.gov, bits = [];
    if (g.controller) bits.push('实控人：' + g.controller);
    if (g.pledge) bits.push('质押率 ' + g.pledge + '%');
    if (state.risks.length) {
      var highs = state.risks.filter(function (r) { return r.level === '高'; }).map(function (r) { return r.text.slice(0, 24); });
      bits.push('主要风险（' + state.risks.length + ' 条）：' + (highs.slice(0, 3).join('；') || '详见风险登记'));
    } else bits.push('风险因素尚未系统登记');
    return bits.join('；') + '。';
  }
  function autoA5() {
    var v = state.val, bits = [];
    if (v.verdict) bits.push('明确判断：' + v.verdict);
    if (v.pe) bits.push('PE ' + v.pe);
    if (v.pePct) bits.push('历史分位 ' + v.pePct + '%');
    if (v.reason) bits.push(v.reason.slice(0, 60));
    return bits.length ? bits.join('；') + '。' : '估值模块尚未完成。';
  }

  function buildVerdict(fromButton) {
    var c = state.company;
    if ($('cd-jc-company')) $('cd-jc-company').textContent = c.name || '未命名分析项目';
    if ($('cd-jc-meta')) {
      $('cd-jc-meta').textContent = [c.code, c.industry, c.market, c.date, c.analyst ? '分析人：' + c.analyst : '']
        .filter(Boolean).join(' · ');
    }
    var fin = finHealth();
    var avg = (state.moat.tech + state.moat.brand + state.moat.channel + state.moat.cost + state.moat.network + state.moat.license) / 6;
    var highRisks = state.risks.filter(function (r) { return r.level === '高'; }).length;
    if (state.finance.gw && state.finance.equity && num(state.finance.gw) / num(state.finance.equity) > 0.3) highRisks++;

    var a1 = state.conclusions.a1 || autoA1();
    var a2 = state.conclusions.a2 || autoA2(avg);
    var a3 = state.conclusions.a3 || autoA3(fin);
    var a4 = state.conclusions.a4 || autoA4();
    var a5 = state.conclusions.a5 || autoA5();

    var vEl = $('cd-jc-verdict');
    if (vEl) {
      var cls = 'mid', label = '分析中';
      var p = stageProgress();
      if (p.overall < 40) label = '待完善';
      else if (state.val.verdict) {
        if ((state.val.verdict === '低估' || state.val.verdict === '合理') && (fin == null || fin >= 60) && highRisks === 0) {
          cls = 'good'; label = '可关注';
        } else if (state.val.verdict === '高估' || highRisks >= 2 || (fin != null && fin < 40)) {
          cls = 'bad'; label = '谨慎/回避';
        } else {
          cls = 'mid'; label = '中性/观察';
        }
      }
      vEl.className = 'cd-verdict ' + cls;
      vEl.textContent = label;
    }

    var qs = [
      { n: 1, q: '这门生意好不好？', a: a1 },
      { n: 2, q: '这家公司强不强？', a: a2 },
      { n: 3, q: '账靠不靠谱？', a: a3 },
      { n: 4, q: '谁在管、有什么风险？', a: a4 },
      { n: 5, q: '当前价格值不值？', a: a5 }
    ];
    var body = $('cd-jc-body');
    if (body) {
      body.innerHTML = qs.map(function (x) {
        return '<div class="cd-jq"><div class="q"><span class="n">' + x.n + '</span>' + x.q + '</div>' +
          '<div class="a">' + esc(x.a) + '</div></div>';
      }).join('');
    }
    ['a1', 'a2', 'a3', 'a4', 'a5'].forEach(function (k) {
      var el = $('cd-' + k);
      if (el && document.activeElement !== el) el.value = state.conclusions[k] || '';
    });
    if (fromButton) {
      var card = $('cd-verdict-card');
      if (card) card.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }
    save(true);
  }

  /* ── 一键拉取（兼容 analyze_company 真实结构） ── */
  window.cdFetchCompany = function () {
    var company = val('cd-name') || val('cd-code');
    if (!company) { toast('请先填写公司名称或代码'); return; }
    var stat = $('cd-fetch-stat');
    if (stat) stat.textContent = '拉取中…';
    fetch('/api/company-analysis', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ company: company })
    }).then(function (r) { return r.json(); }).then(function (data) {
      if (stat) stat.textContent = data && data.ok ? '拉取完成' : (data && data.error || '拉取失败');
      if (!data || data.error) return;
      var profile = data.profile || {};
      var fin = data.financial || {};
      var market = data.market || {};

      if (data.name) { set('cd-name', data.name); state.company.name = data.name; }
      if (profile.industry) { set('cd-industry', profile.industry); state.company.industry = profile.industry; }
      if (data.code) { set('cd-code', data.code); state.company.code = data.code; }

      if (fin.rev_yi != null) { set('cd-fin-rev', fin.rev_yi); state.finance.rev = String(fin.rev_yi); }
      if (fin.net_profit_yi != null) { set('cd-fin-np', fin.net_profit_yi); state.finance.np = String(fin.net_profit_yi); }
      if (fin.roe != null) { set('cd-fin-roe', fin.roe); state.finance.roe = String(fin.roe); }
      if (market.gross_margin != null) { set('cd-gm', market.gross_margin); state.model.gm = String(market.gross_margin); }
      if (market.net_margin != null) { set('cd-nm', market.net_margin); state.model.nm = String(market.net_margin); }
      if (market.pe != null) { set('cd-val-pe', market.pe); state.val.pe = String(market.pe); }
      if (market.pb != null) { set('cd-val-pb', market.pb); state.val.pb = String(market.pb); }

      if (Array.isArray(data.risks) && data.risks.length) {
        data.risks.forEach(function (r) {
          state.risks.push({
            type: (r.name && String(r.name).indexOf('估值') >= 0) ? '市场' : ((r.name && String(r.name).indexOf('杠杆') >= 0) ? '财务' : '经营'),
            level: r.level || '中',
            text: (r.name || '') + '：' + (r.desc || '')
          });
        });
        renderRisks();
      }

      save(true);
      calcFinance();
      buildVerdict();
      toast('公司画像已写入分析卡');
    }).catch(function (e) {
      if (stat) stat.textContent = '拉取失败：' + (e.message || e);
    });
  };

  /* ── 自动化流水线 ── */
  /** 阶段中文名（与后端 STAGE_NAMES 对齐，用于进度展示） */
  var CD_STAGE_LABEL = {
    resolve: '识别公司', profile: '公司概况', finance: '财务与杜邦',
    market: '行情与估值', news: '新闻与竞品', risk: '规则排雷',
    draft: '智能草稿', assemble: '组装分析卡'
  };
  var _cdPendTimer = null;

  function renderPipelineStages(stages) {
    var panel = $('cd-pipeline-panel');
    var box = $('cd-pipeline-stages');
    if (panel) panel.style.display = 'block';
    if (!box) return;
    box.innerHTML = (stages || []).map(function (s) {
      var cls = s.status === 'ok' ? 'ok' : (s.status === 'error' ? 'danger' : (s.status === 'warn' ? 'warn' : 'info'));
      var mark = s.status === 'ok' ? '✓' : (s.status === 'error' ? '✕' : (s.status === 'warn' ? '!' : '…'));
      var label = CD_STAGE_LABEL[s.id] || s.id;
      var code = s.error_code ? ' <span style="opacity:.65">[' + esc(s.error_code) + ']</span>' : '';
      return '<div class="cd-alert ' + cls + '"><div>' + mark + '</div><div class="grow"><b>' +
        esc(label) + '</b> — ' + esc(s.message || s.status) + code + '</div></div>';
    }).join('');
  }

  /**
   * 进行中提示。
   *
   * 背景：`/api/company-deep/auto` 是**同步**接口，一次性返回全部阶段结果，
   * 期间后端无法向前端推进度（真实推进需要 SSE）。此前前端会先画 8 行全
   * pending 的"阶段列表"，看上去像卡死在第一步 —— 属于**假进度**。
   * 现在改为：只显示一行计时提示，明确告知"正在拉取数据"，
   * 阶段明细等真实结果回来后一次性渲染。
   */
  function renderPipelinePending(company) {
    var panel = $('cd-pipeline-panel');
    var box = $('cd-pipeline-stages');
    if (panel) panel.style.display = 'block';
    if (!box) return;
    var t0 = Date.now();
    function paint() {
      var s = Math.round((Date.now() - t0) / 1000);
      box.innerHTML = '<div class="cd-alert info"><div>…</div><div class="grow">' +
        '<b>正在分析 ' + esc(company || '') + '</b> — 已用时 ' + s +
        ' 秒（依次取公司概况 / 财务 / 行情 / 新闻 / 同业，再规则排雷并生成草稿）' +
        '<br/><span style="opacity:.7">阶段明细将在完成后一次性列出</span></div></div>';
    }
    paint();
    if (_cdPendTimer) clearInterval(_cdPendTimer);
    _cdPendTimer = setInterval(paint, 1000);
  }

  function stopPipelinePending() {
    if (_cdPendTimer) { clearInterval(_cdPendTimer); _cdPendTimer = null; }
  }

  function refreshAllViews() {
    try { calcFinance(true); } catch (e) {}
    try { buildVerdict(); } catch (e) {}
    try { renderAnalysisResults(); } catch (e) {}
    try { renderFunnel(); } catch (e) {}
    try { updateMoat(); } catch (e) {}
    try { renderRisks(); } catch (e) {}
  }

  function renderAnalysisResults() {
    var panel = $('cd-results-panel');
    var body = $('cd-results-body');
    if (!panel || !body) return;
    panel.style.display = 'block';

    var c = state.company || {};
    var b = state.business || {};
    var m = state.model || {};
    var moat = state.moat || {};
    var f = state.finance || {};
    var g = state.gov || {};
    var v = state.val || {};
    var pb = state.pb || {};
    var red = state.red || {};
    var concl = state.conclusions || {};
    var risks = state.risks || [];

    function kv(pairs) {
      return '<div class="cd-result-kv">' + pairs.map(function (p) {
        return '<div class="k">' + esc(p[0]) + '</div><div class="v">' + esc(p[1] == null || p[1] === '' ? '—' : p[1]) + '</div>';
      }).join('') + '</div>';
    }
    function textBlock(t) {
      if (!t) return '';
      return '<div class="cd-result-text">' + esc(t) + '</div>';
    }
    function chips(arr) {
      var list = (arr || []).filter(Boolean);
      if (!list.length) return '';
      return '<div class="cd-result-chips">' + list.map(function (x) { return '<span>' + esc(x) + '</span>'; }).join('') + '</div>';
    }
    function filled() {
      var args = Array.prototype.slice.call(arguments);
      return args.some(function (x) { return x != null && String(x).trim() !== ''; }) ? ' filled' : '';
    }

    var moatAvg = (Number(moat.tech || 5) + Number(moat.brand || 5) + Number(moat.channel || 5) +
      Number(moat.cost || 5) + Number(moat.network || 5) + Number(moat.license || 5)) / 6;
    var ocfNp = (f.ocf != null && f.ocf !== '' && f.np) ? (Number(f.ocf) / Number(f.np)) : null;

    var html = '';

    // 头部
    html += '<div class="cd-result-block filled" style="border-color:rgba(91,140,255,.45);background:linear-gradient(135deg,rgba(91,140,255,.12),rgba(34,211,238,.06));">' +
      '<h4>公司概览 <span class="tag">已分析</span></h4>' +
      kv([
        ['公司', (c.name || '—') + (c.code ? '（' + c.code + '）' : '')],
        ['行业 / 市场', (c.industry || '—') + ' · ' + (c.market || '—')],
        ['一句话', c.oneliner || '—'],
        ['分析日期', c.date || '—']
      ]) +
      (concl.a1 ? textBlock('业务判断：' + concl.a1) : '') +
      '</div>';

    // ① 业务与行业
    html += '<div class="cd-result-block' + filled(b.what, b.ceiling, b.kfs, b.position, b.lifecycle, b.score) + '" id="cd-result-business">' +
      '<h4>① 业务与行业 <span class="tag">分析结果</span></h4>' +
      kv([
        ['生命周期', b.lifecycle || '—'],
        ['行业规模', b.size ? b.size + ' 亿元' : '—'],
        ['行业增速', b.growth ? b.growth + '%' : '—'],
        ['市占率', b.share ? b.share + '%' : '—'],
        ['CR3 / CR5', (b.cr3 || '—') + '% / ' + (b.cr5 || '—') + '%'],
        ['政策方向', b.policy || '—'],
        ['行业评分', b.score ? b.score + ' / 5' : '—']
      ]) +
      textBlock('【靠什么赚钱】' + (b.what || '待补充') + '\n【行业天花板】' + (b.ceiling || '待补充') + '\n【关键成功要素】' + (b.kfs || '待补充') + '\n【行业位置】' + (b.position || '待补充')) +
      chips([b.lifecycle, b.policy, b.score ? '行业分 ' + b.score : ''].filter(Boolean)) +
      '</div>';

    // ② 商业模式
    html += '<div class="cd-result-block' + filled(m.customer, m.value, m.channel, m.formula, m.weakest, m.bizType) + '" id="cd-result-model">' +
      '<h4>② 商业模式拆解 <span class="tag">分析结果</span></h4>' +
      kv([
        ['商业模式类型', m.bizType || '—'],
        ['回款模式', m.payment || '—'],
        ['定价权', m.pricing ? m.pricing + ' / 5' : '—'],
        ['毛利率 / 净利率', (m.gm || '—') + '% / ' + (m.nm || '—') + '%'],
        ['客户集中度', (m.cust1 || '—') + '% / 前五 ' + (m.cust5 || '—') + '%'],
        ['模式评分', m.modelScore ? m.modelScore + ' / 5' : '—']
      ]) +
      textBlock(
        '【商业闭环】\n① 客户：' + (m.customer || '—') +
        '\n② 价值主张：' + (m.value || '—') +
        '\n③ 渠道触达：' + (m.channel || '—') +
        '\n④ 收入结构：' + (m.revenue || m.revMix || '—') +
        '\n⑤ 成本结构：' + (m.cost || '—') +
        '\n⑥ 利润与回款：' + (m.cash || '—') +
        '\n\n【利润公式】' + (m.formula || '—') +
        '\n【最脆弱一环】' + (m.weakest || '—')
      ) +
      chips([m.bizType, m.payment, '定价权 ' + (m.pricing || '—')].filter(Boolean)) +
      '</div>';

    // ③ 竞争壁垒
    html += '<div class="cd-result-block' + filled(moat.whyChoose, moat.whyBlocked, moat.shareTrend, moatAvg) + '" id="cd-result-moat">' +
      '<h4>③ 竞争壁垒（护城河） <span class="tag">均分 ' + fmt(moatAvg, 1) + '/10</span></h4>' +
      kv([
        ['技术 / 专利', moat.tech],
        ['品牌心智', moat.brand],
        ['渠道网络', moat.channel],
        ['成本 / 规模', moat.cost],
        ['网络效应', moat.network],
        ['牌照 / 资质', moat.license],
        ['壁垒可持续性', moat.dur ? moat.dur + ' 级' : '—']
      ]) +
      textBlock(
        '【市占率变化】' + (moat.shareTrend || '—') +
        '\n【毛利率 vs 同行】' + (moat.marginPeers || '—') +
        '\n【客户为何非选它】' + (moat.whyChoose || '—') +
        '\n【对手为何打不进】' + (moat.whyBlocked || '—')
      ) +
      chips(['护城河 ' + fmt(moatAvg, 1) + '/10', moat.dur ? '持续性 ' + moat.dur : ''].filter(Boolean)) +
      '</div>';

    // ④ 财务验证
    html += '<div class="cd-result-block' + filled(f.rev, f.np, f.roe, ocfNp) + '" id="cd-result-finance">' +
      '<h4>④ 财务验证 <span class="tag">分析结果</span></h4>' +
      kv([
        ['营业收入', f.rev ? f.rev + ' 亿' : '—'],
        ['净利润', f.np ? f.np + ' 亿' : '—'],
        ['毛利率 / 净利率', (m.gm || '—') + '% / ' + (m.nm || '—') + '%'],
        ['ROE', f.roe ? f.roe + '%' : '—'],
        ['OCF / 净利润', ocfNp != null ? fmt(ocfNp, 2) : '—'],
        ['营收 / 净利增速', (f.revG || '—') + '% / ' + (f.npG || '—') + '%']
      ]) +
      textBlock(concl.a3 || '财务数据待补充后自动生成结论。') +
      '</div>';

    // ⑤ 治理与风险
    var riskLines = risks.slice(0, 6).map(function (r) {
      return '• [' + (r.level || '中') + '] ' + (r.type || '') + '：' + (r.text || '');
    });
    html += '<div class="cd-result-block' + filled(g.controller, risks.length, red.verdict) + '" id="cd-result-gov">' +
      '<h4>⑤ 治理与风险 <span class="tag">风险 ' + risks.length + ' 条</span></h4>' +
      kv([
        ['实际控制人', g.controller || '—'],
        ['持股 / 质押', (g.hold || '—') + '% / ' + (g.pledge || '—') + '%'],
        ['治理评分', g.score ? g.score + ' / 5' : '—'],
        ['排雷结论', red.verdict || '—'],
        ['最担心的雷', red.worst || '—']
      ]) +
      (riskLines.length ? textBlock('【风险登记】\n' + riskLines.join('\n')) : textBlock(concl.a4 || '风险因素待登记。')) +
      '</div>';

    // ⑥ 估值
    html += '<div class="cd-result-block' + filled(v.pe, v.verdict, v.reason) + '" id="cd-result-val">' +
      '<h4>⑥ 估值判断 <span class="tag">' + esc(v.verdict || '待定') + '</span></h4>' +
      kv([
        ['PE / PB', (v.pe || '—') + ' / ' + (v.pb || '—')],
        ['历史分位', v.pePct ? v.pePct + '%' : '—'],
        ['同行 PE 中位', v.peerPe || '—'],
        ['价格结论', v.verdict || '—']
      ]) +
      textBlock(concl.a5 || v.reason || '估值结论待补充。') +
      '</div>';

    // ⑦ 竞争推演
    html += '<div class="cd-result-block' + filled(pb.rival, pb.conclusion, pb.impact) + '" id="cd-result-playbook">' +
      '<h4>⑦ 竞争推演 <span class="tag">分析结果</span></h4>' +
      kv([
        ['最大对手', pb.rival || '—'],
        ['对手打法', pb.move || '—'],
        ['冲击评估', pb.impact || '—'],
        ['守不守得住', pb.conclusion || '—']
      ]) +
      textBlock('【我方优势】' + (pb.strength || '—') + '\n【我方软肋】' + (pb.weak || '—') + '\n【投资含义】' + (pb.invest || '—')) +
      '</div>';

    body.innerHTML = html;

    // 同步给下方表单区加「结果」提示条
    ['business', 'model', 'moat'].forEach(function (key) {
      var sec = $('cd-sec-' + key);
      if (!sec) return;
      sec.style.display = 'block';
    });
  }

  function applyPipelineState(newState) {
    if (!newState) return;
    Object.keys(newState).forEach(function (k) {
      if (newState[k] && typeof newState[k] === 'object' && !Array.isArray(newState[k]) &&
        state[k] && typeof state[k] === 'object' && !Array.isArray(state[k])) {
        Object.keys(newState[k]).forEach(function (kk) {
          if (newState[k][kk] !== '' && newState[k][kk] !== null && newState[k][kk] !== undefined) {
            state[k][kk] = newState[k][kk];
          }
        });
      } else if (Array.isArray(newState[k])) {
        state[k] = newState[k];
      } else if (newState[k] !== '' && newState[k] != null) {
        state[k] = newState[k];
      }
    });

    var map = {
      'cd-name': 'company.name', 'cd-code': 'company.code', 'cd-industry': 'company.industry',
      'cd-market': 'company.market', 'cd-date': 'company.date', 'cd-analyst': 'company.analyst',
      'cd-oneliner': 'company.oneliner',
      'cd-ipo-notes': 'sources.ipo', 'cd-mda-notes': 'sources.mda',
      'cd-audit-notes': 'sources.audit', 'cd-research-notes': 'sources.research',
      'cd-web-notes': 'sources.web',
      'cd-ind-size': 'business.size', 'cd-ind-growth': 'business.growth', 'cd-mkt-share': 'business.share',
      'cd-cr3': 'business.cr3', 'cd-cr5': 'business.cr5', 'cd-ind-score': 'business.score',
      'cd-biz-what': 'business.what', 'cd-biz-ceiling': 'business.ceiling',
      'cd-biz-kfs': 'business.kfs', 'cd-biz-position': 'business.position',
      'cd-loop-customer': 'model.customer', 'cd-loop-value': 'model.value', 'cd-loop-channel': 'model.channel',
      'cd-loop-revenue': 'model.revenue', 'cd-loop-cost': 'model.cost', 'cd-loop-cash': 'model.cash',
      'cd-loop-weakest': 'model.weakest',
      'cd-profit-formula': 'model.formula', 'cd-gm': 'model.gm', 'cd-nm': 'model.nm',
      'cd-biz-type': 'model.bizType', 'cd-model-score': 'model.modelScore', 'cd-pricing': 'model.pricing',
      'cd-share-trend': 'moat.shareTrend', 'cd-margin-peers': 'moat.marginPeers',
      'cd-why-choose': 'moat.whyChoose', 'cd-why-blocked': 'moat.whyBlocked', 'cd-moat-dur': 'moat.dur',
      'cd-fin-rev': 'finance.rev', 'cd-fin-np': 'finance.np', 'cd-fin-gp': 'finance.gp',
      'cd-fin-ocf': 'finance.ocf', 'cd-fin-assets': 'finance.assets', 'cd-fin-equity': 'finance.equity',
      'cd-fin-rev-g': 'finance.revG', 'cd-fin-np-g': 'finance.npG', 'cd-fin-roe': 'finance.roe',
      'cd-fin-cash': 'finance.cash', 'cd-fin-idebt': 'finance.idebt', 'cd-fin-gw': 'finance.gw',
      'cd-fin-fcf': 'finance.fcf', 'cd-fin-ar-g': 'finance.arG', 'cd-fin-inv-g': 'finance.invG',
      'cd-fin-payout': 'finance.payout', 'cd-fin-gm-d': 'finance.gmD',
      'cd-gov-controller': 'gov.controller', 'cd-gov-structure': 'gov.structure', 'cd-gov-score': 'gov.score',
      'cd-gov-hold': 'gov.hold', 'cd-gov-pledge': 'gov.pledge', 'cd-gov-stable': 'gov.stable',
      'cd-gov-related': 'gov.related', 'cd-gov-dividend': 'gov.dividend',
      'cd-gov-promise': 'gov.promise', 'cd-gov-incentive': 'gov.incentive',
      'cd-val-pe': 'val.pe', 'cd-val-pb': 'val.pb', 'cd-val-verdict': 'val.verdict', 'cd-val-reason': 'val.reason',
      'cd-val-pe-pct': 'val.pePct', 'cd-val-pb-pct': 'val.pbPct', 'cd-val-ps': 'val.ps',
      'cd-val-peer-pe': 'val.peerPe',
      'cd-dcf-fcf': 'val.dcfFcf', 'cd-dcf-shares': 'val.dcfShares', 'cd-dcf-netcash': 'val.dcfNetcash',
      'cd-red-verdict': 'red.verdict', 'cd-red-worst': 'red.worst',
      'cd-pb-strength': 'pb.strength', 'cd-pb-weak': 'pb.weak', 'cd-pb-rival': 'pb.rival',
      'cd-pb-move': 'pb.move', 'cd-pb-impact': 'pb.impact', 'cd-pb-conclusion': 'pb.conclusion',
      'cd-pb-invest': 'pb.invest',
      'cd-a1': 'conclusions.a1', 'cd-a2': 'conclusions.a2', 'cd-a3': 'conclusions.a3',
      'cd-a4': 'conclusions.a4', 'cd-a5': 'conclusions.a5'
    };
    Object.keys(map).forEach(function (id) {
      var path = map[id];
      var v = path.split('.').reduce(function (o, k) { return o == null ? o : o[k]; }, state);
      if (v !== undefined && v !== null && v !== '') set(id, v);
    });

    ['tech', 'brand', 'channel', 'cost', 'network', 'license'].forEach(function (k) {
      var el = $('cd-moat-' + k);
      var vEl = $('cd-moat-' + k + '-v');
      if (el && state.moat[k] != null) {
        el.value = state.moat[k];
        if (vEl) vEl.textContent = state.moat[k];
      }
    });

    function applyChip(id, cur) {
      var box = $(id);
      if (!box) return;
      Array.prototype.forEach.call(box.querySelectorAll('.cd-chip'), function (b) {
        b.classList.toggle('on', b.getAttribute('data-value') === cur);
      });
    }
    applyChip('cd-chip-lifecycle', state.business.lifecycle);
    applyChip('cd-chip-policy', state.business.policy);
    applyChip('cd-chip-payment', state.model.payment);

    renderRisks();
    renderPb();
    calcFinance();
    buildVerdict();
    updatePeMarker();
    save(true);
    // 强制展示完整分析结果（业务/模式/壁垒等）
    renderAnalysisResults();
  }

  function _cdAutoPipelineImpl(useAi) {
    if (useAi === undefined) useAi = true;
    // 优先用顶部快查框，其次公司档案
    var company = '';
    var quick = $('cd-quick-input');
    if (quick && String(quick.value || '').trim()) {
      company = String(quick.value).trim();
      if ($('cd-name') && !$('cd-name').value) {
        set('cd-name', company);
        state.company.name = company;
      }
    }
    if (!company) company = val('cd-name') || val('cd-code');
    if (!company) {
      if (quick) quick.focus();
      toast('请输入公司名称或代码');
      return;
    }
    var stat = $('cd-fetch-stat') || $('cd-hero-hint');
    if (stat) stat.textContent = '流水线运行中…（约 20–60 秒）';
    var actions = $('cd-hero-actions');
    if (actions) actions.style.display = 'none';

    renderPipelinePending(company);

    fetch('/api/company-deep/auto', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ company: company, use_ai: useAi !== false })
    }).then(function (r) {
      return r.json().catch(function () { return { ok: false, error: 'HTTP ' + r.status }; });
    }).then(function (data) {
      stopPipelinePending();
      renderPipelineStages(data.stages || []);
      if (!data.ok) {
        if (stat) stat.textContent = '流水线失败：' + (data.error || '未知错误');
        toast('自动分析失败：' + (data.error || ''));
        return;
      }
      applyPipelineState(data.state);
      renderAnalysisResults();
      if (stat) {
        var _deg = (data.meta && data.meta.degraded_stages) || [];
        stat.textContent = '分析完成 · ' + (data.elapsed_sec || '?') + 's · 草稿' +
          ((data.meta && data.meta.draft_source === 'llm') ? 'AI' : '规则') +
          (_deg.length ? ' · [' + _deg.length + ' 个阶段降级，见上方明细]' : '') + ' · 结果已展示';
      }
      // 降级显式提示：避免用户把"某阶段没取到数"误当成"该公司没有该数据"
      if (data.meta && (data.meta.degraded_stages || []).length) {
        var _names = (data.meta.degraded_stages || []).map(function (s) {
          return (CD_STAGE_LABEL[s.id] || s.id) + (s.error_code ? '(' + s.error_code + ')' : '');
        }).join('、');
        toast('注意：以下阶段降级，相关字段留空 — ' + _names);
      }
      if (actions) actions.style.display = 'inline-flex';
      toast('自动分析完成，结果已完整展示');
      // 必须展示在前端：滚到「分析结果总览」
      setTimeout(function () {
        var panel = $('cd-results-panel');
        if (panel) panel.scrollIntoView({ behavior: 'smooth', block: 'start' });
      }, 250);
    }).catch(function (e) {
      stopPipelinePending();
      if (stat) stat.textContent = '流水线异常：' + (e.message || e);
      toast('流水线异常：' + (e.message || e));
    });
  }

  /** 快查入口：只输公司名，回车/点按钮即自动分析 */
  function _cdQuickStartImpl() {
    var quick = $('cd-quick-input');
    var v = quick ? String(quick.value || '').trim() : '';
    if (!v) {
      if (quick) quick.focus();
      toast('请输入公司名称');
      return;
    }
    if ($('cd-name')) set('cd-name', v);
    state.company.name = state.company.name || v;
    // 若像代码则写入 code
    if (/^\d{6}$/.test(v) || /^HK\d{3,4}$/i.test(v)) {
      if ($('cd-code')) set('cd-code', v);
      state.company.code = state.company.code || v;
    }
    _cdAutoPipelineImpl(true);
  }

  window.cdAutoPipeline = _cdAutoPipelineImpl;
  window.cdQuickStart = _cdQuickStartImpl;
  window.cdRenderResults = renderAnalysisResults;

  window.cdSave = function () { save(false); };
  window.cdExport = function () {
    var blob = new Blob([JSON.stringify(state, null, 2)], { type: 'application/json' });
    var a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = (state.company.name || '企业分析') + '.json';
    a.click();
    setTimeout(function () { URL.revokeObjectURL(a.href); }, 1000);
    toast('已导出 JSON');
  };

  window.cdExportReport = function (fmt) {
    fmt = (fmt || 'docx').toLowerCase();
    if (!state.company || !state.company.name) {
      toast('请先填写公司名称');
      return;
    }
    // 先同步当前结论输入
    ['a1', 'a2', 'a3', 'a4', 'a5'].forEach(function (k) {
      var el = $('cd-' + k);
      if (el) state.conclusions[k] = el.value;
    });
    save(true);

    var template = 'navy_business';
    var tsel = $('cd-ppt-template');
    if (tsel && tsel.value) {
      template = tsel.value;
      try { localStorage.setItem('cd-ppt-template', template); } catch (e) {}
    } else {
      try { template = localStorage.getItem('cd-ppt-template') || 'navy_business'; } catch (e) {}
    }

    var stat = $('cd-export-stat') || $('cd-hero-hint');
    if (stat) stat.textContent = '正在生成 ' + fmt.toUpperCase() + '…';

    fetch('/api/company-deep/export', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ state: state, format: fmt, template: template })
    }).then(function (res) {
      if (!res.ok) {
        return res.json().catch(function () { return { error: 'HTTP ' + res.status }; }).then(function (j) {
          throw new Error((j && j.error) || ('HTTP ' + res.status));
        });
      }
      return res.blob();
    }).then(function (blob) {
      var url = URL.createObjectURL(blob);
      var a = document.createElement('a');
      a.href = url;
      a.download = (state.company.name || '企业深度分析') + '_深度分析报告.' + fmt;
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(function () { URL.revokeObjectURL(url); }, 2000);
      if (stat) stat.textContent = fmt.toUpperCase() + ' 已下载';
      toast(fmt.toUpperCase() + ' 报告已生成');
    }).catch(function (e) {
      if (stat) stat.textContent = '生成失败：' + (e.message || e);
      toast('报告生成失败：' + (e.message || e));
    });
  };
  window.cdCalcFinance = function () { calcFinance(); toast('财务指标已计算'); };
  window.cdBuildVerdict = function (scroll) { buildVerdict(!!scroll); };
  window.cdAddRisk = function () {
    var text = val('cd-risk-text');
    if (!text) { toast('请填写风险描述'); return; }
    state.risks.push({ type: val('cd-risk-type') || '经营', level: val('cd-risk-level') || '中', text: text });
    set('cd-risk-text', '');
    save(true);
    renderRisks();
  };
  window.cdAddPbStep = function () {
    var t = val('cd-pb-step');
    if (!t) { toast('请填写推演步骤'); return; }
    state.pb.steps.push(t);
    set('cd-pb-step', '');
    save(true);
    renderPb();
  };
  window.cdCalcDcf = function () {
    var fcf = num(val('cd-dcf-fcf')), g = num(val('cd-dcf-g')), wacc = num(val('cd-dcf-wacc'));
    var shares = num(val('cd-dcf-shares')), netCash = num(val('cd-dcf-netcash')) || 0;
    if (fcf == null || g == null || wacc == null) { toast('请填写 FCF、g、WACC'); return; }
    var w = wacc / 100, gg = g / 100;
    if (w <= gg) { if ($('cd-dcf-result')) $('cd-dcf-result').textContent = 'WACC 必须大于 g'; return; }
    var ev = fcf * (1 + gg) / (w - gg);
    var eq = ev + netCash;
    if ($('cd-dcf-result')) {
      $('cd-dcf-result').textContent = shares && shares > 0 ? '¥ ' + fmt(eq / shares, 2) : '企业价值 ' + fmt(eq) + ' 亿';
    }
  };
  window.cdSaveConclusions = function () {
    ['a1', 'a2', 'a3', 'a4', 'a5'].forEach(function (k) {
      state.conclusions[k] = val('cd-' + k);
    });
    save(true);
    buildVerdict();
    toast('结论已更新');
  };
  window.cdFillDemo = function () {
    set('cd-name', '示例生物科技');
    set('cd-code', '300760');
    set('cd-industry', '医疗器械');
    set('cd-oneliner', '研发销售高毛利体外诊断设备与试剂');
    set('cd-ind-size', '600');
    set('cd-ind-growth', '12');
    set('cd-mkt-share', '8');
    set('cd-ind-score', '4');
    set('cd-biz-what', '向医院销售 IVD 设备+试剂，靠装机带动耗材复购');
    set('cd-fin-rev', '120');
    set('cd-fin-np', '28');
    set('cd-fin-gp', '72');
    set('cd-fin-ocf', '22');
    set('cd-fin-assets', '180');
    set('cd-fin-equity', '120');
    set('cd-fin-cash', '40');
    set('cd-fin-idebt', '18');
    set('cd-fin-gw', '40');
    set('cd-fin-ar-g', '35');
    set('cd-fin-rev-g', '12');
    set('cd-fin-roe', '22.5');
    set('cd-val-pe', '28');
    set('cd-val-pe-pct', '25');
    set('cd-val-verdict', '合理');
    state.company.name = '示例生物科技';
    state.company.code = '300760';
    state.company.industry = '医疗器械';
    state.business.score = '4';
    state.business.lifecycle = '成长期';
    state.business.size = '600';
    state.business.growth = '12';
    state.business.share = '8';
    state.business.what = '向医院销售 IVD 设备+试剂，靠装机带动耗材复购';
    state.model.modelScore = '4';
    state.model.gm = '60';
    state.model.nm = '23.3';
    // chips
    var lc = document.querySelector('#cd-chip-lifecycle .cd-chip[data-value="成长期"]');
    if (lc) lc.classList.add('on');
    state.finance.rev = '120'; state.finance.np = '28'; state.finance.gp = '72';
    state.finance.ocf = '22'; state.finance.assets = '180'; state.finance.equity = '120';
    state.finance.cash = '40'; state.finance.idebt = '18'; state.finance.gw = '40';
    state.finance.arG = '35'; state.finance.revG = '12'; state.finance.roe = '22.5';
    state.val.pe = '28'; state.val.pePct = '25'; state.val.verdict = '合理';
    save(true);
    calcFinance();
    buildVerdict();
    renderAnalysisResults();
    toast('已填入示例数据');
  };

  /* ── moat sliders ── */
  function bindMoat() {
    ['tech', 'brand', 'channel', 'cost', 'network', 'license'].forEach(function (k) {
      var el = $('cd-moat-' + k);
      var vEl = $('cd-moat-' + k + '-v');
      if (!el) return;
      el.value = state.moat[k];
      if (vEl) vEl.textContent = state.moat[k];
      el.addEventListener('input', function () {
        state.moat[k] = Number(el.value);
        if (vEl) vEl.textContent = el.value;
        updateMoat();
        save(true);
      });
    });
    updateMoat();
  }
  function updateMoat() {
    var m = state.moat;
    var avg = (m.tech + m.brand + m.channel + m.cost + m.network + m.license) / 6;
    if ($('cd-moat-total')) $('cd-moat-total').textContent = fmt(avg) + ' / 10';
    renderFunnel();
  }

  function updatePeMarker() {
    var marker = $('cd-pe-marker');
    if (!marker) return;
    var v = Math.max(0, Math.min(100, num(state.val.pePct) != null ? num(state.val.pePct) : 50));
    marker.style.left = v + '%';
  }

  /* ── init ── */
  function init() {
    if (!$('tab-company-deep')) return;

    // company
    bindAll({
      'cd-name': 'company.name', 'cd-code': 'company.code', 'cd-industry': 'company.industry',
      'cd-market': 'company.market', 'cd-date': 'company.date', 'cd-analyst': 'company.analyst',
      'cd-oneliner': 'company.oneliner',
      'cd-ipo-notes': 'sources.ipo', 'cd-mda-notes': 'sources.mda',
      'cd-audit-notes': 'sources.audit', 'cd-research-notes': 'sources.research',
      'cd-web-notes': 'sources.web',
      'cd-ind-size': 'business.size', 'cd-ind-growth': 'business.growth', 'cd-mkt-share': 'business.share',
      'cd-cr3': 'business.cr3', 'cd-cr5': 'business.cr5', 'cd-ind-score': 'business.score',
      'cd-biz-what': 'business.what', 'cd-biz-ceiling': 'business.ceiling',
      'cd-biz-kfs': 'business.kfs', 'cd-biz-position': 'business.position',
      'cd-loop-customer': 'model.customer', 'cd-loop-value': 'model.value', 'cd-loop-channel': 'model.channel',
      'cd-loop-revenue': 'model.revenue', 'cd-loop-cost': 'model.cost', 'cd-loop-cash': 'model.cash',
      'cd-loop-weakest': 'model.weakest', 'cd-rev-mix': 'model.revMix',
      'cd-cust1': 'model.cust1', 'cd-cust5': 'model.cust5', 'cd-pricing': 'model.pricing',
      'cd-profit-formula': 'model.formula', 'cd-gm': 'model.gm', 'cd-nm': 'model.nm',
      'cd-biz-type': 'model.bizType', 'cd-model-score': 'model.modelScore',
      'cd-share-trend': 'moat.shareTrend', 'cd-margin-peers': 'moat.marginPeers',
      'cd-why-choose': 'moat.whyChoose', 'cd-why-blocked': 'moat.whyBlocked', 'cd-moat-dur': 'moat.dur',
      'cd-fin-rev': 'finance.rev', 'cd-fin-np': 'finance.np', 'cd-fin-gp': 'finance.gp',
      'cd-fin-ocf': 'finance.ocf', 'cd-fin-fcf': 'finance.fcf',
      'cd-fin-assets': 'finance.assets', 'cd-fin-equity': 'finance.equity',
      'cd-fin-cash': 'finance.cash', 'cd-fin-idebt': 'finance.idebt',
      'cd-fin-ar-g': 'finance.arG', 'cd-fin-inv-g': 'finance.invG', 'cd-fin-rev-g': 'finance.revG',
      'cd-fin-roe': 'finance.roe', 'cd-fin-gw': 'finance.gw', 'cd-fin-payout': 'finance.payout',
      'cd-fin-np-g': 'finance.npG', 'cd-fin-gm-d': 'finance.gmD',
      'cd-gov-controller': 'gov.controller', 'cd-gov-hold': 'gov.hold', 'cd-gov-pledge': 'gov.pledge',
      'cd-gov-structure': 'gov.structure', 'cd-gov-stable': 'gov.stable', 'cd-gov-related': 'gov.related',
      'cd-gov-dividend': 'gov.dividend', 'cd-gov-promise': 'gov.promise',
      'cd-gov-incentive': 'gov.incentive', 'cd-gov-score': 'gov.score',
      'cd-val-pe': 'val.pe', 'cd-val-pe-pct': 'val.pePct', 'cd-val-pb': 'val.pb',
      'cd-val-pb-pct': 'val.pbPct', 'cd-val-ps': 'val.ps', 'cd-val-peer-pe': 'val.peerPe',
      'cd-dcf-fcf': 'val.dcfFcf', 'cd-dcf-g': 'val.dcfG', 'cd-dcf-wacc': 'val.dcfWacc',
      'cd-dcf-shares': 'val.dcfShares', 'cd-dcf-netcash': 'val.dcfNetcash',
      'cd-val-verdict': 'val.verdict', 'cd-val-reason': 'val.reason',
      'cd-red-verdict': 'red.verdict', 'cd-red-worst': 'red.worst',
      'cd-pb-strength': 'pb.strength', 'cd-pb-weak': 'pb.weak', 'cd-pb-next': 'pb.next',
      'cd-pb-rival': 'pb.rival', 'cd-pb-move': 'pb.move', 'cd-pb-impact': 'pb.impact',
      'cd-pb-conclusion': 'pb.conclusion', 'cd-pb-invest': 'pb.invest',
      'cd-a1': 'conclusions.a1', 'cd-a2': 'conclusions.a2', 'cd-a3': 'conclusions.a3',
      'cd-a4': 'conclusions.a4', 'cd-a5': 'conclusions.a5'
    });

    bindChipGroup('cd-chip-lifecycle', 'business.lifecycle');
    bindChipGroup('cd-chip-policy', 'business.policy');
    bindChipGroup('cd-chip-payment', 'model.payment');

    renderChecks('cd-src-primary', PRIMARY, state.sources.primary, renderFunnel);
    renderChecks('cd-src-secondary', SECONDARY, state.sources.secondary, renderFunnel);
    renderChecks('cd-red-fin', FIN_RED, state.red.fin);
    renderChecks('cd-red-biz', BIZ_RED, state.red.biz);

    bindMoat();
    renderRisks();
    renderPb();
    calcFinance();
    buildVerdict();
    updatePeMarker();

    var pePct = $('cd-val-pe-pct');
    if (pePct) pePct.addEventListener('input', updatePeMarker);

    if (!state.company.date) {
      state.company.date = new Date().toISOString().slice(0, 10);
      set('cd-date', state.company.date);
    }
    renderFunnel();
    // 恢复 PPT 模板偏好
    try {
      var savedTpl = localStorage.getItem('cd-ppt-template');
      var tsel = $('cd-ppt-template');
      if (savedTpl && tsel) tsel.value = savedTpl;
    } catch (e) {}
    // 若已有分析数据，也展示结果总览
    try {
      if (state && state.company && (state.company.name || state.company.code)) {
        renderAnalysisResults();
      }
    } catch (e) {}
    console.log('[company-deep] ready');
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }

  // 暴露给 tabInitMap（若 core 后续注册）
  window.cdInit = init;

  // 同步到启动桥接，覆盖 pending 占位
  if (window.__cdBridge) {
    [
      'cdAutoPipeline', 'cdQuickStart', 'cdExportReport', 'cdFetchCompany', 'cdSave', 'cdExport',
      'cdFillDemo', 'cdBuildVerdict', 'cdAddRisk', 'cdAddPbStep', 'cdCalcDcf',
      'cdCalcFinance', 'cdSaveConclusions', 'cdInit'
    ].forEach(function (name) {
      if (typeof window[name] === 'function') window.__cdBridge[name] = window[name];
    });
    window.__cdReady = true;
    console.log('[company-deep] bridge bound', Object.keys(window.__cdBridge).length);
  }
})();

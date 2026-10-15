/**
 * tutorial.js — 「使用教程」页交互：目录滚动高亮 + 关键词过滤 + 展开/收起。
 *
 * 设计取舍：
 *  - 教程正文与目录都是**静态写在模板里**的（不靠 JS 生成）→ 即使脚本没加载，内容仍可读；
 *    JS 只做三件"锦上添花"的事，任何一件失败都不影响阅读。
 *  - 滚动容器不写死为 window：本应用的页面滚动可能落在某个 overflow 祖先上，
 *    这里运行时向上探测真实滚动容器（探不到就退回 window）。
 *  - 搜索匹配 data-kw（人工维护的关键词）+ 正文文本 + 所属分组名，
 *    命中后自动展开该模块，并同步收掉目录里被隐藏的条目，避免"看得见点不到"。
 */
(function () {
  'use strict';

  var ROOT_ID = 'tab-help';
  var HIT_LINE = 120;          // 距顶部多少像素内算"当前章节"
  var bound = false;
  var _openSnapshot = null;    // 进入搜索前的展开项（用于清空搜索时还原展开状态）

  function root() { return document.getElementById(ROOT_ID); }

  function qsa(sel) {
    var r = root();
    return r ? Array.prototype.slice.call(r.querySelectorAll(sel)) : [];
  }

  /** 探测真实滚动容器：返回 Element 或 null（null 表示用 window/document） */
  function findScroller(el) {
    var p = el && el.parentElement;
    while (p && p !== document.body && p !== document.documentElement) {
      var cs = window.getComputedStyle(p);
      if (/(auto|scroll|overlay)/.test(cs.overflowY) && p.scrollHeight > p.clientHeight + 4) return p;
      p = p.parentElement;
    }
    return null;
  }

  function scrollerTop(sc) {
    return sc ? sc.getBoundingClientRect().top : 0;
  }

  function scrollOffset(sc) {
    if (sc) return sc.scrollTop;
    return window.pageYOffset || document.documentElement.scrollTop || 0;
  }

  /* ── 目录滚动高亮 ─────────────────────────────────────── */

  var _tick = false;

  function syncActive() {
    var r = root();
    if (!r || getComputedStyle(r).display === 'none') return;
    var sc = findScroller(r);
    var groups = qsa('.tgroup');
    if (!groups.length) return;

    var line = scrollerTop(sc) + HIT_LINE;
    var current = groups[0];
    for (var i = 0; i < groups.length; i++) {
      if (groups[i].getBoundingClientRect().top <= line) current = groups[i];
      else break;
    }
    // 滚到底部时强制高亮最后一组：否则末尾分组可能因为到不了命中线而永远不亮
    var atBottom = sc
      ? (sc.scrollTop + sc.clientHeight >= sc.scrollHeight - 8)
      : (window.pageYOffset + window.innerHeight >= document.documentElement.scrollHeight - 8);
    if (atBottom) current = groups[groups.length - 1];
    var blocks = qsa('.tocg');
    for (var j = 0; j < blocks.length; j++) {
      var on = blocks[j].getAttribute('data-g') === current.id;
      if (blocks[j].classList.contains('on') !== on) blocks[j].classList.toggle('on', on);
      if (on) {
        // 让当前分组在侧栏内可见（侧栏自身可能滚动）
        var side = blocks[j].parentElement;
        if (side && side.scrollHeight > side.clientHeight + 8) {
          var top = blocks[j].offsetTop;
          if (top < side.scrollTop || top > side.scrollTop + side.clientHeight - 40) {
            side.scrollTop = Math.max(0, top - side.clientHeight / 3);
          }
        }
      }
    }
    return current;
  }

  function onScroll() {
    if (_tick) return;
    _tick = true;
    window.requestAnimationFrame(function () { _tick = false; syncActive(); });
  }

  /* ── 关键词过滤 ───────────────────────────────────────── */

  function filter(raw) {
    var q = String(raw || '').trim().toLowerCase();
    var total = 0;
    var shown = 0;

    // 搜索会把命中项自动展开；记下搜索前的展开状态，清空搜索时还原，
    // 否则一次搜索会让几十个条目一直摊开在页面上。
    if (q && _openSnapshot === null) {
      _openSnapshot = qsa('.tm, .tqi').filter(function (m) { return m.open; });
    }

    qsa('.tgroup').forEach(function (g) {
      var gname = (g.getAttribute('data-gname') || '').toLowerCase();
      var vis = 0;
      // 模块（.tm）与常见问题（.tqi）都参与搜索；.tqi 没有可跳转的页签，故不计入模块数
      g.querySelectorAll('.tm, .tqi').forEach(function (m) {
        total++;
        var hay = ((m.getAttribute('data-kw') || '') + ' ' + gname + ' ' + (m.textContent || '')).toLowerCase();
        var hit = !q || hay.indexOf(q) >= 0;
        m.classList.toggle('is-hidden', !hit);
        if (hit) {
          vis++; shown++;
          if (q) m.open = true;
        }
      });
      g.classList.toggle('is-hidden', vis === 0);
    });

    // 目录同步：隐藏被过滤掉的条目；整组无命中则连组一起收掉
    qsa('.tocg').forEach(function (b) {
      var gid = b.getAttribute('data-g');
      var g = gid ? document.getElementById(gid) : null;
      b.classList.toggle('is-hidden', !!(g && g.classList.contains('is-hidden')));
      b.querySelectorAll('.tma').forEach(function (a) {
        var href = a.getAttribute('href') || '';
        var t = href.charAt(0) === '#' ? document.getElementById(href.slice(1)) : null;
        a.classList.toggle('is-hidden', !!(t && t.classList.contains('is-hidden')));
      });
    });

    // 清空搜索 → 把展开状态还原成搜索之前的样子
    if (!q && _openSnapshot !== null) {
      var keep = _openSnapshot;
      _openSnapshot = null;
      qsa('.tm, .tqi').forEach(function (m) { m.open = keep.indexOf(m) >= 0; });
    }

    var empty = document.getElementById('tut-empty');
    if (empty) empty.classList.toggle('is-hidden', !(q && shown === 0));
    var eq = document.getElementById('tut-empty-q');
    if (eq) eq.textContent = raw || '';

    var cnt = document.getElementById('tut-count');
    if (cnt) cnt.textContent = q ? (shown + '/' + total) : '';

    // 过滤状态下目录展示所有命中分组，而非只显示"当前滚动位置"
    if (q) qsa('.tocg').forEach(function (b) { b.classList.add('on'); });
    else syncActive();
  }

  function resetFilter() {
    var inp = document.getElementById('tut-search');
    if (inp && inp.value) inp.value = '';
    filter('');
  }

  /* ── 展开 / 收起 ──────────────────────────────────────── */

  function setAll(open) {
    qsa('.tm').forEach(function (m) {
      if (!m.classList.contains('is-hidden')) m.open = open;
    });
  }

  /* ── 初始化 ───────────────────────────────────────────── */

  function init() {
    var r = root();
    if (!r) { console.warn('[Tutorial] 未找到 #' + ROOT_ID); return; }

    if (!bound) {
      bound = true;

      var sc = findScroller(r);
      (sc || window).addEventListener('scroll', onScroll, { passive: true });
      window.addEventListener('resize', onScroll, { passive: true });

      var inp = document.getElementById('tut-search');
      if (inp) {
        inp.addEventListener('input', function () { filter(this.value); });
        inp.addEventListener('keydown', function (e) {
          if (e.key === 'Escape') { e.stopPropagation(); resetFilter(); }
        });
      }
      var ex = document.getElementById('tut-expand');
      if (ex) ex.addEventListener('click', function () { setAll(true); });
      var co = document.getElementById('tut-collapse');
      if (co) co.addEventListener('click', function () { setAll(false); });

      // 目录点击：平滑滚动 + 立即高亮（不必等 scroll 事件）
      qsa('.tg, .tma').forEach(function (a) {
        a.addEventListener('click', function (e) {
          var href = this.getAttribute('href') || '';
          if (href.charAt(0) !== '#') return;
          var target = document.getElementById(href.slice(1));
          if (!target) return;
          e.preventDefault();
          target.scrollIntoView({ behavior: 'smooth', block: 'start' });
          var gid = target.classList.contains('tgroup')
            ? target.id
            : (target.closest('.tgroup') || {}).id;
          qsa('.tocg').forEach(function (b) { b.classList.toggle('on', b.getAttribute('data-g') === gid); });
        });
      });
    }

    // 每次进入页签都重算一次（布局在隐藏状态下量不准）
    syncActive();
    filter((document.getElementById('tut-search') || {}).value || '');
  }

  window.TutorialUI = { init: init, filter: filter, reset: resetFilter, syncActive: syncActive };
})();

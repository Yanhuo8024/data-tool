/* walkthrough.js — 交互式演练 Demo（步骤引导 + 自动切 tab + 控件高亮）
 * 用于「使用教程」页：对复杂功能（DID 计量 / 基金分析 / 固定收益）做可视化逐步引导。
 */
(function () {
  'use strict';

  var SCENES = {
    did: {
      title: 'DID 双重差分计量',
      desc: '政策评估：处理组/对照组 + 试点前后 → 因果效应',
      tab: 'tab-did',
      steps: [
        { hint: '① 从顶栏选择面板数据文件（需包含：个体标识列、时间列、处理组标记列）' },
        { hint: '② 在「基准回归」设置被解释变量（如：家庭人均消费支出）', focus: 'did-dep-var' },
        { hint: '③ 设置处理组列（如：是否试点省份 0/1 标记）', focus: 'did-treat-col' },
        { hint: '④ 设置时间列与个体列（如年份 / 省份编码）', focus: 'did-time-col' },
        { hint: '⑤ 运行 DID 回归，重点查看交互项系数（政策效应）与显著性' },
        { hint: '⑥ 用「平行趋势 / 安慰剂 / 分样本」验证稳健性' },
      ],
    },
    fund: {
      title: '基金分析',
      desc: '公募净值 → 年化收益 / 回撤 / 夏普 / Calmar / Sortino',
      tab: 'tab-fund',
      steps: [
        { hint: '① 输入基金代码（如 005827 易方达蓝筹）选择数据源' },
        { hint: '② 拉取历史净值后，系统自动计算年化收益、波动率、最大回撤' },
        { hint: '③ 查看风险收益指标：夏普比率、Calmar、Sortino' },
        { hint: '④ 可与基准对比回撤与走势' },
      ],
    },
    fixedincome: {
      title: '固定收益分析',
      desc: '债券估值 / 久期凸性 / 收益率曲线 / 信用利差 / 压力测试',
      tab: 'tab-fixedincome',
      steps: [
        { hint: '① 选择债券或组合数据（票息、期限、收益率等字段）' },
        { hint: '② 计算债券估值与久期 / 凸性（利率敏感性）' },
        { hint: '③ 绘制收益率曲线、计算信用利差 / OAS' },
        { hint: '④ 运行压力测试：利率冲击情景下的组合损益' },
      ],
    },
  };

  var bar = null;
  var cur = null;   // {scene, idx}

  function buildBar() {
    bar = document.createElement('div');
    bar.id = 'wt-bar';
    bar.style.cssText = 'position:fixed;top:70px;left:50%;transform:translateX(-50%);z-index:9999;' +
      'background:#fff;border:1px solid #e5e7f0;border-radius:14px;box-shadow:0 10px 34px rgba(0,0,0,.18);' +
      'padding:14px 18px;width:min(620px,92vw);font-family:inherit;';
    bar.innerHTML =
      '<div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:8px;">' +
      '<b id="wt-title" style="font-size:14px;color:#333;"></b>' +
      '<button onclick="closeWalkthrough()" style="border:none;background:none;font-size:16px;cursor:pointer;color:#999;">✕</button></div>' +
      '<div style="display:flex;align-items:center;gap:6px;margin-bottom:10px;">' +
      '<div id="wt-progress" style="flex:1;height:6px;background:#eef0f8;border-radius:4px;overflow:hidden;">' +
      '<div id="wt-progress-fill" style="height:100%;width:0%;background:#FF6B6B;border-radius:4px;transition:width .3s;"></div></div>' +
      '<span id="wt-stepno" style="font-size:11px;color:#999;white-space:nowrap;"></span></div>' +
      '<div id="wt-hint" style="font-size:13px;color:#555;line-height:1.7;min-height:42px;"></div>' +
      '<div style="display:flex;justify-content:space-between;align-items:center;margin-top:10px;">' +
      '<span id="wt-desc" style="font-size:11px;color:#aaa;"></span>' +
      '<div style="display:flex;gap:8px;">' +
      '<button onclick="prevWalkthrough()" class="btn btn-s btn-sm">上一步</button>' +
      '<button onclick="nextWalkthrough()" class="btn btn-p btn-sm" id="wt-next">下一步</button>' +
      '</div></div>';
    document.body.appendChild(bar);
  }

  function clearFocus() {
    var el = document.querySelector('.wt-focus');
    if (el) { el.classList.remove('wt-focus'); el.style.outline = ''; }
  }

  function render() {
    if (!cur) return;
    var scene = SCENES[cur.scene];
    var step = scene.steps[cur.idx];
    document.getElementById('wt-title').textContent = scene.title;
    document.getElementById('wt-desc').textContent = scene.desc;
    document.getElementById('wt-hint').textContent = step.hint;
    document.getElementById('wt-stepno').textContent = (cur.idx + 1) + ' / ' + scene.steps.length;
    document.getElementById('wt-progress-fill').style.width =
      ((cur.idx + 1) / scene.steps.length * 100) + '%';
    document.getElementById('wt-next').textContent =
      cur.idx === scene.steps.length - 1 ? '完成 ✓' : '下一步';
    clearFocus();
    if (step.focus) {
      var f = document.getElementById(step.focus);
      if (f) {
        f.classList.add('wt-focus');
        f.style.outline = '2px solid #FF6B6B';
        f.style.outlineOffset = '3px';
        f.scrollIntoView({ behavior: 'smooth', block: 'center' });
      }
    }
  }

  window.startWalkthrough = function (key) {
    var scene = SCENES[key];
    if (!scene) return;
    if (!bar) buildBar();
    cur = { scene: key, idx: 0 };
    if (typeof switchToTab === 'function') switchToTab(scene.tab);
    render();
  };
  window.nextWalkthrough = function () {
    if (!cur) return;
    var scene = SCENES[cur.scene];
    if (cur.idx < scene.steps.length - 1) {
      cur.idx += 1;
      render();
    } else {
      closeWalkthrough();
    }
  };
  window.prevWalkthrough = function () {
    if (!cur || cur.idx <= 0) return;
    cur.idx -= 1;
    render();
  };
  window.closeWalkthrough = function () {
    clearFocus();
    cur = null;
    if (bar) { bar.remove(); bar = null; }
  };
})();

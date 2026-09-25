/**
 * finance-sync.js - 金融数据全局同步模块
 * 获取金融数据后，自动同步到所有标签页（金融建模、可视化等）
 */

const FinanceSync = {
  // 当前金融数据状态
  state: {
    file: null,          // 文件名
    symbol: '',          // 股票代码
    source: '',          // 数据源 (akshare/tushare/baostock)
    rows: 0,
    cols: 0,
    fetchedAt: null,
    rawData: null,       // 原始数据（内存中保留前100行用于预览）
  },

  /**
   * 更新金融数据（在数据获取成功后调用）
   */
  update(data) {
    this.state = {
      file: data.file || null,
      symbol: data.symbol || '',
      source: data.source || '',
      rows: data.rows || 0,
      cols: data.cols || 0,
      fetchedAt: Date.now(),
      rawData: data.preview || null,
    };

    // 同步到全局选中文件
    if (this.state.file) {
      window._selectedFile = this.state.file;
      window._lastFiFile = this.state.file;
    }

    // 触发自定义事件，通知所有监听者
    window.dispatchEvent(new CustomEvent('financeDataUpdated', {
      detail: { ...this.state }
    }));

    // 更新所有金融相关标签页的文件选择器
    this.syncFileSelectors();

    console.log('[FinanceSync] Data updated:', this.state.file, this.state.rows, 'rows');
  },

  /**
   * 同步文件名到所有标签页的 file select 元素
   */
  syncFileSelectors() {
    const file = this.state.file;
    if (!file) return;

    // 更新所有金融相关的文件选择器
    const selectors = [
      'fm-file',           // 金融建模
      'fv-file',           // 金融可视化
      'viz-file',          // 通用可视化
      'fm-predict-file',   // 预测文件
    ];

    selectors.forEach(id => {
      const el = document.getElementById(id);
      if (!el) return;

      // 如果 select 中不存在该文件选项，添加它
      let exists = false;
      for (let i = 0; i < el.options.length; i++) {
        if (el.options[i].value === file) {
          exists = true;
          break;
        }
      }

      if (!exists) {
        const opt = document.createElement('option');
        opt.value = file;
        opt.textContent = `📈 ${file} (${this.state.rows}行)`;
        el.appendChild(opt);
      }

      // 自动选中新文件
      el.value = file;
    });

    // 更新顶栏全局文件选择器
    const globalSel = document.getElementById('globalFileSelect');
    if (globalSel) {
      let exists = false;
      for (let i = 0; i < globalSel.options.length; i++) {
        if (globalSel.options[i].value === file) { exists = true; break; }
      }
      if (!exists) {
        const opt = document.createElement('option');
        opt.value = file;
        opt.textContent = `📈 ${file}`;
        globalSel.appendChild(opt);
      }
      globalSel.value = file;
    }
  },

  /**
   * 获取当前金融数据文件
   */
  getCurrentFile() {
    return this.state.file;
  },

  /**
   * 获取当前数据源
   */
  getSource() {
    return this.state.source;
  },

  /**
   * 清除数据
   */
  clear() {
    this.state = { file: null, symbol: '', source: '', rows: 0, cols: 0, fetchedAt: null, rawData: null };
    window.dispatchEvent(new CustomEvent('financeDataCleared'));
  },

  /**
   * 监听数据更新
   */
  onUpdate(callback) {
    window.addEventListener('financeDataUpdated', (e) => callback(e.detail));
  },

  /**
   * 监听数据清除
   */
  onClear(callback) {
    window.addEventListener('financeDataCleared', () => callback());
  },
};

// 全局暴露
window.FinanceSync = FinanceSync;

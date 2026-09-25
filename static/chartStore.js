
// Register ECharts dark theme
(function() {
  var colorPalette = ['#45B7D1', '#2ce6c6', '#ff9800', '#ec0000', '#00da3c', '#9c27b0', '#00bcd4'];
  var theme = {
    color: colorPalette,
    backgroundColor: 'transparent',
    textStyle: {},
    title: { textStyle: { color: '#e0e0e0' }, subtextStyle: { color: '#888' } },
    legend: { textStyle: { color: '#e0e0e0' } },
    categoryAxis: {
      axisLine: { lineStyle: { color: '#8392A5' } },
      axisTick: { lineStyle: { color: '#8392A5' } },
      axisLabel: { textStyle: { color: '#e0e0e0' } },
      splitLine: { lineStyle: { color: '#2a2a3a' } },
      splitArea: { areaStyle: { color: ['rgba(255,255,255,0.02)', 'rgba(255,255,255,0.05)'] } }
    },
    valueAxis: {
      axisLine: { lineStyle: { color: '#8392A5' } },
      axisTick: { lineStyle: { color: '#8392A5' } },
      axisLabel: { textStyle: { color: '#e0e0e0' } },
      splitLine: { lineStyle: { color: '#2a2a3a' } },
      splitArea: { areaStyle: { color: ['rgba(255,255,255,0.02)', 'rgba(255,255,255,0.05)'] } }
    }
  };
  echarts.registerTheme('dark', theme);
})();

/**
 * chartStore.js - 全局状态管理（Redux风格）
 * 
 * 职责：管理图表联动所需的状态（时间范围、股票数据等）
 * 通信方式：发布-订阅模式，状态变化时自动通知所有订阅者
 */
(function() {
  'use strict';

  // ==================== 内部状态 ====================
  let _state = {
    symbol: '',           // 当前股票代码
    startDate: null,      // 起始日期
    endDate: null,        // 结束日期
    rawData: [],          // 原始K线数据
    filteredData: [],     // 过滤后的数据
    linkedCharts: {}      // 已注册的联动图表
  };

  let _listeners = [];    // 状态变化监听器

  // ==================== Store API ====================
  const ChartStore = {
    /**
     * 获取当前状态（只读副本）
     */
    getState() {
      return JSON.parse(JSON.stringify(_state));
    },

    /**
     * 订阅状态变化
     * @param {Function} listener - 回调函数(newState, oldState)
     * @returns {Function} 取消订阅函数
     */
    subscribe(listener) {
      _listeners.push(listener);
      return () => {
        _listeners = _listeners.filter(l => l !== listener);
      };
    },

    /**
     * 更新状态并通知订阅者
     * @param {Object} newState - 要合并的新状态
     */
    dispatch(newState) {
      const oldState = JSON.parse(JSON.stringify(_state));
      _state = { ..._state, ...newState };
      
      // 通知所有订阅者
      _listeners.forEach(listener => {
        try {
          listener(_state, oldState);
        } catch (e) {
          console.error('[ChartStore] Listener error:', e);
        }
      });
    },

    /**
     * 重置状态
     */
    reset() {
      const oldState = JSON.parse(JSON.stringify(_state));
      _state = {
        symbol: '',
        startDate: null,
        endDate: null,
        rawData: [],
        filteredData: [],
        linkedCharts: {}
      };
      _listeners.forEach(l => l(_state, oldState));
    },

    /**
     * 更新时间范围（联动触发点）
     * @param {string} startDate - YYYY-MM-DD
     * @param {string} endDate - YYYY-MM-DD
     */
    updateDateRange(startDate, endDate) {
      // 更新过滤数据
      const filtered = _state.rawData.filter(d => {
        const date = d.date || d.日期;
        if (!date) return true;
        if (startDate && date < startDate) return false;
        if (endDate && date > endDate) return false;
        return true;
      });

      this.dispatch({
        startDate,
        endDate,
        filteredData: filtered
      });
    },

    /**
     * 设置原始数据
     */
    setRawData(data) {
      this.dispatch({
        rawData: data,
        filteredData: data,
        startDate: null,
        endDate: null
      });
    },

    /**
     * 注册联动图表
     * @param {string} chartId - 图表唯一标识
     * @param {Function} callback - 数据变化时的回调
     */
    registerChart(chartId, callback) {
      _state.linkedCharts[chartId] = callback;
      // 立即用当前数据触发一次
      if (_state.filteredData.length) {
        callback(_state.filteredData, _state);
      }
    },

    /**
     * 注销联动图表
     */
    unregisterChart(chartId) {
      delete _state.linkedCharts[chartId];
    }
  };

  // 暴露到全局
  window.ChartStore = ChartStore;
})();

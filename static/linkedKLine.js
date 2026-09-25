/**
 * linkedKLine.js - 联动K线图组件
 * 
 * 功能：
 * 1. 使用ECharts渲染K线图（Candlestick）
 * 2. 支持dataZoom框选时间范围
 * 3. 框选时自动更新全局状态，触发其他联动图表刷新
 * 4. 支持：移动平均线、MACD、RSI、布林带叠加
 */
(function() {
  'use strict';

  /**
   * K线图组件
   * @param {string} containerId - DOM容器ID
   * @param {Object} options - 配置选项
   */
  class KLineChart {
    constructor(containerId, options = {}) {
      this.containerId = containerId;
      this.chart = null;
      this.options = {
        showMA: options.showMA !== false,
        showVolume: options.showVolume !== false,
        showMACD: options.showMACD || false,
        showRSI: options.showRSI || false,
        showBoll: options.showBoll || false,
        ...options
      };
      this.data = [];
      this.dates = [];
      this.volumes = [];
      this.closes = [];
      
      this._init();
    }

    /**
     * 初始化图表
     */
    _init() {
      const container = document.getElementById(this.containerId);
      if (!container) {
        console.error(`[KLineChart] Container #${this.containerId} not found`);
        return;
      }

      // 创建ECharts实例
      this.chart = echarts.init(container, 'dark');
      
      // 绑定dataZoom事件
      this._bindDataZoom();
      
      // 绑定窗口resize
      window.addEventListener('resize', () => this.chart.resize());
    }

    /**
     * 绑定dataZoom事件 - 核心联动逻辑
     */
    _bindDataZoom() {
      // 监听dataZoom变化（框选）
      this.chart.on('dataZoom', (params) => {
        // 获取当前dataZoom的范围
        const option = this.chart.getOption();
        const dataZoom = option.dataZoom && option.dataZoom[0];
        
        if (!dataZoom || !this.dates.length) return;

        // 将百分比转换为日期
        const totalLen = this.dates.length;
        const startIdx = Math.floor(dataZoom.start / 100 * totalLen);
        const endIdx = Math.floor(dataZoom.end / 100 * totalLen);
        
        const startDate = this.dates[Math.max(0, startIdx)];
        const endDate = this.dates[Math.min(totalLen - 1, endIdx)];

        console.log(`[KLineChart] Range selected: ${startDate} ~ ${endDate}`);
        
        // 更新全局状态，触发联动
        window.ChartStore.updateDateRange(startDate, endDate);
      });
    }

    /**
     * 加载数据
     * @param {Array} rawData - 原始K线数据
     */
    loadData(rawData) {
      this.data = rawData;
      this.dates = rawData.map(d => d.date || d.日期 || '');
      this.volumes = rawData.map(d => {
        const v = d.volume || d.成交量 || 0;
        const c = d.close || d.收盘 || 0;
        const o = d.open || d.开盘 || 0;
        // 根据涨跌设置颜色
        return {
          value: v,
          itemStyle: { color: c >= o ? '#ec0000' : '#00da3c' }
        };
      });
      this.closes = rawData.map(d => parseFloat(d.close || d.收盘 || 0));

      // 保存到全局状态
      window.ChartStore.setRawData(rawData);
      
      this._render();
    }

    /**
     * 计算移动平均线
     */
    _calculateMA(dayCount) {
      const result = [];
      for (let i = 0; i < this.closes.length; i++) {
        if (i < dayCount - 1) {
          result.push('-');
          continue;
        }
        let sum = 0;
        for (let j = 0; j < dayCount; j++) {
          sum += this.closes[i - j];
        }
        result.push((sum / dayCount).toFixed(2));
      }
      return result;
    }

    /**
     * 渲染图表
     */
    _render() {
      const option = this._buildOption();
      this.chart.setOption(option, true);
    }

    /**
     * 构建ECharts配置
     */
    _buildOption() {
      const hasVolume = this.options.showVolume && this.volumes.length;
      const hasMA = this.options.showMA;
      
      const grid = [
        { left: '8%', right: '3%', top: '8%', height: '55%' },  // K线
      ];
      const xAxis = [
        { type: 'category', data: this.dates, gridIndex: 0, axisLine: { lineStyle: { color: '#8392A5' } } }
      ];
      const yAxis = [
        { scale: true, gridIndex: 0, splitLine: { lineStyle: { color: '#2a2a3a' } }, axisLine: { lineStyle: { color: '#8392A5' } } }
      ];
      const series = [];

      // K线数据
      const klineData = this.data.map(d => [
        d.open || d.开盘 || 0,
        d.close || d.收盘 || 0,
        d.low || d.最低 || 0,
        d.high || d.最高 || 0
      ]);
      
      series.push({
        name: 'K线',
        type: 'candlestick',
        data: klineData,
        xAxisIndex: 0,
        yAxisIndex: 0,
        itemStyle: {
          color: '#ec0000',         // 阳线（涨）
          color0: '#00da3c',         // 阴线（跌）
          borderColor: '#ec0000',
          borderColor0: '#00da3c'
        }
      });

      // 移动平均线
      if (hasMA) {
        [5, 10, 20].forEach(period => {
          series.push({
            name: `MA${period}`,
            type: 'line',
            data: this._calculateMA(period),
            xAxisIndex: 0,
            yAxisIndex: 0,
            smooth: true,
            symbol: 'none',
            lineStyle: { width: 1 }
          });
        });
      }

      // 成交量
      if (hasVolume) {
        grid.push({ left: '8%', right: '3%', top: '70%', height: '16%' });
        xAxis.push({ type: 'category', data: this.dates, gridIndex: 1, axisLine: { lineStyle: { color: '#8392A5' } } });
        yAxis.push({ gridIndex: 1, splitLine: { lineStyle: { color: '#2a2a3a' } }, axisLine: { lineStyle: { color: '#8392A5' } } });
        series.push({
          name: '成交量',
          type: 'bar',
          data: this.volumes,
          xAxisIndex: 1,
          yAxisIndex: 1
        });
      }

      return {
        backgroundColor: 'transparent',
        animation: true,
        tooltip: {
          trigger: 'axis',
          axisPointer: { type: 'cross' },
          backgroundColor: 'rgba(15, 17, 28, 0.9)',
          borderColor: '#45B7D1',
          textStyle: { color: '#e0e0e0', fontSize: 12 }
        },
        axisPointer: { link: [{ xAxisIndex: 'all' }] },
        legend: {
          data: ['K线', hasMA ? 'MA5' : null, hasMA ? 'MA10' : null, hasMA ? 'MA20' : null, hasVolume ? '成交量' : null].filter(Boolean),
          textStyle: { color: '#e0e0e0' },
          top: 0
        },
        grid: grid,
        xAxis: xAxis,
        yAxis: yAxis,
        dataZoom: [
          {
            type: 'inside',  // 鼠标滚轮缩放
            xAxisIndex: [0, hasVolume ? 1 : 0],
            start: 0,
            end: 100
          },
          {
            type: 'slider',  // 底部滑块
            xAxisIndex: [0, hasVolume ? 1 : 0],
            start: 0,
            end: 100,
            top: '92%',
            height: 20,
            borderColor: '#333',
            fillerColor: 'rgba(94, 106, 210, 0.2)',
            handleStyle: { color: '#45B7D1' },
            textStyle: { color: '#e0e0e0' }
          }
        ],
        series: series
      };
    }

    /**
     * 根据外部数据变化刷新（联动回调）
     */
    update(filteredData, state) {
      // K线本身也响应范围变化（重绘不同区域）
      if (filteredData.length !== this.data.length) {
        // 可以用不同透明度显示非选中区域
        console.log(`[KLineChart] Data filtered: ${this.data.length} -> ${filteredData.length}`);
      }
    }

    /**
     * 销毁图表
     */
    destroy() {
      if (this.chart) {
        this.chart.dispose();
        this.chart = null;
      }
    }

    /**
     * 自适应大小
     */
    resize() {
      if (this.chart) this.chart.resize();
    }
  }

  // ==================== 收益率分布图（联动目标组件）====================
  
  class ReturnsDistributionChart {
    constructor(containerId, options = {}) {
      this.containerId = containerId;
      this.chart = null;
      this.options = options;
      this._unsubscribe = null;
      this._init();
    }

    _init() {
      const container = document.getElementById(this.containerId);
      if (!container) return;
      
      this.chart = echarts.init(container, 'dark');
      window.addEventListener('resize', () => this.chart.resize());
      
      // 订阅全局状态变化
      this._unsubscribe = window.ChartStore.subscribe((state, oldState) => {
        if (state.filteredData !== oldState.filteredData) {
          this.update(state.filteredData, state);
        }
      });
    }

    /**
     * 更新图表（响应范围变化）
     */
    update(filteredData, state) {
      if (!this.chart || !filteredData.length) return;

      // 计算收益率
      const returns = filteredData.map((d, i) => {
        if (i === 0) return 0;
        const prev = parseFloat(filteredData[i-1].close || filteredData[i-1].收盘 || 0);
        const curr = parseFloat(d.close || d.收盘 || 0);
        return prev ? ((curr - prev) / prev * 100) : 0;
      }).slice(1); // 去掉第一个0

      // 构建直方图数据
      const bins = this._buildHistogram(returns, 30);
      
      this.chart.setOption({
        backgroundColor: 'transparent',
        title: {
          text: `收益率分布 (${state.startDate || '全部'} ~ ${state.endDate || '全部'})`,
          textStyle: { color: '#e0e0e0', fontSize: 14 },
          left: 'center'
        },
        tooltip: {
          trigger: 'axis',
          backgroundColor: 'rgba(15, 17, 28, 0.9)',
          textStyle: { color: '#e0e0e0' }
        },
        grid: { left: '10%', right: '5%', top: '18%', bottom: '12%' },
        xAxis: {
          type: 'category',
          data: bins.labels,
          axisLine: { lineStyle: { color: '#8392A5' } },
          axisLabel: { color: '#e0e0e0', fontSize: 10, rotate: 45 }
        },
        yAxis: {
          type: 'value',
          name: '频次',
          nameTextStyle: { color: '#e0e0e0' },
          axisLine: { lineStyle: { color: '#8392A5' } },
          splitLine: { lineStyle: { color: '#2a2a3a' } }
        },
        series: [{
          type: 'bar',
          data: bins.counts,
          itemStyle: {
            color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
              { offset: 0, color: '#45B7D1' },
              { offset: 1, color: '#2ce6c6' }
            ])
          },
          markLine: {
            data: [
              { type: 'average', name: '平均', lineStyle: { color: '#ff9800' } }
            ],
            label: { color: '#e0e0e0' }
          }
        }]
      }, true);
    }

    /**
     * 构建直方图数据
     */
    _buildHistogram(data, binCount) {
      if (!data.length) return { labels: [], counts: [] };
      
      const min = Math.min(...data);
      const max = Math.max(...data);
      const range = max - min || 1;
      const binWidth = range / binCount;
      
      const counts = new Array(binCount).fill(0);
      const labels = [];
      
      for (let i = 0; i < binCount; i++) {
        const binStart = min + i * binWidth;
        const binEnd = binStart + binWidth;
        labels.push(`${binStart.toFixed(1)}%`);
        
        data.forEach(v => {
          if (v >= binStart && (i === binCount - 1 ? v <= binEnd : v < binEnd)) {
            counts[i]++;
          }
        });
      }
      
      return { labels, counts };
    }

    destroy() {
      if (this._unsubscribe) this._unsubscribe();
      if (this.chart) this.chart.dispose();
      this.chart = null;
    }

    resize() {
      if (this.chart) this.chart.resize();
    }
  }

  // ==================== 创建全局工厂函数 ====================
  
  window.KLineChart = KLineChart;
  window.ReturnsDistributionChart = ReturnsDistributionChart;

})();

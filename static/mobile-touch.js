/**
 * mobile-touch.js - iOS 移动端触摸优化
 */

(function() {
  'use strict';

  // ==================== 设备检测 ====================
  const isTouchDevice = 'ontouchstart' in window || navigator.maxTouchPoints > 0;
  const isIOS = /iPad|iPhone|iPod/.test(navigator.userAgent);
  
  if (!isTouchDevice) return;

  // ==================== 防止双击缩放 ====================
  let lastTouchEnd = 0;
  document.addEventListener('touchend', function(e) {
    const now = Date.now();
    if (now - lastTouchEnd <= 300) {
      e.preventDefault();
    }
    lastTouchEnd = now;
  }, { passive: false });

  // ==================== 触摸反馈 ====================
  document.addEventListener('touchstart', function(e) {
    const target = e.target.closest('.nbtn, .btn, .card, .flow-step, .db-card, .file-item');
    if (target) {
      target.classList.add('touch-active');
    }
  }, { passive: true });

  document.addEventListener('touchend', function(e) {
    const target = e.target.closest('.nbtn, .btn, .card, .flow-step, .db-card, .file-item');
    if (target) {
      setTimeout(() => target.classList.remove('touch-active'), 150);
    }
  }, { passive: true });

  // ==================== 文件上传触摸支持 ====================
  const uz = document.getElementById('uz');
  if (uz) {
    uz.addEventListener('click', function(e) {
      // 移动端点击上传区域触发文件选择
      if (isTouchDevice && !e._filePickerOpened) {
        e._filePickerOpened = true;
        const input = document.createElement('input');
        input.type = 'file';
        input.multiple = true;
        input.accept = '.csv,.xlsx,.xls,.json,.parquet';
        input.onchange = function() {
          if (window.fsChange) {
            window.fsChange(input.files);
          }
        };
        input.click();
        setTimeout(() => { e._filePickerOpened = false; }, 1000);
      }
    });
  }

  // ==================== 图表 Pinch-Zoom ====================
  // 为 ECharts 图表添加双指缩放支持
  const chartContainers = document.querySelectorAll('[id*="chart"], [id*="kline"], .cc');
  chartContainers.forEach(function(container) {
    let initialDistance = 0;
    let initialScale = 1;
    
    container.addEventListener('touchstart', function(e) {
      if (e.touches.length === 2) {
        e.preventDefault();
        initialDistance = getDistance(e.touches[0], e.touches[1]);
        initialScale = container._chartScale || 1;
      }
    }, { passive: false });

    container.addEventListener('touchmove', function(e) {
      if (e.touches.length === 2) {
        e.preventDefault();
        const currentDistance = getDistance(e.touches[0], e.touches[1]);
        const scale = currentDistance / initialDistance;
        container._chartScale = Math.max(0.5, Math.min(3, initialScale * scale));
        
        // 触发 ECharts 的 dataZoom
        if (window.echarts) {
          const chart = echarts.getInstanceByDom(container);
          if (chart) {
            const option = chart.getOption();
            const dataZoom = option.dataZoom && option.dataZoom[0];
            if (dataZoom) {
              const range = 100 / container._chartScale;
              const center = (dataZoom.start + dataZoom.end) / 2;
              chart.dispatchAction({
                type: 'dataZoom',
                start: Math.max(0, center - range / 2),
                end: Math.min(100, center + range / 2)
              });
            }
          }
        }
      }
    }, { passive: false });

    container.addEventListener('touchend', function(e) {
      if (e.touches.length < 2) {
        // 延迟重置，防止误触
        setTimeout(() => {
          if (container._chartScale) {
            initialScale = container._chartScale;
          }
        }, 100);
      }
    }, { passive: true });
  });

  function getDistance(touch1, touch2) {
    const dx = touch1.clientX - touch2.clientX;
    const dy = touch1.clientY - touch2.clientY;
    return Math.sqrt(dx * dx + dy * dy);
  }

  // ==================== 滑动切换标签页 ====================
  let touchStartX = 0;
  let touchStartY = 0;
  let touchStartTime = 0;

  document.addEventListener('touchstart', function(e) {
    touchStartX = e.touches[0].clientX;
    touchStartY = e.touches[0].clientY;
    touchStartTime = Date.now();
  }, { passive: true });

  document.addEventListener('touchend', function(e) {
    const touchEndX = e.changedTouches[0].clientX;
    const touchEndY = e.changedTouches[0].clientY;
    const deltaX = touchEndX - touchStartX;
    const deltaY = touchEndY - touchStartY;
    const deltaTime = Date.now() - touchStartTime;

    // 快速滑动（300ms内，水平位移>100px，垂直位移<50px）
    if (deltaTime < 300 && Math.abs(deltaX) > 100 && Math.abs(deltaY) < 50) {
      const tabs = ['tab-guide', 'tab-nlq', 'tab-rag', 'tab-chart', 
                    'tab-security', 'tab-help', 'tab-up', 'tab-store', 'tab-scrape', 
                    'tab-finance', 'tab-screen', 'tab-table', 'tab-clean', 'tab-ml', 
                    'tab-pipeline', 'tab-nlp', 'tab-analyze', 'tab-fs', 
                    'tab-ts', 'tab-ht', 'tab-finance-model', 'tab-finance-viz', 'tab-report'];
      
      // 获取当前激活的标签
      const currentTab = document.querySelector('.nbtn.on');
      if (!currentTab) return;
      
      const currentTabId = currentTab.getAttribute('data-t');
      const currentIndex = tabs.indexOf(currentTabId);
      
      if (currentIndex === -1) return;
      
      let newIndex;
      if (deltaX > 0) {
        // 向右滑动 - 上一个标签
        newIndex = Math.max(0, currentIndex - 1);
      } else {
        // 向左滑动 - 下一个标签
        newIndex = Math.min(tabs.length - 1, currentIndex + 1);
      }
      
      if (newIndex !== currentIndex && window.switchToTab) {
        window.switchToTab(tabs[newIndex]);
      }
    }
  }, { passive: true });

  // ==================== iOS 特定优化 ====================
  if (isIOS) {
    // 修复 iOS 键盘弹出后页面不回弹的问题
    document.addEventListener('focusout', function() {
      setTimeout(() => {
        window.scrollTo(0, 0);
      }, 100);
    });

    // 防止 iOS 橡皮筋滚动导致的布局问题
    document.addEventListener('touchmove', function(e) {
      if (e.target.closest('.ai-assistant-messages, .canvas-sidebar, .log')) {
        return; // 允许滚动区域滚动
      }
      if (document.body.scrollHeight <= window.innerHeight) {
        e.preventDefault();
      }
    }, { passive: false });
  }

  // ==================== 暴露 API ====================
  window.MobileTouch = {
    isTouchDevice: isTouchDevice,
    isIOS: isIOS
  };

})();

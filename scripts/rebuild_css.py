#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Rebuild the CSS file"""

css = '''/* data.tool v3.0 - Linear Tech Design System */
:root {
  --bg-deep: #08090a;
  --bg-surface: #0f1011;
  --bg-card: rgba(255,255,255,0.03);
  --bg-card-hover: rgba(255,255,255,0.06);
  --glass-border: rgba(255,255,255,0.06);
  --glass-border-strong: rgba(255,255,255,0.10);
  --ink: #f7f8f8;
  --ink-muted: #b8babd;
  --ink-subtle: #8a8f98;
  --ink-tertiary: #62666d;
  --primary: #FF6B6B;
  --primary-glow: rgba(94,106,210,0.4);
  --primary-hover: #828fff;
  --secondary: #7170ff;
  --accent: #2ce6c6;
  --success: #34d399;
  --success-glow: rgba(52,211,153,0.3);
  --warning: #fbbf24;
  --warning-glow: rgba(251,191,36,0.3);
  --error: #f87171;
  --error-glow: rgba(248,113,113,0.3);
  --radius-sm: 6px;
  --radius-md: 10px;
  --radius-lg: 14px;
  --radius-xl: 20px;
  --shadow-md: 0 4px 16px rgba(0,0,0,0.4);
  --shadow-lg: 0 8px 32px rgba(0,0,0,0.6);
  --shadow-glow: 0 0 20px var(--primary-glow);
  --transition-fast: 0.15s ease;
  --transition-base: 0.2s ease;
}

* { margin: 0; padding: 0; box-sizing: border-box; }

html { scroll-behavior: smooth; }

body {
  font-family: 'Inter', 'Microsoft YaHei', 'PingFang SC', 'Segoe UI', system-ui, -apple-system, sans-serif;
  background: var(--bg-deep);
  color: var(--ink);
  font-size: 14px;
  line-height: 1.5;
  -webkit-font-smoothing: antialiased;
  overflow-x: hidden;
  min-height: 100vh;
}

/* Background Aurora Glow */
#particle-canvas { display: none; }

.ambient-glow {
  position: fixed;
  border-radius: 50%;
  filter: blur(120px);
  pointer-events: none;
  z-index: 0;
  opacity: 0.5;
  animation: ambientFloat 20s ease-in-out infinite;
}
.ambient-glow-1 { width: 500px; height: 500px; background: radial-gradient(circle, rgba(94,106,210,0.4), transparent 70%); top: -100px; left: -100px; animation-delay: 0s; }
.ambient-glow-2 { width: 400px; height: 400px; background: radial-gradient(circle, rgba(44,230,198,0.3), transparent 70%); bottom: -50px; right: -50px; animation-delay: -7s; }
.ambient-glow-3 { width: 300px; height: 300px; background: radial-gradient(circle, rgba(113,112,255,0.3), transparent 70%); top: 50%; left: 50%; animation-delay: -14s; }

@keyframes ambientFloat {
  0%, 100% { transform: translate(0, 0) scale(1); }
  25% { transform: translate(30px, -30px) scale(1.1); }
  50% { transform: translate(-20px, 20px) scale(0.95); }
  75% { transform: translate(20px, 10px) scale(1.05); }
}

/* Header */
.header {
  height: 56px;
  display: flex; align-items: center;
  padding: 0 32px; gap: 24px;
  position: sticky; top: 0;
  background: rgba(8,9,10,0.85);
  backdrop-filter: blur(16px);
  -webkit-backdrop-filter: blur(16px);
  border-bottom: 1px solid var(--glass-border);
  z-index: 100;
}
.logo { font-size: 18px; font-weight: 700; letter-spacing: -0.02em; color: var(--ink); }
.logo span { color: var(--primary); margin-left: -2px; }
.tagline { font-size: 11px; color: var(--primary); font-weight: 510; letter-spacing: 0.08em; text-transform: uppercase; }
.sp { flex: 1; }
.status { font-size: 11px; color: var(--ink-subtle); display: flex; align-items: center; gap: 8px; padding: 4px 10px; background: rgba(255,255,255,0.03); border-radius: 50px; border: 1px solid var(--glass-border); }
.dot { width: 6px; height: 6px; border-radius: 50%; background: var(--success); box-shadow: 0 0 8px var(--success-glow); animation: pulse 2s ease-in-out infinite; }
@keyframes pulse { 0%, 100% { opacity: 1; transform: scale(1); } 50% { opacity: 0.6; transform: scale(0.8); } }

/* Nav */
.nav {
  display: flex; flex-wrap: wrap; align-items: center;
  gap: 0;
  padding: 6px 32px;
  position: fixed; top: 56px; left: 0; right: 0;
  background: var(--bg-deep);
  backdrop-filter: blur(16px);
  -webkit-backdrop-filter: blur(16px);
  border-bottom: 1px solid var(--glass-border);
  z-index: 99;
  box-shadow: 0 2px 12px rgba(0,0,0,0.08);
}
.nav-row { display: flex; flex-wrap: wrap; align-items: center; gap: 4px 4px; flex-basis: 100%; padding: 4px 0; }
.nav-cat { font-size: 10px; font-weight: 600; color: var(--primary); letter-spacing: 0.08em; text-transform: uppercase; padding: 3px 8px; background: rgba(94,106,210,0.08); border-radius: 4px; white-space: nowrap; margin-right: 6px; cursor: pointer; user-select: none; transition: all 0.2s; border: 1px solid transparent; }
.nav-cat:hover { background: rgba(94,106,210,0.18); border-color: rgba(94,106,210,0.25); }
.nav-cat::before { content: '▾'; margin-right: 4px; font-size: 9px; display: inline-block; transition: transform 0.2s; }
.nav-row.collapsed .nav-cat::before { transform: rotate(-90deg); }
.nav-row.collapsed .nbtn, .nav-row.collapsed .nav-cat { display: none; }
.nav-row.collapsed { padding: 0; height: 0; overflow: hidden; }
.nav-toggle { font-size: 16px; color: var(--ink-subtle); cursor: pointer; padding: 4px 10px; border-radius: 6px; line-height: 1; transition: all 0.2s; user-select: none; flex-shrink: 0; border: 1px solid var(--glass-border); background: var(--bg-card); }
.nav-toggle:hover { color: var(--primary); background: rgba(94,106,210,0.1); border-color: rgba(94,106,210,0.25); }
.nav-div { display: none; }
.nbtn { background: none; border: 1px solid transparent; color: var(--ink-subtle); padding: 6px 14px; border-radius: 6px; font-size: 13px; font-weight: 510; cursor: pointer; transition: all var(--transition-fast); white-space: nowrap; font-family: inherit; position: relative; }
.nbtn:hover { color: var(--ink-muted); background: var(--bg-card-hover); border-color: var(--glass-border); }
.nbtn.on { color: #fff; background: var(--primary); border: 1px solid var(--primary); font-weight: 600; }
.nbtn.on::after { content: ''; position: absolute; bottom: -2px; left: 15%; right: 15%; height: 2px; background: var(--accent); border-radius: 2px; }

/* Main */
.main { padding: 32px; padding-bottom: 36px; max-width: 1600px; margin: 0 auto; position: relative; z-index: 1; }

/* Section */
.sec { position: fixed; top: 96px; left: 0; right: 0; bottom: 28px; overflow-y: auto; padding: 20px; display: none; }
.sec.on { display: block !important; opacity: 1 !important; visibility: visible !important; animation: tabFadeIn 0.25s ease; }
.sec.on .card { opacity: 1 !important; animation: none !important; transform: none !important; }
@keyframes tabFadeIn { from { opacity: 0; transform: translateY(8px); } to { opacity: 1; transform: translateY(0); } }

/* Card */
.card { background: var(--bg-card); backdrop-filter: blur(20px) saturate(1.5); -webkit-backdrop-filter: blur(20px) saturate(1.5); border: 1px solid var(--glass-border); border-radius: var(--radius-lg); box-shadow: var(--shadow-md), inset 0 1px 0 rgba(255,255,255,0.05); padding: 28px; margin-bottom: 24px; transition: all var(--transition-base); opacity: 1 !important; visibility: visible !important; animation: cardEnter 0.5s ease backwards; }
.card:hover { background: var(--bg-card-hover); border-color: var(--glass-border-strong); box-shadow: var(--shadow-lg), var(--shadow-glow), inset 0 1px 0 rgba(255,255,255,0.08); transform: translateY(-2px); }
@keyframes cardEnter { from { opacity: 0; transform: translateY(20px); } to { opacity: 1; transform: translateY(0); } }
.c-head { display: flex; align-items: center; gap: 12px; margin-bottom: 16px; }
.c-title { font-size: 16px; font-weight: 600; }
.c-sub { font-size: 12px; color: var(--ink-muted); }

/* Eyebrow */
.eyebrow { font-size: 11px; font-weight: 600; color: var(--ink-muted); text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 12px; }

/* Form */
.fg { display: flex; flex-direction: column; gap: 4px; margin-bottom: 12px; }
.fg label { font-size: 11px; font-weight: 500; color: var(--ink-muted); }
.fg input, .fg select, .fg textarea { padding: 8px 12px; border-radius: var(--radius-sm); border: 1px solid var(--glass-border); background: var(--bg-surface); color: var(--ink); font-size: 13px; font-family: inherit; transition: all var(--transition-fast); }
.fg input:focus, .fg select:focus, .fg textarea:focus { outline: none; border-color: var(--primary); box-shadow: 0 0 0 3px var(--primary-glow); }
.grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
.grid-3 { grid-template-columns: 1fr 1fr 1fr; }
.grid-4 { grid-template-columns: 1fr 1fr 1fr 1fr; }

/* Buttons */
.bgrp { display: flex; gap: 8px; margin-top: 12px; flex-wrap: wrap; }
.btn { padding: 8px 16px; border-radius: var(--radius-sm); border: 1px solid transparent; cursor: pointer; font-size: 13px; font-weight: 500; font-family: inherit; transition: all var(--transition-fast); display: inline-flex; align-items: center; gap: 6px; white-space: nowrap; }
.btn:active { transform: scale(0.98); }
.btn-p { background: var(--primary); color: #fff; box-shadow: 0 2px 8px var(--primary-glow); }
.btn-p:hover { background: var(--primary-hover); box-shadow: 0 4px 16px var(--primary-glow); transform: translateY(-1px); }
.btn-s { background: var(--bg-surface); color: var(--ink); border-color: var(--glass-border); }
.btn-s:hover { background: var(--bg-card-hover); border-color: var(--glass-border-strong); }
.btn-g { background: rgba(52,211,153,0.1); color: var(--success); border-color: rgba(52,211,153,0.2); }
.btn-g:hover { background: rgba(52,211,153,0.2); border-color: rgba(52,211,153,0.4); }
.btn-w { background: rgba(251,191,36,0.1); color: var(--warning); border-color: rgba(251,191,36,0.2); }
.btn-w:hover { background: rgba(251,191,36,0.2); border-color: rgba(251,191,36,0.4); }
.btn-r { background: rgba(248,113,113,0.1); color: var(--error); border-color: rgba(248,113,113,0.2); }
.btn-r:hover { background: rgba(248,113,113,0.2); border-color: rgba(248,113,113,0.4); }
.btn-sm { padding: 5px 10px; font-size: 11px; }

/* Divider */
.divider { height: 1px; background: var(--glass-border); margin: 16px 0; }

/* Log */
.log { background: var(--bg-surface); border: 1px solid var(--glass-border); border-radius: var(--radius-sm); padding: 12px; font-family: 'SF Mono', 'Consolas', monospace; font-size: 11px; max-height: 200px; overflow-y: auto; color: var(--ink-subtle); }

/* Stats */
.stats-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 12px; }
.stat-card { background: var(--bg-surface); border: 1px solid var(--glass-border); border-radius: var(--radius-sm); padding: 16px; text-align: center; }
.stat-value { font-size: 24px; font-weight: 700; color: var(--primary); }
.stat-label { font-size: 11px; color: var(--ink-muted); margin-top: 4px; }

/* Table */
.table-container { overflow-x: auto; border: 1px solid var(--glass-border); border-radius: var(--radius-sm); }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th, td { padding: 10px 12px; text-align: left; border-bottom: 1px solid var(--glass-border); }
th { background: var(--bg-surface); font-weight: 600; color: var(--ink); font-size: 12px; text-transform: uppercase; letter-spacing: 0.05em; }
tr:hover { background: var(--bg-card-hover); }

/* Charts */
.chart-container { position: relative; height: 400px; margin: 16px 0; }
.chart-container-sm { height: 250px; }
.chart-container-lg { height: 500px; }

/* Upload */
.uz { border: 2px dashed var(--glass-border); border-radius: var(--radius-md); padding: 40px; text-align: center; cursor: pointer; transition: all var(--transition-base); }
.uz:hover { border-color: var(--primary); background: rgba(94,106,210,0.03); }
.uz-i { font-size: 32px; color: var(--ink-muted); margin-bottom: 8px; }
.uz-t { font-size: 13px; color: var(--ink-muted); margin-bottom: 4px; }
.uz-h { font-size: 11px; color: var(--ink-tertiary); }

/* File Selector */
.file-selector { background: var(--bg-card); border: 1px solid var(--glass-border); border-radius: var(--radius-md); padding: 16px; }
.file-selector-title { font-size: 13px; font-weight: 600; color: var(--ink); margin-bottom: 12px; }
.file-list { display: flex; flex-direction: column; gap: 8px; }
.file-item { display: flex; align-items: center; gap: 12px; padding: 12px 16px; background: var(--bg-surface); border: 1px solid var(--glass-border); border-radius: var(--radius-sm); cursor: pointer; transition: all 0.2s; }
.file-item:hover { background: var(--bg-card-hover); border-color: var(--glass-border-strong); }
.file-item.selected { border-color: var(--primary); background: rgba(94,106,210,0.08); }
.file-item-icon { font-size: 20px; flex-shrink: 0; }
.file-item-info { flex: 1; min-width: 0; }
.file-item-name { font-size: 13px; font-weight: 500; color: var(--ink); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.file-item-meta { font-size: 11px; color: var(--ink-subtle); margin-top: 2px; }
.file-item-check { width: 20px; height: 20px; border-radius: 50%; border: 2px solid var(--glass-border); display: flex; align-items: center; justify-content: center; flex-shrink: 0; transition: all 0.2s; }
.file-item.selected .file-item-check { background: var(--primary); border-color: var(--primary); }
.file-item.selected .file-item-check::after { content: '✓'; color: #fff; font-size: 12px; font-weight: 700; }

/* Badge */
.badge { display: inline-flex; align-items: center; gap: 4px; padding: 2px 8px; border-radius: 50px; font-size: 10px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.05em; }
.badge-primary { background: var(--primary-glow); color: var(--primary); }
.badge-success { background: var(--success-glow); color: var(--success); }
.badge-warning { background: var(--warning-glow); color: var(--warning); }
.badge-error { background: var(--error-glow); color: var(--error); }

/* AI Assistant Panel */
.ai-assistant-panel { position: fixed; right: -400px; top: 56px; bottom: 28px; width: 380px; background: var(--bg-surface); border-left: 1px solid var(--glass-border); z-index: 200; display: flex; flex-direction: column; transition: right 0.3s ease; box-shadow: -4px 0 24px rgba(0,0,0,0.3); }
.ai-assistant-panel.open { right: 0; }
.ai-assistant-header { padding: 12px 16px; border-bottom: 1px solid var(--glass-border); display: flex; align-items: center; gap: 8px; font-weight: 600; color: var(--ink); background: rgba(94,106,210,0.05); }
.ai-assistant-messages { flex: 1; overflow-y: auto; padding: 12px; display: flex; flex-direction: column; gap: 8px; }
.ai-assistant-input { padding: 12px; border-top: 1px solid var(--glass-border); display: flex; gap: 8px; }
.ai-assistant-input input { flex: 1; padding: 8px 12px; border-radius: var(--radius-sm); border: 1px solid var(--glass-border); background: var(--bg-deep); color: var(--ink); font-size: 13px; }
.ai-assistant-input input:focus { outline: none; border-color: var(--primary); }
.ai-assistant-input button { padding: 8px 16px; border-radius: var(--radius-sm); border: none; background: var(--primary); color: #fff; cursor: pointer; font-size: 16px; transition: all var(--transition-fast); }
.ai-assistant-input button:hover { background: var(--primary-hover); }

/* AI Context Panel */
.ai-context { background: rgba(94,106,210,0.08) !important; border-color: rgba(94,106,210,0.2) !important; }
.ai-context details summary { cursor: pointer; font-weight: 600; color: var(--primary); padding: 4px 0; }
.ai-context details pre { background: rgba(0,0,0,0.2); padding: 8px; border-radius: 4px; font-size: 11px; max-height: 200px; overflow-y: auto; }

/* AI Messages */
.ai-msg { padding: 10px 14px; border-radius: var(--radius-md); max-width: 85%; word-wrap: break-word; }
.ai-msg.user { align-self: flex-end; background: var(--primary); color: #fff; }
.ai-msg.assistant { align-self: flex-start; background: var(--bg-card); border: 1px solid var(--glass-border); }
.ai-msg.assistant pre { background: rgba(0,0,0,0.3); padding: 10px; border-radius: 6px; overflow-x: auto; margin: 6px 0; font-size: 11px; }
.ai-msg.assistant code { background: rgba(0,0,0,0.2); padding: 2px 6px; border-radius: 4px; font-size: 12px; }

/* Structured Report */
.ai-report { background: var(--bg-card) !important; border: 1px solid var(--glass-border) !important; }
.ai-report .report-section { margin: 8px 0; padding: 8px; border-radius: 6px; background: rgba(0,0,0,0.15); }
.ai-report .report-section h4 { margin: 0 0 6px 0; font-size: 13px; color: var(--primary); }
.ai-report .report-section p, .ai-report .report-section li { font-size: 12px; line-height: 1.6; }

/* AI Settings */
.ai-settings { padding: 12px 16px; border-bottom: 1px solid var(--glass-border); background: var(--bg-surface); max-height: 280px; overflow-y: auto; }
.ai-settings-title { font-size: 12px; font-weight: 600; color: var(--ink); margin-bottom: 10px; }
.ai-settings .fg { margin-bottom: 8px; }
.ai-settings label { font-size: 11px; }
.ai-settings input, .ai-settings select { padding: 6px 10px; font-size: 12px; }
.ai-settings .bgrp { margin-top: 8px; }
.ai-settings select option { font-size: 12px; }

/* Spinner */
.spin { display: inline-block; width: 16px; height: 16px; border: 2px solid rgba(255,255,255,0.2); border-top-color: var(--primary); border-radius: 50%; animation: spin 0.6s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }

/* Misc */
.mt { margin-top: 12px; }
.mb { margin-bottom: 12px; }
.hide { display: none !important; }
.flex { display: flex; align-items: center; gap: 8px; }
.flex-col { flex-direction: column; }
.flex-1 { flex: 1; }
.gap-4 { gap: 4px; }
.gap-8 { gap: 8px; }
.gap-12 { gap: 12px; }
.text-center { text-align: center; }
.text-right { text-align: right; }
.text-sm { font-size: 12px; }
.text-xs { font-size: 11px; }
.text-muted { color: var(--ink-muted); }
.text-subtle { color: var(--ink-subtle); }
.text-primary { color: var(--primary); }
.text-success { color: var(--success); }
.text-warning { color: var(--warning); }
.text-error { color: var(--error); }

/* Scrollbar */
::-webkit-scrollbar { width: 8px; height: 8px; }
::-webkit-scrollbar-track { background: var(--bg-deep); }
::-webkit-scrollbar-thumb { background: var(--glass-border); border-radius: 4px; }
::-webkit-scrollbar-thumb:hover { background: var(--glass-border-strong); }

/* Status Bar */
.status-bar { position: fixed; bottom: 0; left: 0; right: 0; height: 28px; background: var(--bg-surface); border-top: 1px solid var(--glass-border); display: flex; align-items: center; padding: 0 16px; font-size: 11px; color: var(--ink-subtle); z-index: 100; }
.status-bar-sep { margin: 0 10px; color: var(--glass-border); }
.status-bar-tip { color: var(--ink-tertiary); }
.status-bar-tip kbd { display: inline-flex; align-items: center; justify-content: center; min-width: 16px; height: 16px; padding: 0 4px; background: rgba(255,255,255,0.08); border: 1px solid rgba(255,255,255,0.12); border-radius: 3px; font-size: 10px; font-family: 'SF Mono', 'Consolas', monospace; color: var(--ink-muted); }

/* Global Loading Overlay */
#global-loading { position: fixed; top: 0; left: 0; right: 0; bottom: 0; background: rgba(0,0,0,0.5); backdrop-filter: blur(4px); -webkit-backdrop-filter: blur(4px); z-index: 9999; display: none; align-items: center; justify-content: center; flex-direction: column; gap: 16px; }
#global-loading.show { display: flex; }
#global-loading .spinner { width: 40px; height: 40px; border-width: 3px; }
#global-loading .loading-text { color: var(--ink); font-size: 14px; }

/* Toast */
.toast-container { position: fixed; top: 72px; right: 16px; z-index: 10000; display: flex; flex-direction: column; gap: 8px; }
.toast { padding: 12px 16px; border-radius: var(--radius-sm); background: var(--bg-card); border: 1px solid var(--glass-border); color: var(--ink); font-size: 13px; box-shadow: var(--shadow-md); animation: toastIn 0.3s ease; max-width: 300px; }
.toast.success { border-color: var(--success); }
.toast.error { border-color: var(--error); }
.toast.warning { border-color: var(--warning); }
.toast.info { border-color: var(--primary); }
@keyframes toastIn { from { opacity: 0; transform: translateX(20px); } to { opacity: 1; transform: translateX(0); } }

/* AI Toggle Button */
.ai-toggle { position: fixed; bottom: 76px; right: 24px; width: 44px; height: 44px; border-radius: 50%; background: var(--bg-surface); border: 1px solid var(--glass-border); color: var(--ink); font-size: 20px; cursor: pointer; display: flex; align-items: center; justify-content: center; z-index: 1000; box-shadow: var(--shadow-md); transition: all var(--transition-base); }
.ai-toggle:hover { transform: scale(1.1); box-shadow: var(--shadow-lg); }

/* Theme Toggle Button */
.theme-toggle { position: fixed; bottom: 24px; right: 24px; width: 44px; height: 44px; border-radius: 50%; background: var(--bg-surface); border: 1px solid var(--glass-border); color: var(--ink); font-size: 20px; cursor: pointer; display: flex; align-items: center; justify-content: center; z-index: 1000; box-shadow: var(--shadow-md); transition: all var(--transition-base); }
.theme-toggle:hover { transform: scale(1.1); box-shadow: var(--shadow-lg); }

/* Keyboard Shortcut Hint */
.shortcut-hint { position: fixed; bottom: 28px; left: 50%; transform: translateX(-50%); background: var(--bg-card); border: 1px solid var(--glass-border); border-radius: var(--radius-sm); padding: 8px 16px; font-size: 11px; color: var(--ink-muted); z-index: 1000; pointer-events: none; opacity: 0; transition: opacity 0.3s ease; }
.shortcut-hint.show { opacity: 1; }

/* Light Theme */
body.light-theme { --bg-deep: #f5f6f8; --bg-surface: #ffffff; --bg-card: rgba(0,0,0,0.02); --bg-card-hover: rgba(0,0,0,0.04); --glass-border: rgba(0,0,0,0.08); --glass-border-strong: rgba(0,0,0,0.12); --ink: #1a1d23; --ink-muted: #4a5058; --ink-subtle: #6b7280; --ink-tertiary: #9ca3af; --primary-glow: rgba(94,106,210,0.2); --shadow-md: 0 4px 16px rgba(0,0,0,0.08); --shadow-lg: 0 8px 32px rgba(0,0,0,0.12); --shadow-glow: 0 0 20px var(--primary-glow); }
body.light-theme .header { background: rgba(255,255,255,0.85); }
body.light-theme .nav { background: rgba(255,255,255,0.8); }
body.light-theme .status-bar { background: rgba(255,255,255,0.9); }
body.light-theme .ai-assistant-panel { background: #ffffff; }
body.light-theme .ai-msg.assistant { background: #ffffff; }
body.light-theme .ai-context { background: rgba(94,106,210,0.05) !important; }
body.light-theme .card { background: #ffffff; box-shadow: 0 2px 8px rgba(0,0,0,0.06); }
body.light-theme .card:hover { box-shadow: 0 4px 16px rgba(0,0,0,0.1); }
body.light-theme .uz { background: rgba(0,0,0,0.02); }
body.light-theme .uz:hover { background: rgba(94,106,210,0.05); }
body.light-theme .btn-s { background: #ffffff; }
body.light-theme .btn-s:hover { background: #f5f6f8; }
body.light-theme .fg input, body.light-theme .fg select, body.light-theme .fg textarea { background: #ffffff; }
body.light-theme .ai-assistant-input input { background: #f5f6f8; }
body.light-theme .log { background: #f5f6f8; }
body.light-theme .stat-card { background: #ffffff; }
body.light-theme th { background: #f5f6f8; }
body.light-theme .nav-toggle { background: #ffffff; }
body.light-theme .nav-toggle:hover { background: rgba(94,106,210,0.1); }
body.light-theme .file-item { background: #ffffff; }
body.light-theme .file-item:hover { background: #f5f6f8; }
body.light-theme .file-selector { background: #ffffff; }
body.light-theme .ai-settings { background: #ffffff; }
body.light-theme .report-table-dark table { background: #ffffff; color: #1a1d23; }
body.light-theme .report-table-dark th { background: #f3f4f6; color: #1a1d23; border-color: #d1d5db; }
body.light-theme .report-table-dark td { color: #1a1d23; border-color: #e5e7eb; }
body.light-theme .shortcut-table th, body.light-theme .shortcut-table td { color: #4a5058; border-color: rgba(0,0,0,0.08); }
body.light-theme .shortcut-table kbd { background: #f3f4f6; color: #1a1d23; border-color: #d1d5db; }

/* Responsive */
@media (max-width: 767px) {
  body { font-size: 16px; line-height: 1.6; }
  .header { height: 48px; padding: 0 16px; gap: 12px; }
  .logo { font-size: 16px; }
  .tagline { font-size: 10px; }
  .status { font-size: 12px; padding: 4px 8px; }
  .nav { font-size: 15px; }
  .nav-row { flex-wrap: wrap; gap: 4px; }
  .nbtn { font-size: 14px; padding: 6px 14px; }
  .main { padding: 20px 16px; }
  .header { padding: 0 16px; }
  .nav { padding: 4px 12px; }
  .nav-row { padding: 3px 0; gap: 2px; }
  .nav-cat { font-size: 9px; padding: 2px 6px; }
  .card { padding: 20px; margin-bottom: 16px; }
  .c-title { font-size: 17px; }
  .grid { grid-template-columns: 1fr; }
  .bgrp { flex-direction: column; }
  .btn { width: 100%; justify-content: center; }
  .stats-grid { grid-template-columns: 1fr 1fr; }
  .chart-container { height: 300px; }
  .ai-assistant-panel { width: 100%; right: -100%; top: 0; }
  .ai-assistant-panel.open { right: 0; }
  .ai-msg { max-width: 90%; }
  .toast-container { top: auto; bottom: 48px; right: 8px; left: 8px; }
  .status-bar { height: 36px; font-size: 10px; }
  .ai-toggle, .theme-toggle { width: 48px; height: 48px; bottom: 60px; }
  .theme-toggle { bottom: 120px; }
}

/* Touch device optimizations */
@media (hover: none) and (pointer: coarse) {
  .btn:hover { transform: none; }
  .card:hover { transform: none; }
  .nbtn:hover { background: transparent; }
  .nbtn.on:hover { background: var(--primary); }
}

/* Reduce motion for accessibility */
@media (prefers-reduced-motion: reduce) {
  * { animation-duration: 0.01ms !important; animation-iteration-count: 1 !important; transition-duration: 0.01ms !important; }
}
'''

with open('static/style.css', 'w', encoding='utf-8') as f:
    f.write(css)

print(f"CSS file rebuilt: {len(css)} chars")

# scripts/ — 运维与构建脚本

## 启动器套件（顶层 .py，对应修复版移植）

| 文件 | 用途 | 启动方式 |
|---|---|---|
| `launcher.py` | 跨平台启动入口（自动调起 一键启动.bat） | `python launcher.py` |
| `start.py` | Python 版启动器（支持 `--port` / `--no-browser` 参数） | `python start.py --port 8080` |
| `watchdog_server.py` | 进程级看门狗（被外部强杀时自动拉起 run_server.py） | 后台：`start "" /B python watchdog_server.py` |
| `run_worker.py` | 独立 Worker 进程（定时任务调度，与主 Web 隔离） | `EMBED_SCHEDULER=0 python run_worker.py` |

### 启动器组合方式（推荐）

**日常开发 / 单机部署**：
```bash
python start.py
# 或：start.bat（Windows 一键脚本）
```

**生产 / 防宕机部署**（让 watchdog 自动拉起）：
```bat
:: 启动主服务 + 后台 watchdog
start "" /B python watchdog_server.py
python start.py
```

**主从进程拆分**（Web 与调度隔离）：
```bat
:: 主进程：只跑 Web，禁调度
set EMBED_SCHEDULER=0
python start.py

:: Worker 进程：接管调度
python run_worker.py
```

## 调试探针（顶层 _xxx.py）

| 文件 | 用途 |
|---|---|
| `_e2e_multi.py` | 端到端多轮 AI 助手对话验证（agent tool-calling 续接） |
| `_health_check_all.py` | 全站路由健康检查（枚举所有路由探测） |
| `_ltm_quick_test.py` | 长期记忆 (Long-Term Memory) 快速验证 |
| `_planner_integration_test.py` | Planner 模块集成测试 |
| `_scan_think.py` | 思维链扫描工具（看模型有没有按预期先 think） |
| `_sse.py` | SSE (Server-Sent Events) 流式输出探针 |
| `_stream_probe.py` | 流式响应探测（断点检测 / 延迟分布） |
| `_t11_debug.py` | 任务 T11 专项调试 |
| `_verify_http.py` | HTTP 端点连通性检查（批量） |

## UI 构建脚本

| 文件 | 用途 |
|---|---|
| `scripts/rebuild_css.py` | v1 风格 CSS 重生成（Linear Tech Design System） |
| `scripts/rebuild_css_v2.py` | v2 改进版 CSS（设计系统迭代） |

## 测试运行

参见顶层 `pytest.ini` 与 `tests/conftest.py`。常用命令：

```bash
# 跑全部测试（跳过 network 标记）
pytest tests/ -m "not network"

# 跑单个文件
pytest tests/test_scorecard.py -v

# 跑网络相关（需要联网 + 数据源 token）
pytest tests/ -m "network"
```

## 知识库迁移（顶层 .py）

| 文件 | 用途 |
|---|---|
| `migrate_knowledge.py` | 旧 RAG → 新 knowledge_db 表结构迁移 |
| `gen_knowledge.py` | 把 63 条知识点重新生成 tab-knowledge.html 静态卡片 |

## 一键同步到U盘

| 文件 | 用途 |
|---|---|
| `同步到U盘.bat` | 已优化版（含 pyc 预编译 + 路径排除） |

---

_这些脚本从修复版（2026-09-11 011933）同步而来。启动器默认关闭，watchdog / worker 按需启用。_
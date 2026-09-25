# data.tool — 数据采集分析工具

> 一站式数据采集 · 清洗 · 分析 · 建模 · 金融量化 · AI 智能体的本地 Web 平台

基于 Flask + pandas/sklearn 的单机数据工作台，覆盖"数据采集 → 清洗 → 分析 → 建模 → 报告 → AI 辅助"的完整链路，同时内置金融量化（A股）、DID 计量、信用评分卡、知识库 RAG 等深度能力。

## ✨ 功能特性

| 模块 | 能力 |
|---|---|
| **数据采集** | 网页抓取、金融行情（akshare / tushare / baostock / yfinance / FRED 五源）、联网搜索、股票筛选器 |
| **数据清洗** | 智能编码检测（UTF-8/GBK）、清洗流水线（可撤销）、缺失值热力图、文本清洗 |
| **统计分析** | 描述统计、相关性、分组聚合、matplotlib/plotly 图表、时间序列（Prophet / ARIMA / ADF） |
| **机器学习** | 分类/回归/聚类、自动特征工程、模型管理与评估面板 |
| **金融量化** | K线、CAPM、Fama-French、均线/MACD 回测、财务报表比率、资金流向热力图 |
| **计量分析** | DID 双重差分全套（平行趋势 / 安慰剂 / 中介 / 异质性）、信用评分卡（WOE/IV/PSI 监控） |
| **NLQ 与 RAG** | 自然语言转 SQL（规则引擎 + 安全校验）、知识库问答（2000+ 内置知识点） |
| **AI 智能体** | 意图识别助手、ReAct 代理循环（工具缓存 / 进度流式）、多轮对话记忆、成本追踪 |
| **运维能力** | 外部数据库直连（MySQL/PG/ClickHouse）、SQL 审计、定时任务、内存监控、任务队列 |
| **工程底座** | JWT 认证 + API Key + 每日配额、限流、gzip、DataFrame LRU 缓存、代码沙箱、路径遍历防护 |

## 🚀 快速开始

### 环境要求

- Python 3.10+（开发环境使用 `D:/Scripts/python.exe`，可修改 `start.bat` 调整）
- 依赖：见 `requirements.txt`

### 安装

```bash
pip install -r requirements.txt
```

### 启动

```bash
# 开发模式（Flask 自带服务器，自动重载）
python app.py

# 生产模式（waitress 多线程，推荐）
start.bat
```

启动后访问：`http://127.0.0.1:5000`（局域网内手机访问：`http://<本机IP>:5000`）

### 首次登录

- 默认管理员账号：`admin`
- 密码优先级：
  1. 环境变量 `ADMIN_PASSWORD`（推荐设置）
  2. 未设置时**随机生成**，打印在启动控制台

> ⚠ 生产部署请务必通过环境变量 `ADMIN_PASSWORD` 设置强密码，登录后立即修改。

## ⚙️ 环境变量

复制 `.env.example` 为 `.env` 后按需配置：

| 变量 | 说明 |
|---|---|
| `SECRET_KEY` | Flask/JWT 签名密钥。不设置时自动生成并持久化到 `data/secret.key` |
| `ADMIN_PASSWORD` | 首次创建 admin 用户的密码（不设置则随机生成） |
| `FLASK_HOST` / `FLASK_PORT` / `FLASK_DEBUG` | 服务器配置，默认 `0.0.0.0:5000` |
| `TUSHARE_TOKEN` | tushare 数据源 Token（可选） |
| `SENSENOVA_API_KEY` / `DEEPSEEK_API_KEY` | LLM API Key，用于 AI 助手/智能体（可选） |

## 🧪 测试

```bash
python -m pytest -v
```

- 测试位于 `tests/`，覆盖：知识库 RAG、信用评分卡、Web 搜索、ML 训练器、金融量化（CAPM/回测）、DID 计量、AI 意图识别、NLQ 引擎
- 标 `network` 的测试默认跳过（需联网）
- 部分测试依赖 `linearmodels`（DID），缺失时自动跳过对应用例

## 📁 项目结构

```
├── app.py                  # Flask 应用入口（限流/压缩/错误处理/路由注册）
├── run_server.py           # waitress 生产服务器入口
├── core_config.py          # 共享配置、DataFrame 缓存、模块装配
├── routes/                 # 20+ 路由注册器（按领域拆分，共 220+ API）
├── templates/  static/     # 前端 SPA（深色科技风，37 个功能 Tab）
├── tests/                  # pytest 自动化测试
├── data/                   # SQLite 数据库、密钥、对话历史（运行时生成）
├── output/                 # 数据文件、模型、报表输出（运行时生成）
└── design/                 # UI 设计稿与设计系统文档
```

核心模块：`scraper`（采集）、`clean_advanced`（清洗）、`ml_module`（ML）、`finance_quant`（回测）、`did`（计量）、`scorecard`（评分卡）、`nlq_engine`（NL→SQL）、`knowledge_rag`（知识库）、`ai_assistant`（AI 助手）、`ai_agent`（ReAct 代理）、`sandbox_exec`（代码沙箱）。

## 🔒 安全说明

- 密码使用 werkzeug 哈希存储，API 凭证 Fernet 加密落盘（LLM Key 可用 `secure_config.set_llm_api_key` 写入加密库，避免 .env 明文）
- 上传文件白名单校验 + 100MB 限制；代码执行经 AST 静态审查（plugin_sandbox 统一策略）+ 子进程沙箱（超时/内存/危险模式拦截）
- SQL 只允许只读 SELECT，注入关键字拦截；定时任务执行带审计日志
- 鉴权模型（本地优先）：文件读写/代码执行/插件/模型训练等高危接口挂 `local_guard`——本地/局域网匿名回退 admin，公网请求必须携带 JWT 或 API Key（经隧道暴露时自动生效）
- 所有外部访问端点默认限流（200/min），敏感接口需 JWT 或 API Key
- 生产部署建议：设置 `SECRET_KEY` 与 `ADMIN_PASSWORD`、关闭 `FLASK_DEBUG`、置于反向代理之后、不要将 0.0.0.0 直接暴露公网

## 📄 License

内部工具，仅限授权使用。

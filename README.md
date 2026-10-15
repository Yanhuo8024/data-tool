# data.tool — 数据采集分析工具

<p align="center">
  <b>一站式数据采集 · 清洗 · 分析 · 建模 · 金融量化 · AI 智能体 的本地 Web 平台</b>
</p>

<p align="center">
  <img alt="Python" src="https://img.shields.io/badge/Python-3.10%2B-blue">
  <img alt="Flask" src="https://img.shields.io/badge/Flask-Web-green">
  <img alt="Tests" src="https://img.shields.io/badge/pytest-2100%2B%20passed-brightgreen">
  <img alt="License" src="https://img.shields.io/badge/License-MIT-yellow">
</p>

基于 Flask + pandas/scikit-learn 的**单机数据工作台**，覆盖「数据采集 → 清洗 → 分析 → 建模 → 回测 → 报告 → AI 辅助」完整链路，内置金融量化（A股/港股）、DID 因果推断、信用评分卡、知识库 RAG、多 Agent 智能体等深度能力。所有数据默认留在本机，开箱即用。

## 目录

- [功能特性](#-功能特性)
- [快速开始](#-快速开始)
- [环境变量](#️-环境变量)
- [测试](#-测试)
- [项目结构](#-项目结构)
- [常见问题](#-常见问题)
- [安全说明](#-安全说明)
- [免责声明](#-免责声明)
- [路线图](#-路线图)
- [License](#-license)

## ✨ 功能特性

| 模块 | 能力 |
|---|---|
| **数据接入** | 文件上传（CSV/XLSX/TXT，多级编码嗅探）、网页爬取、金融五源行情（akshare/tushare/baostock/yfinance/FRED）、数据源导航、联网搜索 |
| **数据准备** | 智能编码清洗流水线（可撤销）、缺失值热力图、表格工具（JOIN/透视）、英文列名自动翻译展示、持久化存储与数据血缘/分享 |
| **统计与可视化** | 描述统计、相关、分组聚合、智能图表推荐、交互图表、假设检验（t/ANOVA/卡方/正态性等） |
| **时序与建模** | 时序分析（Prophet/ARIMA/ADF/分解）、机器学习（分类/回归/聚类）、7 类自动特征工程、预测模型 7 种横向评估对比、Pipeline 流水线 |
| **策略回测** | 专业级回测引擎：均线/MACD 信号参数分离、T+1 开盘成交（防未来函数）、全成本建模（佣金/印花税/滑点）、成交约束（涨跌停/成交量/整手）、止损止盈与最大回撤风控、Sharpe/Sortino/Calmar/Alpha/Beta/IR 全套指标、样本内外验证/滚动回测/参数敏感性热力图/蒙特卡洛抗过拟合 |
| **因子与估值** | CAPM、Fama-French 五因子（支持板块筛选与自定义行业输入、分层随机抽样）、Brinson 归因、DCF/可比公司/先例交易估值、财报三表与杜邦分析 |
| **基金与固收** | 基金风险收益指标（Sharpe/Sortino/Calmar/回撤）、久期/凸性/DV01/收益率曲线/信用利差与压力测试 |
| **计量与风控** | DID 全套（平行趋势/安慰剂/中介/异质性）、信用评分卡（卡方/分位数分箱→WOE/IV→LR→KS/AUC/PSI/拒绝推断、参数调优建议、PDF 中文导出） |
| **NLQ 与知识** | 自然语言转 SQL（只读审计）、知识库管理、SSE 流式 RAG 问答（回答缓存秒回）、NLP 文本分析（分词/情感/TF-IDF/词云）、模板市场 |
| **AI 智能体** | 意图路由助手、LangGraph ReAct（32+ 工具）、多 Agent 协同/并行管线、辩论引擎自审、流程可视化画布、数字台账与事实核验（公式复核/证据分级/假核验拦截） |
| **运维与安全** | 外部数据库直连、SQL 审计、定时任务、全局搜索、JWT/API Key/配额、限流、代码沙箱、安全中心 |

## 🚀 快速开始

### 环境要求

- Windows 10+（一键脚本）或任意平台（Python 3.10+）
- 依赖：见 `requirements.txt`

### 方式一：源码运行（推荐开发者）

```bash
# 1. 创建虚拟环境并安装依赖
python -m venv venv
venv\Scripts\pip install -r requirements.txt

# 2. （可选）配置环境变量
copy .env.example .env    # 填入 ADMIN_PASSWORD / LLM Key 等

# 3. 启动
python app.py             # 开发模式（自动重载）
# 或
python run_server.py      # 生产模式（waitress 多线程）
```

### 方式二：一键脚本（Windows）

```bat
:: 首次：安装依赖
install.bat

:: 日常启动（自动找 Python → 释放端口 → 打开浏览器）
start.bat
:: 停止
stop.bat
```

启动后访问 **http://127.0.0.1:5000**（局域网设备：`http://<本机IP>:5000`，防火墙弹窗点"允许访问"）。

### 方式三：便携 U 盘版（免安装）

1. 在本机运行一次 `setup_portable.bat`（下载嵌入式 Python + 依赖，约 2.3GB）
2. 把整个项目文件夹拷贝到 U 盘
3. 任意 Windows 电脑上双击 `start.bat` 即可运行，无需装 Python

> 增量同步到 U 盘可用 `同步到U盘.bat`（只拷贝新文件，含字节码预编译加速冷启动）。

### 首次登录

- 管理员账号：`admin`
- 密码优先级：环境变量 `ADMIN_PASSWORD` > 随机生成并打印在启动控制台

> ⚠ 生产部署务必通过 `ADMIN_PASSWORD` 设置强密码，登录后立即修改。

## ⚙️ 环境变量

复制 `.env.example` 为 `.env` 后按需配置（完整列表见该文件）：

| 变量 | 说明 |
|---|---|
| `SECRET_KEY` | Flask/JWT 签名密钥。不设置时自动生成并持久化到 `data/secret.key` |
| `ADMIN_PASSWORD` | 首次创建 admin 用户的密码（不设置则随机生成） |
| `FLASK_HOST` / `FLASK_PORT` / `FLASK_DEBUG` | 服务器配置，默认 `0.0.0.0:5000` |
| `TUSHARE_TOKEN` | tushare 数据源 Token（可选，金融行情增强） |
| `SENSENOVA_API_KEY` / `DEEPSEEK_API_KEY` | LLM API Key，用于 AI 助手/智能体（可选） |

> LLM Key 推荐写入加密凭据库而非 .env 明文：
> `python -c "from secure_config import set_llm_api_key; set_llm_api_key('sensenova', 'sk-...')"`

## 🧪 测试

```bash
python -m pytest -v
```

- 测试位于 `tests/`，2100+ 用例，覆盖：知识库 RAG、信用评分卡、Web 搜索、ML 训练器、金融量化（CAPM/回测/五因子）、DID 计量、AI 意图识别、NLQ 引擎、数字台账核验
- 标 `network` 的测试默认跳过（需联网）
- 部分测试依赖 `linearmodels`（DID），缺失时自动跳过对应用例
- CI 级架构守卫（`tools/arch_guard.py`）：文件行数、吞异常、循环依赖、出境主机白名单

## 📁 项目结构

```
├── app.py                  # Flask 应用入口（限流/压缩/错误处理/路由注册）
├── run_server.py           # waitress 生产服务器入口
├── core_config.py          # 共享配置、DataFrame 缓存、模块装配
├── routes/                 # 20+ 路由注册器（按领域拆分，260+ API）
├── templates/ static/      # 前端 SPA（深色科技风，37 个功能 Tab）
├── backtest_*.py           # 回测引擎（信号/成交/成本/风控/指标/抗过拟合验证）
├── factor_model.py         # Fama-French 五因子（板块筛选见 factor_industry.py）
├── finance_quant.py        # CAPM / 旧版回测 / 预测评估
├── ai_agent.py             # LangGraph ReAct 智能体
├── rag_answer.py           # RAG 流式问答编排
├── tests/                  # pytest 自动化测试
├── data/                   # SQLite 数据库、密钥、对话历史（运行时生成，不入库）
├── output/                 # 数据文件、模型、报表输出（运行时生成，不入库）
├── python-portable/        # 嵌入式 Python 运行时（仅便携版，不入库）
└── design/                 # UI 设计稿与设计系统文档
```

核心模块：`scraper`（采集）、`clean_advanced`（清洗）、`ml_module`（ML）、`backtest_engine`/`backtest_service`（回测）、`did`（计量）、`scorecard`（评分卡）、`nlq_engine`（NL→SQL）、`knowledge_rag`/`rag_answer`（知识库问答）、`ai_assistant`/`ai_agent`（AI 助手/智能体）、`calc_check`/`number_ledger`（数字核验）、`sandbox_exec`（代码沙箱）。

## ❓ 常见问题

**Q: 端口 5000 被占用？**
`start.bat` 会自动释放端口；手动改：设置 `FLASK_PORT=8080` 后重启。

**Q: AI 助手不回答 / 报 429？**
未配置 LLM Key 或免费额度限流。用 `secure_config.set_llm_api_key` 写入 Key，或等待额度重置。

**Q: 金融接口取不到数据？**
akshare/baostock 为免费源，偶发限流属正常；tushare 需配置 `TUSHARE_TOKEN`。联网功能需可访问外网。

**Q: U 盘版启动报 ModuleNotFoundError？**
先在本机跑一次 `setup_portable.bat` 确保 `python-portable/` 完整，再整体拷贝；增量同步用 `同步到U盘.bat`。

**Q: 如何备份/迁移数据？**
只需拷贝 `data/`（数据库与配置）与 `output/`（你的文件），代码可随时重装。

## 🔒 安全说明

- 密码使用 werkzeug 哈希存储；API 凭证 Fernet 加密落盘（避免 .env 明文）
- 上传文件白名单校验 + 100MB 限制；代码执行经 AST 静态审查 + 子进程沙箱（超时/内存/危险模式拦截）
- SQL 只允许只读 SELECT，注入关键字拦截；定时任务执行带审计日志
- 鉴权模型（本地优先）：高危接口挂 `local_guard`——本地/局域网匿名回退 admin，公网请求必须携带 JWT 或 API Key
- 外部访问端点默认限流（200/min）；所有外部主机走出境白名单
- 生产部署建议：设置 `SECRET_KEY` 与 `ADMIN_PASSWORD`、关闭 `FLASK_DEBUG`、置于反向代理之后、不要将 `0.0.0.0` 直接暴露公网

## ⚠️ 免责声明

本工具的金融回测、因子分析、估值与行情功能**仅供学习与研究使用，不构成任何投资建议**。回测基于历史数据，过往表现不代表未来收益；涉及真实交易决策请自行判断并承担风险。行情与财务数据来自公开接口，其准确性、完整性与时效性不做担保。

## 🗺 路线图

- [ ] 多标的组合回测（等权/市值加权/风险平价、调仓周期、组合归因）
- [ ] 分钟级回测与逐笔成交模拟
- [ ] 因子库扩展（动量/质量/红利因子与因子投资组合）
- [ ] RAG 混合检索升级（重排序 + 知识图谱）
- [ ] 多语言界面（English / 中文切换）

欢迎提 [Issue](../../issues) 与 [PR](../../pulls)。

## 📄 License

[MIT License](LICENSE)

> 历史版本曾标注为内部工具；2026-10 起开源发布采用 MIT。如需私有部署可保留版权声明与免责声明段落。

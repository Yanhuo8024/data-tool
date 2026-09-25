# 数据外发清单（DATA EGRESS）

> 中文名：数据外发清单 ｜ 维护：随出站代码变更同步更新
> 生成方式：对全仓 Python 源码做静态检索（出站调用点 + 环境开关 + 载荷构造），非配置声明。
> 检索口径与复现命令见文末 §6；**§8 是机器校验的登记表**（`tools/arch_guard.py` 的 R8 规则）。
> 姊妹文档：`design/` 目录下的架构说明；结构规则由 `tools/arch_guard.py` 守护。

## 0. 结论先行

1. **不存在"整工作区打包上传"类设计**：全仓 0 处对外**文件上传**接口——无出站 multipart、
   无 OSS/S3 SDK、无 `put_object`/`upload_file`。`validate_upload_file()` 是**入站**校验，方向相反。
2. 出站点共 **6 类**，全部可枚举，均写在本文；其中 3 类需用户显式触发，2 类默认开启（云 LLM、
   联网搜索）、**且两者现在都有开关**，1 类默认关闭（遥测）。
3. **本机数据出境的唯一实质通道是"云 LLM 推理"**，其中最大的一笔是**上传数据文件的样本行**
   （见 E2）——不是文件名，是内容。
4. 两条可关闭通道的**默认值、当前生效值、开关位置**：

   | 通道 | 环境变量 | 默认 | 开关位置 | 关闭后行为 |
   | --- | --- | --- | --- | --- |
   | 联网搜索 | `ALLOW_WEB_SEARCH` | **开启** | AI 助手 ⚙️ 设置 → 数据出境 | 工具不注册，不外呼 |
   | 文件样本出境 | `ALLOW_FILE_SAMPLE_EGRESS` | **开启** | AI 助手 ⚙️ 设置 → 数据出境 | 只上行列名与统计量 |

   代码：`egress_policy.py`（策略与优先级）｜接口：`GET/POST /api/assistant/egress-policy`
   优先级：**环境变量（部署方强制） > 设置面板（持久化 `data/egress_policy.json`） > 默认开启**。

## 1. 出站点总表

| # | 通道 | 对端 | 上行内容 | 触发条件 | 默认 | 关闭方式 |
| --- | --- | --- | --- | --- | --- | --- |
| E1 | 云 LLM 推理 | `token.sensenova.cn`（默认）等 16 个 provider | 系统提示词 + 知识库片段 + 会话历史 + **数据文件样本 / 文档正文** + **用户上传图片** + 工具结果 | 用户在 AI 助手提问 | **开启**（有凭据即用） | 不配置 API Key |
| E2 | 文件内容进 Prompt | 经 E1 出站 | CSV/Excel **样本前 6000 字符**；文档类（PDF/DOCX/TXT/MD/JSON/HTML）**正文前 20000 字符**；RAG 片段前 300 字符 | 该文件被选为当前文件并提问 | **开启** | 设置 → 数据出境 → 关闭（转「仅本地统计」，文档只剩元信息），或移除当前文件 |
| E3 | 联网搜索 | `cn.bing.com`（主）、`www.baidu.com`（备）、`html.duckduckgo.com`（兜底） | 由问题清洗出的**搜索关键词** | 意图判定命中，或 Agent 选 `web_search` 工具 | **开启**（可关） | 设置 → 数据出境 → 关闭联网搜索，或 `ALLOW_WEB_SEARCH=0` |
| E4 | 行情/宏观数据 | akshare（东财等）、baostock、tushare、yfinance、FRED | 股票/指数代码、日期区间、频度 | 用户请求行情/财务/宏观数据 | 按需 | 不调用相应功能 |
| E5 | 遥测导出（OTel） | `TRACE_OTEL_ENDPOINT` 指定地址 | Agent 运行事件 span（名称/属性/序号/错误） | 显式设置 endpoint | **关闭** | 不设置该变量即关闭 |
| E6 | 前端 CDN（浏览器侧） | `cdn.jsdelivr.net` | 静态资源请求（含访客 IP/UA，无业务数据） | 打开引用 CDN 的页面 | 开启 | 改为本地静态资源（§5-P2-3） |
| E7 | 识图（多模态） | 经 E1 出站（云端视觉模型） | 用户上传/粘贴的**图片原文**（base64，最多 4 张） | 对话中带图提问 | **开启**（可关） | 设置 → 数据出境 → 关闭「图片出境（识图）」，或 `ALLOW_IMAGE_EGRESS=0` |

## 2. 逐条说明

### E1 云 LLM 推理（最大出口）
- 代码：`llm_client.py`（`_PROVIDER_URLS` L33-49、`_build_payload`）、`ai_assistant.py`、`ai_agent.py`、
  `agent_pipeline.py`。
- 默认路由：provider=`sensenova`，model=`glm-5.2`。
- 上行载荷字段：`model` / `messages` / `max_tokens` / `reasoning_effort` / `tools` / `tool_choice`。
  其中 `messages` 含**系统提示词全文**，因此也含技能说明、知识库检索片段、会话历史。
- 可选 provider 共 16 个（全部列于 §8），均为**自带 Key 才可用**，Key 存 `data/credentials.enc`（§4）。
- 说明：换 provider 只改对端，不改变"内容仍在出境"这一事实。

### E2 数据文件与文档内容进入 Prompt（E1 的放大项）
- 代码：`file_rag.py`（`_load_as_text` → 表格 `sample.to_string(index=False)[:6000]`、
  文档 `_load_doc_as_text` → 正文 `[:20000]`；`search_file_rag` → `[:300]`）。
- 含义：**上传文件的真实内容**会随提问发往云端模型。这是本系统数据出境的主要体量来源，
  也是客户/合规最需要知情的一点。覆盖两类：
  - 表格类（csv/xlsx/xls/parquet）：数据行与列结构；
  - 文档类（pdf/docx/txt/md/json/html）：**正文全文**（研报、财报、合同、产品规则等）。
    解析库缺失时（PDF 需 `pypdf` 或 PyMuPDF）只索引元信息，并明写"未成功解析"，
    不让模型凭空推断内容。
- **知情提示**：挂了当前文件时，AI 助手上下文栏常驻显示「📤 文件样本会随提问发往云端模型」。
- **可关闭（仅本地统计模式）**：设置 → 数据出境 → 关闭后，`file_rag` 表格只索引**列名 / 类型 /
  缺失 / 数值分布（describe）**，文档只索引**文件名 / 类型 / 体积 / 字符数**，原文不进入索引；
  `tool_file_rag` 与已索引内容会**自动重建**，关闭后检索不到旧内容；
  此时上下文栏显示「🔒 仅本地统计：样本行不外发」。

### E3 联网搜索
- 代码：`web_search.py`（多源链）、`ai_assistant.py`（`_web_search_answer` 等）、
  `ai_agent.py`（工具注册 `tool_web_search`）、`agent_pipeline.py`（市场/合规阶段工具白名单）。
- 上行：`build_search_query()` 从用户问题中剔除提问词后得到的关键词（**不是**整段原始问题）。
- **已补开关**：`ALLOW_WEB_SEARCH`（默认开启）。关闭后 `ToolRegistry` 不再列出/分发该工具
  （prompt 与执行共用同一闸门），`web_search()` 本身也直接返回关闭提示，不发外呼。

### E4 行情 / 宏观数据接口
- 免密钥：akshare（`push2.eastmoney.com`、`quote.eastmoney.com`、`data.eastmoney.com`、
  `datacenter-web.eastmoney.com` 等）、baostock、yfinance。
- 需密钥：tushare（`TUSHARE_TOKEN`）、FRED（`FRED_API_KEY`）。
- 上行仅为代码与日期区间等**市场参数**，不含用户上传的业务数据。
- ⚠️ 边界：akshare / baostock / yfinance 的**具体端点由库内部拼接**，R8 看不到（只扫本仓字符串
  常量），本文按其官方文档列出已知域名。

### E5 遥测导出（OpenTelemetry 槽位）
- 代码：`trace_store.py`（`OTEL_ENABLED` / `_otel_emit`）。
- 默认 `OTEL_ENABLED=False`；仅当 `TRACE_OTEL_ENDPOINT` 非空或 `TRACE_EXPORTER=otel` 时才导出，
  且未安装 `opentelemetry-*` 时自动跳过。**这是本仓"默认关闭 + 开关可见"的范例做法。**
- ⚠️ 上端点是**动态值**，无法静态登记——R8 对这类文件只给 info 提示，需人工确认。

### E6 前端 CDN（浏览器侧出站）
- `routes/openapi_routes.py` 等页面引用 `cdn.jsdelivr.net` 的静态资源；请求由**用户浏览器**发出，
  服务端不参与，也不携带业务数据（仅 IP/UA）。R8 只检查服务端调用，本条为人工登记。

### E7 识图（图片原文出境）
- 代码：`ai_assistant.py`（同步 `process_message` 与流式 `process_message_stream` 两处
  `if images:` 分支）、`egress_policy.py`（`image_egress` 特性 + `vision_blocked_reply()`）。
- 上行：用户上传/粘贴的图片**原文**（`data:image/...;base64,...`，最多 4 张）随 `image_url`
  消息体发往云端视觉模型。图片是**非结构化资料的原样外发**，体量与敏感度都高于文本摘要，
  因此单列一条。
- **已补开关**：`ALLOW_IMAGE_EGRESS`（默认开启）。关闭后识图分支在**构造消息体之前**返回，
  图片不会进入任何请求；同步返回 `ok=False` 的说明，流式以 `done` 事件同口径返回。
- **两处同一判定**：同步与流式共用 `_vision_egress_blocked()`，防止"同步拦住了、流式还在发图"。

## 3. 经检索确认**不**外发的内容

- **文件上传**：0 处（无出站 multipart / OSS / S3）。
- **仓库快照 / .git 打包**：本仓库根无 `.git`，工作区内无任何"归档→上传"链路。
- **轨迹与流程可视化**：`output/traces/*.jsonl` 本地；`agent_flow` / `flowviz` 读本地。
- **报告导出**：Word / Excel / PDF / HTML 仅写本地 `output/`。
- **会话与归档**：`data/chat_history.json`、`data/chat_history/YYYY-MM-DD/` 本地。
- **向量与嵌入**：本地计算（`sentence_transformers` + TF-IDF + 内置 `_MiniBM25`）。
  注意：首次使用 `BAAI/bge-m3` 会从 HuggingFace 拉**模型权重**（上行仅模型标识，不含用户数据）。
- **本地模型**：Ollama 仅连 `127.0.0.1:11434`。

## 4. 本地凭据与加密

| 项 | 位置 | 说明 |
| --- | --- | --- |
| 云服务凭据 | `data/credentials.enc` | Fernet 加密；依赖 `cryptography`，缺失时静默失效 |
| 加密主密钥 | `data/secret.key` | 首次运行自动生成 |
| DB 凭据 | `data/db_secret.key` | 同上机制 |
| 出境开关 | `data/egress_policy.json` | 明文；仅两个布尔值，不含敏感信息 |
| 环境变量兜底 | `SENSENOVA_API_KEY`、`TUSHARE_TOKEN`、`FRED_API_KEY`、`SECRET_KEY`、`ADMIN_PASSWORD` … | 共 56 个应用侧环境变量 |

**风险提示**：`secret.key` 与 `credentials.enc` 同目录——拿到磁盘即可解密。若防护目标包含
"设备被带走"，密钥需移出数据目录或改为外部注入（§5-P2-1）。

## 5. 缺口与建议

- ~~P0-1 联网搜索补开关~~ **已完成 2026-09-21**：`ALLOW_WEB_SEARCH`，默认开启；工具注册表与
  运行时共用同一闸门；设置面板显示默认值 / 当前生效值 / 来源（环境变量 / 面板 / 默认）。
- ~~P0-2 文件内容出境须知情~~ **已完成 2026-09-21**：上下文栏常驻知情提示 + 「仅本地统计」模式
  （`ALLOW_FILE_SAMPLE_EGRESS`），切换后自动重建索引，不残留旧样本。
- ~~P1-1 清单与代码防漂移~~ **已完成 2026-09-21**：`arch_guard.py` 新增 **R8**——出站调用的主机名
  必须在本文 §8 登记，未登记即回归失败（首跑即抓出 23 处未登记主机，已全部登记）。
- **P1-2 运行时可见**：每类出站计数（次数 / 字符数 / 耗时）写入 trace，健康面板可见。
- **P2-1 主密钥外置**：`data/secret.key` 支持外部注入，避免"密钥与密文同盘"。
- **P2-2 出站域名白名单**（运行时强制）：默认仅允许 §8 登记的对端，越界拒绝并告警。
- **P2-3 前端资源本地化**：移除 `cdn.jsdelivr.net` 依赖，改为本仓静态文件（去掉 E6）。
- **P2-4 E4 库内部端点**：为 akshare / baostock / yfinance 补一层包装，把实际请求端点也纳入登记。
- **P2-5 文档解析依赖**：PDF 需 `pypdf`（或 PyMuPDF）。当前便携运行时**未安装**，PDF 只登记
  元信息、正文不入索引。补齐后需同步到 U 盘运行时（见 `portable-python-dep-sync` 技能）。

## 5.5 边界与**不保证**（知情同意的另一半）

前四章讲的是"哪些内容会出去、怎么关"。同等重要的是**哪些事本机制不负责**——
把边界写清楚，用户才不会把开关当成兜底承诺：

1. **不覆盖用户主动发起的出口。** 本清单与开关只管 AI 助手的通道。用户自己在对话里
   粘贴的文本、在"数据接入"页填的 URL 抓取、DB 直连查询，走各自的既有逻辑，不经这里。
2. **不保证最小化。** 开启状态下，上行的是**原文片段**（表格 6000 字符 / 文档 20000 字符），
   不是摘要或脱敏结果。字段级脱敏（P0 路线图中的"数据脱敏"）尚未实现。
3. **不保证默认加密。** 上行链路依赖 provider 自身的 HTTPS；本机侧落盘为明文 JSON
   （策略文件、会话归档），仅凭据走 Fernet 加密（§4）。
4. **开关不等于撤销。** 关闭后**已发出**的内容无法收回，也无法从对方侧删除；
   `file_rag` 重建索引只清理本机副本。
5. **登记表是人工维护的。** §8 的白名单来自人工归纳，R8 只能发现"代码里出现但没登记"的主机；
   库内部动态拼接的端点（E4）扫不到——**登记表完整 ≠ 无出境**，只代表"已知出口都已声明"。
6. **识图限制。** 单次最多 4 张图片（`images[:4]`），超出部分静默丢弃，不做提示。

## 6. 复现方式（如何自查）

```bash
# 1) 结构守卫（含 R8 出站登记）：逐条规则可单跑
python tools/arch_guard.py                 # 全规则，exit=0 即通过
python tools/arch_guard.py --rule R8       # 只看出站登记
python tools/arch_guard.py --json          # 机器可读
python tools/arch_guard.py --self-test     # 守卫自证：违规样本逐条命中、干净样本 0 误报

# 2) 出站主机枚举（与 R8 同一口径：只取 AST 字符串常量里的 URL）
python - <<'PY'
import ast, os, re
CALL = re.compile(r"\brequests\.(get|post|put|delete|patch|request|Session)\b|\burlopen\b"
                  r"|\bhttp\.client\b|\bsocket\.(socket|create_connection)\b|\baiohttp\b|\bhttpx\b")
HOST = re.compile(r"https?://([A-Za-z0-9._\-]+)")
for dp, dn, fs in os.walk('.'):
    dn[:] = [d for d in dn if d not in {'.git', '.workbuddy', '.trae', '__pycache__', 'node_modules',
                                        'site-packages', 'python-portable', 'output', 'archive'}]
    for f in fs:
        if not f.endswith('.py'):
            continue
        p = os.path.join(dp, f)
        src = open(p, encoding='utf-8', errors='ignore').read()
        if not CALL.search(src):
            continue
        hosts = {h for n in ast.walk(ast.parse(src))
                 if isinstance(n, ast.Constant) and isinstance(n.value, str) and '://' in n.value
                 for h in HOST.findall(n.value)}
        print(f'{p:44s} {sorted(hosts)}')
PY

# 3) 出境开关当前生效值与来源
python -c "import egress_policy as e, json; print(json.dumps(e.snapshot(), ensure_ascii=False, indent=1))"
```

## 7. 变更记录

| 日期 | 变更 |
| --- | --- |
| 2026-09-21 | 首次建立。基线与方法：全仓 Python 静态检索；确认无出站上传通道；识别 E3 无开关、E2 内容出境两项缺口。 |
| 2026-09-21 | 补齐 P0-1 / P0-2 / P1-1：新增 `egress_policy.py` + `ALLOW_WEB_SEARCH` / `ALLOW_FILE_SAMPLE_EGRESS` + 设置面板开关与知情提示；`arch_guard.py` 新增 R8 出站登记规则；新增 E6（浏览器侧 CDN）；§8 登记表成为 R8 白名单来源。首跑登记 23 处未登记主机。 |
| 2026-09-21 | 第二轮补口：① E2 从"仅 CSV/Excel"扩到**文档类正文**（`file_rag` 支持 pdf/docx/txt/md/json/html，PDF 缺解析库时降级为元信息）；② 新增 **E7 识图**（图片原文出境）与开关 `ALLOW_IMAGE_EGRESS`，同步/流式共用 `vision_blocked_reply()`；③ 新增 **§5.5 边界与不保证** 6 条；④ 拆出 `text_clean.py`（`strip_thinking`）以满足 R1 行数棘轮——`ai_assistant.py` 3749→3741 行。 |

## 8. 出站主机登记表（R8 白名单）

> 规则：`tools/arch_guard.py` 的 **R8** 扫描应用代码（排除 `tests/` `tools/`）中的出站调用，
> 若 URL 主机名未出现在本文件中，**回归失败**。新增出站必须先在本节登记。
> 免登记（内置放行）：回环 `127.0.0.1` / `localhost` / `0.0.0.0`，文档保留域 `example.com`、
> `*.invalid`，JSON Schema 命名空间 `schemas`。

| 通道 | 登记主机 |
| --- | --- |
| E1 云 LLM（16） | `token.sensenova.cn` `api.deepseek.com` `open.bigmodel.cn` `dashscope.aliyuncs.com` `qianfan.baidubce.com` `ark.cn-beijing.volces.com` `api.moonshot.cn` `api.lingyiwanwu.com` `api.hunyuan.tencent.com` `spark-api-open.xf-yun.com` `api.openai.com` `api.anthropic.com` `generativelanguage.googleapis.com` `api.x.ai` `api.mistral.ai` `api.groq.com` |
| E3 联网搜索 | `www.baidu.com` `cn.bing.com` `html.duckduckgo.com` |
| E4 行情/宏观（本仓直连部分） | `push2.eastmoney.com` `quote.eastmoney.com` `data.eastmoney.com` `datacenter-web.eastmoney.com` `finance.sina.com.cn` `hq.sinajs.cn` `qt.gtimg.cn` `api.stlouisfed.org` `fred.stlouisfed.org` |
| E5 遥测（动态端点，不可静态登记） | 由 `TRACE_OTEL_ENDPOINT` 决定 —— R8 对此类文件只给 info |
| E6 前端 CDN（浏览器侧，R8 不扫描） | `cdn.jsdelivr.net` |

维护提示：删掉主机前先确认代码里已无引用（`python tools/arch_guard.py --rule R8` 会告诉你），
否则清单与实际不符——清单本身也是"默认值必须可查"的一部分。

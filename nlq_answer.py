"""自然语言查数：统一 Text-to-SQL 链路（真实表结构 + LLM 生成 + 只读护栏）。

取代 `nlq_engine.NLQEngine` 的规则实现——后者把表名写死为 `sales`、列名写死为
`amount`/`province`/`city` 等 41 处，schema 恒为空，`llm_config` 定义后从未使用，
实测任何提问都生成同一条 SQL 并报 `no such table: sales`。

本模块的处理顺序：
    确定目标库 → 取真实表结构 → LLM 生成只读 SQL → 人工预览（可选）
    → db_connector 双层只读校验 + 自动 LIMIT + 超时保护执行

对外只暴露 `answer()`。**绝不返回伪造数据**：任何失败都进 `errors` 且 `ok=False`。
环境变量 `NLQ_ANSWER_LLM=0` 可关闭生成（离线排查用），此时返回明确错误而非猜测降级。
"""
import os
import re
from pathlib import Path

from core_config import logger

BASE_DIR = Path(__file__).resolve().parent
MAX_TABLES = 20           # 送入提示词的表数量上限
MAX_SCHEMA_CHARS = 4000   # 表结构文本截断长度


def llm_enabled() -> bool:
    """是否允许调用 LLM 生成 SQL（env 开关，便于离线排查与测试）。"""
    return os.environ.get('NLQ_ANSWER_LLM', '1') != '0'


_SYS_PROMPT = (
    "你是资深 SQL 工程师。根据提供的表结构，把用户的自然语言问题转成一条只读 SQL。\n"
    "硬性要求：\n"
    "1. 只允许 SELECT/WITH 查询，禁止任何写入、修改、删除、建表语句；\n"
    "2. 表名和列名必须逐字使用表结构中提供的真实名称，禁止翻译、改写或自造；\n"
    "3. 仅聚合结果可以加中文别名（AS \"中文别名\"，用英文双引号包裹），普通列不加别名；\n"
    "4. 只输出 SQL 本身，不要解释、不要 markdown 代码块、不要分号结尾；\n"
    "5. 若问题与表结构无关或无法查询，只输出：-- 无法回答：<原因>"
)

_SQL_START = r'^(SELECT|WITH|EXPLAIN|SHOW|DESCRIBE|DESC)'


def clean_sql(raw: str) -> str:
    """清洗 LLM 输出：去 markdown 代码块与前置说明，取第一条有效语句。"""
    s = (raw or '').strip()
    m = re.search(r'```(?:sql)?\s*(.*?)```', s, flags=re.DOTALL | re.IGNORECASE)
    if m:
        s = m.group(1).strip()
    lines = [ln for ln in s.splitlines() if ln.strip()]
    if lines and not re.match(_SQL_START, lines[0].strip(), re.IGNORECASE):
        for i, ln in enumerate(lines):
            if re.match(_SQL_START, ln.strip(), re.IGNORECASE):
                s = '\n'.join(lines[i:])
                break
    return s.strip().rstrip(';').strip()


def detect_literal_cols(columns: list, rows: list) -> list:
    """检测字面量误用：SQLite 会把不存在的双引号列名（如 "名称"）当字符串常量返回，
    导致整列值都等于列名本身。这种结果不可信，须拦截重试。"""
    if not columns or not rows or len(rows) < 2:
        return []
    bad = []
    for i, col in enumerate(columns):
        vals = {r[i] for r in rows[:50] if len(r) > i}
        if vals == {col}:
            bad.append(col)
    return bad


def discover_sources() -> list:
    """自动发现可查数据源：output/ 下的 SQLite 文件 + 已保存的数据库连接。

    返回 [{id, label, kind, db_path|conn_id}]；id 可直接回传给 answer()。
    """
    sources = []
    out = BASE_DIR / 'output'
    try:
        if out.is_dir():
            files = sorted(out.glob('*.db'), key=lambda x: x.stat().st_mtime, reverse=True)
            for p in files[:MAX_TABLES]:
                sources.append({'id': 'sqlite:' + p.name, 'label': f'output/{p.name}',
                                'kind': 'sqlite', 'db_path': str(p)})
    except Exception as e:
        logger.debug(f"nlq discover output failed: {e}")
    try:
        from models import DBConnection
        for c in DBConnection.query.order_by(DBConnection.created_at.desc()).limit(MAX_TABLES).all():
            sources.append({'id': 'conn:' + c.id, 'label': f'{c.name} ({c.db_type})',
                            'kind': 'conn', 'conn_id': c.id})
    except Exception as e:
        logger.debug(f"nlq discover connections skipped: {e}")
    return sources


def split_source(source: str):
    """把 discover_sources() 返回的 id 拆成 (db_path, conn_id)。"""
    s = (source or '').strip()
    if s.startswith('conn:'):
        return None, s[5:]
    if s.startswith('sqlite:'):
        return str(BASE_DIR / 'output' / s[7:]), None
    return None, None


def _cfg_from_conn(conn_id: str):
    """已保存连接 → 引擎配置（解密密码）。"""
    from models import DBConnection
    from db_connector import decrypt_password
    c = DBConnection.query.get(conn_id)
    if not c:
        raise ValueError(f'连接不存在: {conn_id}')
    cfg = {
        'db_type': c.db_type, 'host': c.host, 'port': c.port,
        'database': c.database, 'username': c.username,
        'password': decrypt_password(c.password_enc or ''),
    }
    return cfg, f'{c.name} ({c.db_type})'


def resolve_target(db_path: str = None, conn_id: str = None, source: str = None):
    """确定查询目标，返回 (cfg, label)。优先 conn_id > db_path > source > 自动发现。"""
    from db_connector import safe_sqlite_path
    if source:
        s_path, s_conn = split_source(source)
        db_path = db_path or s_path
        conn_id = conn_id or s_conn
    if conn_id:
        return _cfg_from_conn(conn_id)
    if not db_path:
        found = [s for s in discover_sources() if s.get('kind') == 'sqlite']
        if not found:
            raise ValueError('未找到可查数据源：output/ 下没有 .db 文件，也没有已保存的数据库连接')
        db_path = found[0]['db_path']
    rp = safe_sqlite_path(db_path)
    if rp is None:
        raise ValueError(f'数据库路径不被允许（须位于 output/、uploads/ 或 data/ 内）: {db_path}')
    if not rp.exists():
        raise ValueError(f'数据库文件不存在: {rp.name}')
    return {'db_type': 'sqlite', 'database': str(rp)}, f'output/{rp.name}'


def generate_sql(cfg: dict, question: str, hint: str = '') -> str:
    """依真实表结构调用 LLM 生成只读 SQL（返回清洗后的文本）。"""
    from db_connector import schema_text
    from llm_client import chat
    ctx = (schema_text(cfg) or '')[:MAX_SCHEMA_CHARS]
    if not ctx.strip():
        raise ValueError('目标库中没有任何表，无法生成 SQL')
    user_prompt = (
        f"表结构：\n{ctx}\n\n"
        f"用户问题：{question}\n\n"
        f"{hint}\n请生成 SQL："
    )
    resp = chat([{'role': 'system', 'content': _SYS_PROMPT},
                 {'role': 'user', 'content': user_prompt}])
    return clean_sql((resp or {}).get('content') or '')


def _blank(question: str, preview: bool) -> dict:
    return {'ok': False, 'question': question, 'sql': '', 'preview': bool(preview),
            'source': '', 'columns': [], 'rows': [], 'data': [],
            'truncated': False, 'errors': [], 'notice': '', 'llm': llm_enabled()}


def answer(question: str, *, db_path: str = None, conn_id: str = None,
           source: str = None, preview: bool = False, sql: str = None,
           allowed_tables: list = None) -> dict:
    """自然语言 → SQL → （可选）执行。

    preview=True 时只生成 SQL 供人工审核，不执行——这是默认建议的用法。
    传入 sql 则跳过生成、直接执行该条语句（用于「用户已审核」的二段式流程，
    避免确认执行时重新生成导致 SQL 漂移）；该语句同样经过只读校验。

    返回 {ok, question, sql, preview, source, columns, rows, data,
         truncated, errors, notice, llm}；rows 与 data 是同一份结果的两种形态。
    """
    question = (question or '').strip()
    out = _blank(question, preview)
    if not question and not sql:
        out['errors'].append('问题不能为空')
        return out
    try:
        cfg, label = resolve_target(db_path=db_path, conn_id=conn_id, source=source)
        out['source'] = label
    except Exception as e:
        out['errors'].append(str(e))
        return out

    from db_connector import execute_readonly_sql

    def _run(statement: str):
        r = execute_readonly_sql(cfg, statement, allowed_tables=allowed_tables)
        bad = detect_literal_cols(r.get('columns') or [], r.get('rows') or [])
        if bad:
            raise ValueError(f"引用了不存在的列名（{', '.join(bad)}），结果不可信")
        return r

    try:
        if sql:
            # 用户已审核的语句：跳过生成，直接执行
            statement = clean_sql(sql)
            if not statement:
                out['errors'].append('待执行的 SQL 为空')
                return out
            out['sql'] = statement
            if preview:
                out['ok'] = True
                return out
            res = _run(statement)
        else:
            if not llm_enabled():
                out['errors'].append('LLM 生成已关闭（NLQ_ANSWER_LLM=0），无法把问题转换为 SQL')
                return out
            generated = generate_sql(cfg, question)
            if not generated:
                out['errors'].append('模型未返回任何 SQL')
                return out
            if generated.startswith('-- 无法回答'):
                out['errors'].append(generated.lstrip('- ').strip() or '该问题无法用当前表结构回答')
                return out
            out['sql'] = generated
            if preview:
                out['ok'] = True
                return out
            try:
                res = _run(generated)
            except Exception as e1:
                logger.debug(f"nlq first attempt failed, retrying with hint: {e1}")
                hint = (f'注意：你上一次生成的 SQL 有问题：{e1}。'
                        f'请检查表名与列名是否逐字来自表结构（真实名称，禁止自造），重新生成。')
                retry = generate_sql(cfg, question, hint=hint)
                if not retry or retry.startswith('-- 无法回答'):
                    out['errors'].append(f'SQL 执行失败：{e1}')
                    return out
                out['sql'] = retry
                try:
                    res = _run(retry)
                except Exception as e2:
                    out['errors'].append(f'SQL 执行失败：{e2}')
                    return out

        cols = res.get('columns') or []
        rows = res.get('rows') or []
        out['columns'] = cols
        out['rows'] = rows
        out['data'] = [dict(zip(cols, r)) for r in rows]
        out['truncated'] = bool(res.get('truncated'))
        out['sql'] = res.get('sql') or out['sql']
        out['ok'] = True
        if not rows:
            out['notice'] = '查询已执行，但没有返回任何记录'
        return out
    except Exception as e:
        out['errors'].append(f'生成或执行失败：{e}')
        return out

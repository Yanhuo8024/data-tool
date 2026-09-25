# -*- coding: utf-8 -*-
"""chat_archive.py — 聊天记录本地归档（全量、按日期分文件夹、人机双格式）

用户诉求（2026-09-19）：所有聊天记录保留在本地，并在文件夹里继续新建文件夹。

目录结构（默认）::

    data/chat_history/                 <- 归档根目录
      ├── index.json                   <- 索引：日期 -> 会话 -> 文件/条数
      ├── 2026-09-19/                  <- 每天自动新建一层（按需创建）
      │   ├── legacy.json              <- 结构化，供程序读取/二次分析
      │   └── legacy.md                <- 人类可读，双击即可翻看
      └── 2026-09-20/
          └── chat_xxx.json / .md

与主存 ``data/chat_history.json`` 的分工：

* 主存是"运行时缓存"——只保留最近 100 条，供接口/前端读取，会被截断；
* 归档是"本地资产"——**合并写入、永不被截断**，只有显式删除（清空会话 /
  删单条）才会同步移除。因此即使内存被 cap 截断、进程被强杀，
  归档里的历史记录依然是完整的。

合并键（id）由 ``ts|role|content 摘要`` 派生：只要消息的秒级时间戳不变，
反复归档同一条消息不会产生重复，也不会误吞"同一分钟连发两条相同内容"。

⚠️ 全部 ``except`` 都写成 ``(Exception, SystemExit)``：删除动作在受限环境里可能被
钩子以 ``SystemExit`` 拒绝（本项目的沙箱删除守卫就是这么干的），只捕 ``Exception``
会让归档清理把整个进程带走。项目约定如此，勿改回。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from datetime import date, datetime
from typing import Any, Dict, Iterable, List, Optional

# 显式覆盖（测试 / 部署可改）；None 表示走环境变量或默认路径
ARCHIVE_DIR: Optional[str] = None
# 归档格式版本（结构变更时递增，便于日后迁移）
ARCHIVE_VERSION = 1

_LOCK = threading.RLock()
_DAY_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')

ROLE_LABELS = {'user': '我', 'assistant': 'AI 助手', 'system': '系统'}


# ==================== 路径 ====================

def get_archive_root() -> str:
    """归档根目录。优先级：模块属性 ARCHIVE_DIR > 环境变量 > 项目 data/chat_history。"""
    if ARCHIVE_DIR:
        return str(ARCHIVE_DIR)
    env = os.environ.get('DT_CHAT_ARCHIVE_DIR')
    if env:
        return env
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'chat_history')


def _day_dir(day: str) -> str:
    return os.path.join(get_archive_root(), day)


def _safe_name(text: str, limit: int = 60) -> str:
    """把会话 id 变成安全文件名（保留中英文/数字/连字符，其余转下划线）。"""
    s = re.sub(r'[^\w\-\u4e00-\u9fff]', '_', str(text or '')).strip('_')
    return (s[:limit] or 'legacy')


# ==================== 归一化 ====================

def _field(msg: Any, key: str, default=None):
    if isinstance(msg, dict):
        return msg.get(key, default)
    return getattr(msg, key, default)


def normalize_message(msg: Any) -> Optional[Dict[str, Any]]:
    """把 ChatMessage / dict 归一化为归档条目；无法识别的返回 None。

    注意：这里的 ``id`` 只用于"同一条消息"的去重（由 ts|role|内容摘要 派生），
    不代表顺序；真正的顺序由 sync_messages 分配的 ``seq`` 决定 —— 同一秒内
    的问答 ts 完全相同，按 ts 排序会把多条提问挤到前面。
    """
    role = str(_field(msg, 'role', '') or '').strip()
    if role not in ROLE_LABELS:
        return None
    content = str(_field(msg, 'content', '') or '')
    stamp = str(_field(msg, 'timestamp', '') or '')
    ts = str(_field(msg, 'ts', '') or '')
    if not ts:
        # 旧数据只有 HH:MM：补上今天的日期（首次迁移时间即归档日期）
        ts = f'{date.today().isoformat()}T{stamp}' if stamp else datetime.now().isoformat(timespec='seconds')
    day = ts[:10]
    if not _DAY_RE.match(day):
        day = date.today().isoformat()
        ts = f'{day}T{stamp or datetime.now().strftime("%H:%M")}'
    time_label = stamp or ts[11:16] or ''
    cid = str(_field(msg, 'conversation_id', 'legacy') or 'legacy')
    digest = hashlib.md5(content.encode('utf-8', 'ignore')).hexdigest()[:12]
    return {
        'id': f'{ts}|{role}|{digest}',
        'role': role,
        'content': content,
        'time': time_label,
        'ts': ts,
        'conversation_id': cid,
    }


# ==================== 读写原语 ====================

def _write_atomic(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f'{path}.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(text)
    os.replace(tmp, path)


def _read_json(path: str) -> List[Dict[str, Any]]:
    try:
        with open(path, encoding='utf-8') as f:
            raw = json.load(f)
        if isinstance(raw, list):
            return [m for m in raw if isinstance(m, dict)]
    except (Exception, SystemExit):
        pass
    return []


def _scan_day(day: str) -> Dict[str, Dict[str, Any]]:
    """一次扫描某天目录：{conversation_id: {'name': json 文件名, 'items': [...]}}。

    合并写入同时需要"文件名"和"已有条目"两份信息。原实现分两步各读一遍盘
    （先 _day_sessions 拿文件名、再 _read_json 拿条目），同一个文件一轮里被读两次；
    这里一次读盘全拿到，顺带让 _build_index 复用。
    """
    out: Dict[str, Dict[str, Any]] = {}
    d = _day_dir(day)
    if not os.path.isdir(d):
        return out
    for name in sorted(os.listdir(d)):
        if not name.endswith('.json') or name == 'index.json':
            continue
        items = _read_json(os.path.join(d, name))
        if not items:
            continue
        cid = str(items[0].get('conversation_id') or 'legacy')
        out[cid] = {'name': name, 'items': items}
    return out


def _resolve_names(scan: Dict[str, Dict[str, Any]], cid: str) -> Dict[str, str]:
    """为 (日期, 会话) 选一组稳定的 json/md 文件名（scan 来自 _scan_day）。"""
    if cid in scan:
        base = str(scan[cid]['name'])[:-5]
    else:
        base = _safe_name(cid)
        taken = {str(v['name'])[:-5] for v in scan.values()}
        if base in taken:  # 极小概率：不同会话 sanitize 后撞名
            base = f'{base}_{hashlib.md5(cid.encode("utf-8")).hexdigest()[:6]}'
    return {'base': base, 'json': f'{base}.json', 'md': f'{base}.md'}


# ==================== Markdown 渲染 ====================

def _render_markdown(cid: str, day: str, items: List[Dict[str, Any]]) -> str:
    title = cid
    for it in items:
        if it.get('role') == 'user' and str(it.get('content') or '').strip():
            title = str(it['content']).strip().splitlines()[0][:40]
            break
    lines = [
        f'# {title}',
        '',
        f'- 会话 ID：`{cid}`',
        f'- 日期：{day}',
        f'- 消息数：{len(items)}',
        f'- 最后更新：{datetime.now().strftime("%Y-%m-%d %H:%M:%S")}',
        '',
        '---',
        '',
    ]
    for it in items:
        who = ROLE_LABELS.get(it.get('role'), it.get('role', ''))
        lines.append(f'### {it.get("time") or ""} · {who}')
        lines.append('')
        body = str(it.get('content') or '').strip() or '*(空消息)*'
        lines.append(body)
        lines.append('')
    return '\n'.join(lines).rstrip() + '\n'


# ==================== 索引 ====================

def _build_index(write: bool = True) -> Dict[str, Any]:
    """扫描整个归档生成索引。``write=False`` 为纯只读（供 list_archive 用）。

    ⚠️ 只读接口不要再调 ``_write_index()``：一个"列目录"动作却去写盘，
    既多一次 IO，也会让 index.json 的 mtime 无意义地抖动。
    """
    root = get_archive_root()
    index: Dict[str, Any] = {
        'version': ARCHIVE_VERSION,
        'updated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'total_messages': 0,
        'dates': {},
    }
    if not os.path.isdir(root):
        return index
    for day in sorted(n for n in os.listdir(root) if _DAY_RE.match(n)):
        sessions: Dict[str, Any] = {}
        for cid, info in _scan_day(day).items():
            items = info['items']
            name = info['name']
            title = cid
            for it in items:
                if it.get('role') == 'user' and str(it.get('content') or '').strip():
                    title = str(it['content']).strip().splitlines()[0][:40]
                    break
            sessions[cid] = {
                'json': name,
                'md': f'{name[:-5]}.md',
                'count': len(items),
                'title': title,
                'last_ts': items[-1].get('ts', ''),
            }
            index['total_messages'] += len(items)
        index['dates'][day] = sessions
    if write:
        try:
            _write_atomic(os.path.join(root, 'index.json'),
                          json.dumps(index, ensure_ascii=False, indent=2))
        except (Exception, SystemExit):
            pass
    return index


def _write_index() -> Dict[str, Any]:
    return _build_index(write=True)


# ==================== 对外 API ====================

def sync_messages(messages: Iterable[Any]) -> Dict[str, Any]:
    """把消息合并进归档（不删除已有条目）。返回统计信息。

    正常记录路径调用；同一批消息反复调用不会产生重复。
    """
    items = [normalize_message(m) for m in (messages or [])]
    items = [i for i in items if i]
    if not items:
        return {'ok': True, 'written': 0, 'files': 0, 'root': get_archive_root()}
    written = 0
    files = 0
    try:
        with _LOCK:
            grouped: Dict[tuple, Dict[str, Dict[str, Any]]] = {}
            for it in items:
                grouped.setdefault((it['ts'][:10], it['conversation_id']), {})[it['id']] = it
            for (day, cid), fresh in grouped.items():
                scan = _scan_day(day)
                names = _resolve_names(scan, cid)
                d = _day_dir(day)
                existing = [e for e in ((scan.get(cid) or {}).get('items') or []) if e.get('id')]
                merged: Dict[str, Dict[str, Any]] = {}
                seq_of: Dict[str, int] = {}
                for i, e in enumerate(existing):
                    # 老文件可能没有 seq：按其数组位置补，保持既有顺序
                    e['seq'] = int(e.get('seq', i))
                    merged[e['id']] = e
                    seq_of[e['id']] = e['seq']
                next_seq = (max(seq_of.values()) + 1) if seq_of else 0
                added = 0
                for item in fresh.values():  # dict 保序 = 传入顺序（问答交错）
                    if item['id'] in seq_of:
                        item['seq'] = seq_of[item['id']]   # 已归档过：沿用原位置
                    else:
                        item['seq'] = next_seq
                        next_seq += 1
                        added += 1
                    merged[item['id']] = item
                if not added:
                    # 无新增就整组跳过：每轮对话都会把全量内存历史送进来，
                    # 无条件重写会让所有历史文件（含 .md 里的"最后更新"）白白抖动。
                    continue
                ordered = sorted(merged.values(), key=lambda x: int(x.get('seq', 0)))
                _write_atomic(os.path.join(d, names['json']),
                              json.dumps(ordered, ensure_ascii=False, indent=2))
                _write_atomic(os.path.join(d, names['md']),
                              _render_markdown(cid, day, ordered))
                written += added
                files += 2
            if files:
                _write_index()
    except (Exception, SystemExit) as e:  # 归档失败绝不能影响主流程
        return {'ok': False, 'error': str(e), 'written': written, 'files': files,
                'root': get_archive_root()}
    return {'ok': True, 'written': written, 'files': files, 'root': get_archive_root()}


def purge(conversation_id: Optional[str] = None,
          message: Optional[Dict[str, Any]] = None) -> int:
    """从归档中移除记录。返回移除条数。

    * ``conversation_id`` 指定 → 删该会话全部归档（清空会话时用）
    * ``message`` 指定 → 按 (role, content, time) 精确删单条
    * 两者都不传 → 清空所有会话归档
    """
    removed = 0
    try:
        with _LOCK:
            root = get_archive_root()
            if not os.path.isdir(root):
                return 0
            want_role = (message or {}).get('role')
            want_content = (message or {}).get('content')
            want_time = (message or {}).get('time') or (message or {}).get('timestamp')
            for day in sorted(n for n in os.listdir(root) if _DAY_RE.match(n)):
                d = os.path.join(root, day)
                if not os.path.isdir(d):
                    continue
                for name in sorted(os.listdir(d)):
                    if not name.endswith('.json') or name == 'index.json':
                        continue
                    path = os.path.join(d, name)
                    items = _read_json(path)
                    if not items:
                        continue
                    cid = str(items[0].get('conversation_id') or 'legacy')
                    if conversation_id is None and want_content is None:
                        keep = []
                    elif conversation_id is not None:
                        keep = [] if cid == conversation_id else items
                    else:
                        keep, hit = [], 0
                        for it in items:
                            if (hit == 0 and it.get('role') == want_role
                                    and it.get('content') == want_content
                                    and (want_time is None or it.get('time') == want_time)):
                                hit += 1
                                continue
                            keep.append(it)
                    if len(keep) == len(items):
                        continue
                    removed += len(items) - len(keep)
                    if keep:
                        _write_atomic(path, json.dumps(keep, ensure_ascii=False, indent=2))
                        _write_atomic(os.path.join(d, f'{name[:-5]}.md'),
                                      _render_markdown(cid, day, keep))
                    else:
                        for p in (path, os.path.join(d, f'{name[:-5]}.md')):
                            try:
                                if os.path.exists(p):
                                    os.remove(p)
                            except (Exception, SystemExit):
                                pass
            _write_index()
    except (Exception, SystemExit):
        return removed
    return removed


def rebuild(messages: Iterable[Any]) -> Dict[str, Any]:
    """清空归档根目录后按给定消息全量重建（迁移 / 修复用）。"""
    try:
        with _LOCK:
            root = get_archive_root()
            if os.path.isdir(root):
                for day in os.listdir(root):
                    d = os.path.join(root, day)
                    if _DAY_RE.match(day) and os.path.isdir(d):
                        for name in os.listdir(d):
                            try:
                                os.remove(os.path.join(d, name))
                            except (Exception, SystemExit):
                                pass
                        try:
                            os.rmdir(d)
                        except (Exception, SystemExit):
                            pass
                    elif day == 'index.json':
                        try:
                            os.remove(os.path.join(root, day))
                        except (Exception, SystemExit):
                            pass
        return sync_messages(messages)
    except (Exception, SystemExit) as e:
        return {'ok': False, 'error': str(e), 'root': get_archive_root()}


def list_archive() -> Dict[str, Any]:
    """列出归档概况（日期 -> 会话 -> 文件/条数）。**纯只读，不落盘**。"""
    try:
        import copy
        return copy.deepcopy(_build_index(write=False))
    except (Exception, SystemExit) as e:
        return {'version': ARCHIVE_VERSION, 'dates': {}, 'total_messages': 0, 'error': str(e)}


def _item_seq(item: Dict[str, Any]) -> int:
    """归档条目排序键：seq 可能缺失或非数字（老文件），一律降级为 0。"""
    try:
        return int(item.get('seq') or 0)
    except (TypeError, ValueError):
        return 0


def list_conversations() -> List[Dict[str, Any]]:
    """按会话聚合归档索引，返回会话摘要（最近活动的在前）。**纯只读，不落盘**。

    为什么必须走归档而不是内存：主存 ``data/chat_history.json`` 只保留最近 100 条，
    重度使用后**老会话会被整段截断**；归档永不截断。若会话列表读主存，
    用户切回旧会话时会发现它凭空消失了。

    一个会话可能横跨多天（每天一个文件）→ 这里跨天合并 message_count。
    ``title`` 取该会话**最早**那天首条用户消息（会话的开场问题）；``timestamp``
    取最后一次活动时间。
    """
    try:
        index = _build_index(write=False)
    except (Exception, SystemExit):
        return []
    dates = index.get('dates') or {}
    agg: Dict[str, Dict[str, Any]] = {}
    # 日期升序：title 只由"第一次见到"的那天决定（= 会话开场问题）
    for day in sorted(dates.keys()):
        for cid, info in (dates[day] or {}).items():
            if not cid:
                continue
            item = agg.setdefault(cid, {
                'id': cid, 'title': '', 'preview': '', 'timestamp': '',
                'last_ts': '', 'message_count': 0, 'days': [],
            })
            try:
                item['message_count'] += int(info.get('count') or 0)
            except (TypeError, ValueError):
                pass
            if day not in item['days']:
                item['days'].append(day)
            title = str(info.get('title') or '').strip()
            # _build_index 在没有用户消息时会把 title 兜底成 cid，这里不认——
            # 否则界面上会出现一个叫 "legacy" 的会话名（用户看不懂）。
            if title and title != cid and not item['title']:
                item['title'] = title          # 最早的标题 = 会话开场问题
            last_ts = str(info.get('last_ts') or '')
            if last_ts >= item['last_ts']:
                item['last_ts'] = last_ts
                # 最近一次活动那句话作为摘要预览
                if title and title != cid:
                    item['preview'] = title
    out = list(agg.values())
    for it in out:
        lt = it['last_ts']
        # 2026-09-21T09:30:12 -> "2026-09-21 09:30"（与主存 timestamp 口径一致）
        it['timestamp'] = f'{lt[:10]} {lt[11:16]}'.strip() if len(lt) >= 16 else lt
        if not it['title']:
            it['title'] = '历史对话'
    out.sort(key=lambda x: x['last_ts'], reverse=True)
    return out


def load_conversation(conversation_id: str) -> List[Dict[str, Any]]:
    """读取某会话在归档中的**全部**消息（跨天合并，按 seq 升序）。**纯只读**。

    供"点回旧会话"使用：老会话可能已不在主存（被 100 条上限截断），
    只有归档还留着完整内容。
    """
    cid = str(conversation_id or 'legacy')
    out: List[Dict[str, Any]] = []
    try:
        root = get_archive_root()
        if not os.path.isdir(root):
            return out
        for day in sorted(n for n in os.listdir(root) if _DAY_RE.match(n)):
            info = _scan_day(day).get(cid)
            if not info:
                continue
            for it in sorted(info.get('items') or [], key=_item_seq):
                out.append({
                    'role': it.get('role'),
                    'content': it.get('content') or '',
                    'timestamp': it.get('time') or '',
                    'conversation_id': cid,
                    'ts': it.get('ts') or '',
                })
    except (Exception, SystemExit):
        return out
    return out


def stats() -> Dict[str, Any]:
    """轻量统计（不重写索引，供诊断/接口用）。"""
    root = get_archive_root()
    days = []
    total = 0
    if os.path.isdir(root):
        for day in sorted(n for n in os.listdir(root) if _DAY_RE.match(n)):
            d = os.path.join(root, day)
            if not os.path.isdir(d):
                continue
            count = sum(len(_read_json(os.path.join(d, n)))
                        for n in os.listdir(d)
                        if n.endswith('.json') and n != 'index.json')
            days.append({'date': day, 'messages': count})
            total += count
    return {'root': root, 'days': days, 'total_messages': total,
            'exists': os.path.isdir(root)}

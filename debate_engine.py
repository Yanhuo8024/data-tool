#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
debate_engine.py - 对抗式多 Agent 辩论引擎（状态图可持久化版本）
P2 升级：将顺序辩论升级为可持久化状态图，支持：
- 可暂停：任意节点中断后保存状态，后续可恢复继续执行
- 可分支：对争议点可以并行多路径探索（多个不同的修正方向）
- 可并行复核：skeptic/domain_expert 可并行多轮质疑/修正
- 会话可恢复：全流程状态 checkpoint 落盘 JSON
- 兼容原有 API：保持 debate_analysis 接口不变（全流程单次执行）

工作流状态机：
  START → [ANALYST] → SKEPTIC → [DOMAIN_EXPERT] → SYNTHESIZER → END
  * SKEPTIC → 可分支到多个 DOMAIN_EXPERT（并行复核争议点）
  * 任意状态 → PAUSED（保存 checkpoint）→ 从 checkpoint RESUME
  * 支持初始分析直接复用（深度分析失败后的二次复核模式）
"""

import os
import json
import time
import logging
from pathlib import Path
from typing import Dict, Any, Optional, List, Tuple, Union
from dataclasses import dataclass, field, asdict

logger = logging.getLogger(__name__)

# =============================================================================
# 状态图数据结构（可持久化 JSON）
# =============================================================================

@dataclass
class DebateNode:
    """辩论图一个节点：某个角色在某一步的输出"""
    node_id: str          # 唯一ID: "role-step-branch"
    role: str             # analyst / skeptic / domain_expert / synthesizer
    parent_ids: List[str] # 父节点（输入来源）
    content: str = ""     # 输出内容
    created_at: float = 0.0
    completed: bool = False
    metadata: Dict[str, Any] = field(default_factory=dict)

@dataclass
class DebateGraph:
    """完整辩论状态图（可持久化）"""
    graph_id: str           # 唯一ID（含会话前缀）
    message: str            # 用户原始问题
    file_name: Optional[str] # 数据文件（若有）
    llm_config: Dict[str, Any] # LLM 配置
    initial_analysis: Optional[str] # 前置分析（若复用已有结论）
    nodes: Dict[str, DebateNode] = field(default_factory=dict)
    current_nodes: List[str] = field(default_factory=list) # 当前待处理节点
    status: str = "running" # running / paused / completed / failed
    created_at: float = 0.0
    updated_at: float = 0.0
    max_tokens_per_role: int = 700
    usage_total: Dict[str, int] = field(default_factory=lambda: {
        "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0
    })

    def to_dict(self) -> Dict:
        return {
            "graph_id": self.graph_id,
            "message": self.message,
            "file_name": self.file_name,
            "llm_config": self.llm_config,
            "initial_analysis": self.initial_analysis,
            "nodes": {k: asdict(v) for k, v in self.nodes.items()},
            "current_nodes": self.current_nodes,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "max_tokens_per_role": self.max_tokens_per_role,
            "usage_total": self.usage_total,
        }

    @classmethod
    def from_dict(cls, data: Dict) -> "DebateGraph":
        g = cls(
            graph_id=data["graph_id"],
            message=data["message"],
            file_name=data.get("file_name"),
            llm_config=data.get("llm_config", {}),
            initial_analysis=data.get("initial_analysis"),
            max_tokens_per_role=data.get("max_tokens_per_role", 700),
        )
        g.status = data.get("status", "running")
        g.created_at = data.get("created_at", 0.0)
        g.updated_at = data.get("updated_at", 0.0)
        g.usage_total = data.get("usage_total", g.usage_total)
        g.current_nodes = data.get("current_nodes", [])
        for node_id, nd in data.get("nodes", {}).items():
            # 缺省补齐关键字段，容忍旧版本/外部改动导致的字段缺失，避免整图恢复失败
            g.nodes[node_id] = DebateNode(
                node_id=nd.get("node_id", node_id),
                role=nd.get("role", ""),
                parent_ids=nd.get("parent_ids", []),
                content=nd.get("content", ""),
                created_at=nd.get("created_at", 0.0),
                completed=nd.get("completed", False),
                metadata=nd.get("metadata", {}) or {},
            )
        return g

AGENT_ROLES = {
    "analyst": "你是一位客观严谨的数据分析师。请基于给定的数据与用户问题，客观描述数据与统计特征，先给关键事实和数字，不要下绝对化的投资/经营结论。",
    "skeptic": "你是一位风控审查员/魔鬼代言人。请对上一轮分析师的结论提出至少 3 点质疑：假设是否成立、样本是否有偏、是否有幸存者偏差、把相关性当因果了吗、极端值是否被忽略、结论是否过度外推。",
    "domain_expert": "你是一位业务专家（根据场景表现为量化研究员/固收/风控/计量经济学家）。请结合业务背景，修正或补充分析师结论中被质疑的地方，给出专业视角，并指出关键风险点。",
    "synthesizer": "你是一位首席综合分析师。请综合前面所有节点的观点，输出最终结论。必须包含：1) 结论与依据（关键数字加粗）；2) 存在的争议点及不同意见；3) 数据局限与后续建议。语气专业、中文、条理清晰但不要八股套话。",
}

# =============================================================================
# 持久化存储（checkpoint 落盘）
# =============================================================================

def _debate_dir() -> Path:
    """辩论状态图存储目录"""
    try:
        from core_config import OUT_DIR
        base = OUT_DIR
    except Exception:
        base = Path(__file__).parent / "output"
    d = base / "debate"
    d.mkdir(parents=True, exist_ok=True)
    return d

def _new_graph_id(session_id: str = None) -> str:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    rand = os.urandom(2).hex()
    slug = ""
    if session_id:
        import re
        slug = re.sub(r"[^\w\-]", "", str(session_id))[-10:] + "-"
    return f"{stamp}-{slug}{rand}"

def save_graph(graph: DebateGraph) -> bool:
    """保存状态图到持久化存储"""
    path = _debate_dir() / f"{graph.graph_id}.json"
    try:
        graph.updated_at = time.time()
        data = graph.to_dict()
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        logger.warning("保存辩论状态失败: %s", e)
        return False

def load_graph(graph_id: str) -> Optional[DebateGraph]:
    """从持久化存储恢复状态图"""
    path = _debate_dir() / f"{graph_id}.json"
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return DebateGraph.from_dict(data)
    except Exception as e:
        logger.warning("加载辩论状态失败: %s", e)
        return None

def list_graphs(limit: int = 20) -> List[Dict[str, Any]]:
    """列出最近的辩论会话"""
    d = _debate_dir()
    items = []
    try:
        for f in sorted(d.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True)[:limit]:
            meta = {
                "graph_id": f.stem,
                "mtime": f.stat().st_mtime,
            }
            try:
                g = load_graph(f.stem)
                if g:
                    meta["message"] = g.message[:80]
                    meta["status"] = g.status
                    meta["num_nodes"] = len(g.nodes)
            except Exception:
                pass
            items.append(meta)
    except Exception:
        pass
    return items

def delete_graph(graph_id: str) -> bool:
    """删除辩论会话"""
    path = _debate_dir() / f"{graph_id}.json"
    try:
        if path.exists():
            path.unlink()
            return True
    except Exception:
        pass
    return False

# =============================================================================
# 状态图执行引擎（步进 / 暂停 / 恢复 / 分支）
# =============================================================================

def create_debate_graph(
    message: str,
    llm_config: dict,
    file_name: str = None,
    initial_analysis: str = None,
    session_id: str = None,
    max_tokens_per_role: int = 700,
) -> DebateGraph:
    """创建一个新的辩论状态图，初始化但不完整执行（可步进执行支持暂停）"""
    g = DebateGraph(
        graph_id=_new_graph_id(session_id),
        message=message,
        file_name=file_name,
        llm_config=llm_config,
        initial_analysis=initial_analysis,
        max_tokens_per_role=max_tokens_per_role,
        created_at=time.time(),
        updated_at=time.time(),
    )
    # 若有初始分析，直接跳过 analyst，从 skeptic 开始
    if initial_analysis and initial_analysis.strip():
        node = DebateNode(
            node_id="analyst-0-0",
            role="analyst",
            parent_ids=[],
            content=initial_analysis.strip()[:3000],
            completed=True,
            created_at=time.time(),
        )
        g.nodes[node.node_id] = node
        # skeptic 作为下一个待处理节点（继承 analyst 为父节点）
        if "skeptic-0-0" not in g.nodes:
            g.nodes["skeptic-0-0"] = DebateNode(node_id="skeptic-0-0", role="skeptic",
                                                parent_ids=[node.node_id], created_at=time.time())
        g.current_nodes = ["skeptic-0-0"]
    else:
        # 从 analyst 开始
        g.current_nodes = ["analyst-0-0"]
    g.status = "running"
    save_graph(g)
    return g

def _compact_data_summary(file_name: str) -> str:
    """生成数据的紧凑数值摘要，供各 Agent 使用（失败返回空）"""
    if not file_name:
        return "（无数据文件，基于问题与通用知识回答）"
    try:
        from core_config import load_dataframe, OUT_DIR
        from pathlib import Path
        df = load_dataframe(str(OUT_DIR / file_name))
        if df is None or df.empty:
            return f"（数据文件 {file_name} 读取失败或为空）"
        num = df.select_dtypes('number')
        lines = [f"数据: {file_name}，{df.shape[0]} 行 × {df.shape[1]} 列"]
        if not num.empty:
            stats = num.describe().T
            for col in stats.index:
                row = stats.loc[col]
                lines.append(f"{col}: mean={row['mean']:.4g} std={row['std']:.4g} min={row['min']:.4g} max={row['max']:.4g} count={int(row['count'])}")
        return "\n".join(lines[:40])
    except Exception as e:
        logger.warning("辩论引擎数据摘要失败: %s", e)
        return f"（无法读取数据摘要: {e}）"

def _call_role(
    graph: DebateGraph,
    node_id: str,
    extra_context: str = "",
    temperature: float = None,
) -> Tuple[str, Dict[str, int]]:
    """调用 LLM 完成角色节点"""
    from llm_client import chat
    role = node_id.split("-")[0]
    sys_role = AGENT_ROLES[role]
    if extra_context:
        sys_role = sys_role + "\n\n参考上一轮内容：\n" + extra_context
    data_ctx = _compact_data_summary(graph.file_name)
    user_q = f"用户问题：{graph.message}\n\n数据摘要：\n{data_ctx}"
    role_cfg = dict(graph.llm_config or {})
    if temperature is not None:
        role_cfg["temperature"] = temperature
    u_out = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    try:
        resp = chat(
            [{"role": "system", "content": sys_role},
             {"role": "user", "content": user_q}],
            role_cfg, max_tokens=graph.max_tokens_per_role,
        )
        usage = resp.get("usage") if isinstance(resp, dict) else None
        if usage:
            for k in u_out:
                u_out[k] = int(usage.get(k, 0))
        content = (resp.get("content") or "").strip() if isinstance(resp, dict) else str(resp).strip()
        return content, u_out
    except Exception as e:
        logger.warning("辩论 %s 轮失败: %s", role, e)
        return "", u_out

def _schedule_successors(graph: DebateGraph, node: DebateNode, role: str,
                         branch: bool, next_nodes: List[str]) -> None:
    """一个节点完成后，按角色调度后继节点（analyst→skeptic→domain_expert 分支→synthesizer）。
    branch=True 时把 skeptic 的多个质疑点拆分为多个 domain_expert 分支（并行复核）。"""
    if role == "analyst":
        next_id = "skeptic-0-0"
        if next_id not in graph.nodes:
            graph.nodes[next_id] = DebateNode(node_id=next_id, role="skeptic",
                                              parent_ids=[node.node_id], created_at=time.time())
        next_nodes.append(next_id)
    elif role == "skeptic" and branch:
        import re
        # 以「序号/项目符号 + 分隔标点」作为条目起点拆分，把多个质疑点拆成独立分支
        items = re.split(r'(?m)^\s*[\d•\-*]+[\.、．)）:：]\s*', node.content)
        points = [p.strip() for p in items if p and p.strip()]
        if not points:
            points = [node.content]
        for i, point in enumerate(points[:5]):  # 最多 5 分支避免成本爆炸
            bid = f"domain_expert-0-{i}"
            if bid not in graph.nodes:
                graph.nodes[bid] = DebateNode(node_id=bid, role="domain_expert",
                                              parent_ids=[node.node_id], created_at=time.time())
            graph.nodes[bid].metadata["focus"] = point
            next_nodes.append(bid)
    elif role == "skeptic":
        nid = "domain_expert-0-0"
        if nid not in graph.nodes:
            graph.nodes[nid] = DebateNode(node_id=nid, role="domain_expert",
                                          parent_ids=[node.node_id], created_at=time.time())
        next_nodes.append(nid)
    # domain_expert / synthesizer：无显式后继（由 synthesizer 统一汇总或直接结束）

def step_graph(graph: DebateGraph, branch: bool = False) -> bool:
    """执行一步：处理当前所有待处理节点，推进状态图。
    若 branch=True，对 skeptic 输出拆分为多质疑点分支到多个 domain_expert 节点（并行复核）。
    返回 True 表示执行未结束（还有节点或还需创建 synthesizer）；False 表示流程已完成。"""
    if graph.status != "running":
        return False

    # 处理当前所有待处理节点
    next_nodes = []
    for node_id in list(graph.current_nodes):
        if node_id in graph.nodes and graph.nodes[node_id].completed:
            # 已完成节点：按其 role 调度后继（与未完成分支共用调度逻辑）
            node = graph.nodes[node_id]
            role = node_id.split("-")[0]
            _schedule_successors(graph, node, role, branch, next_nodes)
            continue
        role = node_id.split("-")[0]
        if node_id not in graph.nodes:
            graph.nodes[node_id] = DebateNode(node_id=node_id, role=role,
                                              parent_ids=[], created_at=time.time())
        node = graph.nodes[node_id]

        # 构造上下文：聚合所有父节点内容
        parent_content = []
        for p_id in node.parent_ids:
            pn = graph.nodes.get(p_id)
            if pn and pn.completed:
                header = f"{pn.role.capitalize()}：\n"
                parent_content.append(header + pn.content[:1200])
        context = "\n\n".join(parent_content)

        # 根据角色选温度
        temp_map = {
            "analyst": 0.2, "skeptic": 0.8, "domain_expert": 0.4, "synthesizer": 0.2
        }
        content, usage = _call_role(graph, node_id, context, temp_map.get(role))
        # 累计用量
        for k in graph.usage_total:
            graph.usage_total[k] += usage.get(k, 0)

        if not content:
            # 调用失败，保留在当前节点重试（供再次 step 重试）
            logger.warning("%s 节点调用失败，等待重试", node_id)
            next_nodes.append(node_id)
            continue

        node.content = content
        node.completed = True
        node.created_at = time.time()
        _schedule_successors(graph, node, role, branch, next_nodes)

    graph.current_nodes = next_nodes
    save_graph(graph)
    graph.updated_at = time.time()

    # 中央完成判定：无待处理节点时，
    #   - 已存在完成的 synthesizer → 整体完成；
    #   - 否则创建 synthesizer 节点并继续。
    if not graph.current_nodes:
        synth_done = any(n.role == "synthesizer" and n.completed for n in graph.nodes.values())
        if synth_done:
            graph.status = "completed"
            save_graph(graph)
            return False
        # 创建 synthesizer：汇总全部已完成的分析/质疑/专家观点
        all_content = []
        for node in graph.nodes.values():
            if node.role in ("analyst", "skeptic", "domain_expert") and node.completed:
                all_content.append(f"{node.role.capitalize()}观点：\n" + node.content[:1500])
        synth_id = "synthesizer-0-0"
        if synth_id not in graph.nodes:
            graph.nodes[synth_id] = DebateNode(
                node_id=synth_id, role="synthesizer",
                parent_ids=list(graph.nodes.keys()), created_at=time.time())
        graph.current_nodes = [synth_id]
        # 汇总内容作为 synthesizer 的输入上下文
        node = graph.nodes[synth_id]
        if not node.completed:
            synth_content, synth_usage = _call_role(
                graph, synth_id, "\n\n".join(all_content),
                0.2)
            for k in graph.usage_total:
                graph.usage_total[k] += synth_usage.get(k, 0)
            if synth_content:
                node.content = synth_content
                node.completed = True
                node.created_at = time.time()
            else:
                # 合成失败：用最近一次 completed 的分析观点兜底，保证有输出
                fallback = next((n.content for n in reversed(list(graph.nodes.values()))
                                 if n.completed and n.role in ("domain_expert", "skeptic", "analyst")), "")
                node.content = fallback or "（合成失败，无可用结论）"
                node.completed = True
        graph.current_nodes = []
        graph.status = "completed"
        save_graph(graph)
        return False

    return True

def pause_graph(graph: DebateGraph) -> bool:
    """暂停执行：保存当前状态，后续可恢复。API 使用者在 step 后按需调用。"""
    if graph.status == "running":
        graph.status = "paused"
        save_graph(graph)
        return True
    return False

def resume_graph(graph: DebateGraph) -> bool:
    """从暂停状态恢复执行"""
    if graph.status == "paused":
        graph.status = "running"
        save_graph(graph)
        return True
    return False

def collect_result(graph: DebateGraph) -> Optional[Dict[str, Any]]:
    """从完成的状态图收集结果（兼容原有 API 返回格式）"""
    if graph.status != "completed":
        return None
    # 找到 synthesizer 节点
    synth_nodes = [n for n in graph.nodes.values() if n.role == "synthesizer" and n.completed]
    if not synth_nodes:
        return None
    synth = sorted(synth_nodes, key=lambda x: x.created_at)[-1]
    rounds = []
    for n in graph.nodes.values():
        if n.completed:
            rounds.append({"role": n.role, "content": n.content, "source": f"state-graph:{n.node_id}"})
    return {
        "ok": True,
        "reply": synth.content,
        "rounds": rounds,
        "usage": graph.usage_total,
        "source": "debate",
        "graph_id": graph.graph_id,
        "graph_status": graph.status,
    }

# =============================================================================
# 兼容原 API：完整执行一次辩论（全流程一步到位，保持向后兼容）
# =============================================================================

def debate_analysis(message: str, llm_config: dict, file_name: str = None,
                    max_tokens_per_role: int = 700,
                    initial_analysis: str = None,
                    session_id: str = None,
                    parallel_branching: bool = True) -> Optional[Dict[str, Any]]:
    """
    对抗式多 Agent 辩论（兼容原接口）。
    新增参数：
      initial_analysis: 深度分析失败后的评审模式（复用已有结论，跳过重复生成 analyst）
      session_id: 绑定会话 ID，持久化状态图支持断点恢复
      parallel_branching: P2 增强，对 skeptic 质疑拆分为多分支并行复核
    """
    g = create_debate_graph(message, llm_config, file_name, initial_analysis,
                            session_id, max_tokens_per_role)
    # 全流程步进执行
    has_more = True
    while has_more:
        try:
            has_more = step_graph(g, branch=parallel_branching)
        except Exception as e:
            logger.error("辩论步进异常: %s", e)
            g.status = "failed"
            save_graph(g)
            return None
    return collect_result(g)

# =============================================================================
# 触发检测（保持原有接口）
# =============================================================================

DEBATE_TRIGGER_KW = ("辩论", "多角度", "正方反方", "不同观点", "不同意见", "多方讨论", "争议", "挑毛病")

def wants_debate(message: str) -> bool:
    low = message.lower()
    return any(k in low for k in DEBATE_TRIGGER_KW)

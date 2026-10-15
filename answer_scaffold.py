# ==================== 回答脚手架剥离（Scaffold Stripping）====================
# 故障现象（2026-10-06 实测，江西铜业 600362 估值问答）：
#   用户看到的回答长这样 ——
#       用户希望我分析江西铜业（600362）的财务报表，并基于提供的实时数据…
#       数据概览：- DCF估值：…  - 财务比率：…
#       要求：1. 先给结论…  3. 结尾必须注明…
#       分析：结论：高估。依据1：…            ← 模型的内部草稿
#       草稿：江西铜业当前估值**高估**。…      ← 真正的最终稿
#       3. **技术面走弱**：近一月下跌13.64%，股价   ← 被 max_tokens 截断
#   即：**任务复述 + 内部草稿 + 最终稿 + 截断**四件事叠在一起。
#
# 两个独立的成因（必须都修，只修一个都还会复现）：
#   ① 提示词未禁止输出思考过程 → reasoning 系模型（glm-5.2 等）把草稿写进 content；
#   ② max_tokens 预算太小 → 正文被挤掉后半截。
#   本模块只负责兜底剥离「脚手架」，治本的那部分在提示词与预算（见调用点）。
#
# 设计原则：**保守**。只在明确命中结构标记时才动手，其余原样返回 ——
# 宁可漏剥（顶多不好看），不可误伤正常回答（那是把好答案改坏）。
# 本模块零项目依赖（只用标准库 re），因此不会触发 R4 循环 import。

import re

# 金融实时短答的输出纪律 + 预算（原先分散在 ai_assistant 的两处提示词里，
# 同步/流式各写一遍极易漂移 —— 本次漂移出的正是"思考混进正文"和"预算截断"两处）。
FINANCE_ANSWER_RULE = (
    "\n【输出纪律】只输出最终答案本身：不要复述任务、不要输出'分析：''草稿：'等中间过程、"
    "不要复述数据概览与要求清单、不要写'字数控制在…'之类的自我提醒。"
    "直接给结论与依据。"
)
# 200 字中文正文 + Markdown 标记 + 免责声明约需 400~600 token；
# 旧值 500/600 在模型啰嗦时会把正文截断（实测用户看到回答断在半句"股价"）。
FINANCE_MAX_TOKENS = 1200

# 「最终稿」分隔标记 —— 命中即只保留其后内容。
# 取最后一个出现位置：模型可能先写"草稿："又自我修正写"最终稿："。
_FINAL_MARKERS = ('最终答案：', '最终答案:', '最终稿：', '最终稿:',
                  '正式回答：', '正式回答:', '草稿：', '草稿:')

# 「内部草稿」起始标记 —— 仅在没有最终稿标记时，用它切掉前半段复述。
_DRAFT_MARKERS = ('分析：', '分析:', '推理：', '推理:',
                  '思考过程：', '思考过程:', '思路：', '思路:')

# 「任务复述」开头特征 —— 正文以这些开头时，前面几段都是复述系统提示词。
_ECHO_HEADS = ('用户希望我', '用户询问', '用户想了解', '用户想要', '用户提出',
               '用户的问题', '用户请求', '根据用户')

# 任务复述里的清单/小节标记，用来定位复述段的结束位置。
_ECHO_BODY = ('数据概览', '数据摘要', '要求：', '要求:', '任务要求',
              '实时数据：', '实时数据:', '输出要求', '回答要求')

# 剥完前缀若为空说明只剩标记没有正文，此时保留原文（别把内容剥没了）。


def _last_index(text, markers):
    pos = -1
    for m in markers:
        i = text.rfind(m)
        if i > pos:
            pos = i
    return pos


def _first_index(text, markers):
    """返回最早命中的标记**结束位置**（跳过整个标记，调用方直接取其后正文）。"""
    best = -1
    for m in markers:
        i = text.find(m)
        if i >= 0 and (best < 0 or i < best):
            best = i + len(m)
    return best


def strip_answer_scaffold(text):
    """剥离回答里的「任务复述 + 内部草稿」，只保留面向用户的最终稿。

    返回剥离后的文本；未识别到脚手架时**原样返回**（不做任何改写）。
    """
    if not text:
        return text
    s = str(text)
    if '<' in s and '>' in s:
        # 含标签的交给 strip_thinking 处理，这里不掺和
        return s

    # ① 有「最终稿」标记 → 只取最后一个标记之后的内容
    pos, cut = -1, ''
    for m in _FINAL_MARKERS:
        i = s.rfind(m)
        if i > pos:
            pos, cut = i, m
    if pos >= 0:
        tail = s[pos + len(cut):].strip()
        if tail:
            return tail

    # ② 无最终稿标记，但开头是任务复述 + 中间有「分析：」→ 只取草稿段
    head = s[:400]
    if not any(h in head for h in _ECHO_HEADS):
        return s
    d = _first_index(s, _DRAFT_MARKERS)
    if d < 0:
        d = _first_index(s, _ECHO_BODY)
    if d <= 0:
        return s
    tail = s[d:].strip().lstrip('：:').strip()
    # 剥完只剩标点/空白 = 没剥到正文，比不剥更糟 → 保留原文
    if not re.sub(r'[\s：:\-*#]', '', tail):
        return s
    return tail


def scaffold_report(text):
    """诊断用：返回命中的脚手架类型（无则空串）。不参与业务判断。"""
    if not text:
        return ''
    s = str(text)
    if _last_index(s, _FINAL_MARKERS) >= 0:
        return 'final_marker'
    if any(h in s[:400] for h in _ECHO_HEADS) and _first_index(s, _DRAFT_MARKERS) > 0:
        return 'echo_head+draft'
    return ''


def clean_reply(text):
    """调用点唯一入口：先剥 <thinking> 标签，再剥回答脚手架。"""
    from text_clean import strip_thinking
    return strip_answer_scaffold(strip_thinking(text))

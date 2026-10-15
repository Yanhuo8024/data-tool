# -*- coding: utf-8 -*-
"""风控多信号识别 + 未上市公司三件套 + 复核队列新类别。"""

import pytest

import evidence_grade as eg
import review_queue as rq
import risk_checks_finance as rf
import unlisted_company as uc


# ---------- 风险段落：多信号识别（第二次误报后重写） ----------

class Test风险段落多信号:

    def _long(self, title):
        return (f'# 研究报告\n\n## 一、行业\n竞争激烈。\n\n## 二、估值\n对标同业。\n\n'
                f'## 三、{title}\n1. 竞争加剧；2. 政策变化；3. 算力依赖。\n\n'
                f'## 免责声明\n仅供参考。\n') + '正文补充。' * 40

    @pytest.mark.parametrize('title', ['风险提示', '风险与催化', '风险因素',
                                       '风险挑战', '主要风险与应对'])
    def test_各类风险标题都识别(self, title):
        present, _ = rf.has_risk_section(self._long(title))
        assert present, title
        assert rf.check_missing_risk_section(self._long(title)) is None

    def test_序号标题含风险也识别(self):
        body = ('# 报告\n\n## 一、行业\n竞争激烈。\n\n四、风险与催化\n商业化不及预期。\n\n'
                ) + '正文。' * 60
        present, hit = rf.has_risk_section(body)
        assert present and '标题级' in hit

    def test_无标题但有风险论述按密度识别(self):
        """内容级兜底：信号词 ≥3 次即认有风险论述（不依赖标题）。"""
        body = ('# 报告\n\n公司面临监管压力，行业竞争加剧，需求存在不确定性。\n\n'
                ) + '正文。' * 60
        present, hit = rf.has_risk_section(body)
        assert present and '内容级' in hit

    def test_智谱报告现场不再误报(self):
        """用户实测第二次误报的现场：报告有「四、风险与催化」。"""
        report = ('# 智谱 AI 研究报告\n\n## 一、行业格局\n大模型行业竞争加剧。\n\n'
                  '## 二、商业模式\nTo B 与 To C 双轮驱动。\n\n'
                  '## 四、风险与催化\n1. 行业竞争加剧，商业化不及预期；\n'
                  '2. 政策变化带来的合规压力；\n3. 算力依赖与断供风险。\n\n'
                  '## 免责声明\n本报告仅供参考。\n') + '内容补足。' * 60
        assert rf.check_missing_risk_section(report) is None

    def test_真无风险段落仍须报(self):
        body = ('# 公司估值分析\n\n## 业务\n主营稳定增长，收入结构多元。\n\n'
                ) + '公司估值处于合理区间，盈利能力持续改善，股价表现平稳，投资回报稳定。' * 30
        r = rf.check_missing_risk_section(body)
        assert r is not None and r['rule'] == 'missing_risk_section'


# ---------- 证据等级补充（转载 / 招股书） ----------

class Test证据等级补充:

    def test_转载自媒体归D(self):
        """用户口径：D = 百科/**转载**。不能因含「媒体」被抢判 C。"""
        for s in ('媒体报道（转载）', '自媒体传闻', '网传消息'):
            assert eg.grade_of(s)['grade'] == 'D', s

    def test_招股书归A(self):
        """用户口径：A = 招股书原文/财报接口。"""
        for s in ('招股书原文', '港股招股书', 'financial_statements', 'segment_breakdown'):
            assert eg.grade_of(s)['grade'] == 'A', s


# ---------- 未上市公司三件套 ----------

class Test烧钱速度:

    def test_三档判定(self):
        assert uc.burn_runway(30.0, 15.0)['level'] == 'tight'       # 2 年
        assert uc.burn_runway(30.0, 10.0)['level'] == 'manageable'  # 3 年
        assert uc.burn_runway(50.0, 8.0)['level'] == 'comfortable'  # 6.25 年
        r = uc.burn_runway(10.0, 8.0)
        assert r['level'] == 'critical' and r['years'] == 1.25

    def test_已盈利无紧迫性(self):
        r = uc.burn_runway(40.0, 0.0)
        assert r['ok'] is True and r['years'] is None
        assert '无烧钱紧迫性' in r['verdict_cn']

    def test_缺输入不猜(self):
        r = uc.burn_runway(None, 10.0)
        assert r['ok'] is False and '累计融资' in r['reason']


class Test同业PS对比:

    def test_区间与落点(self):
        r = uc.peer_ps_table([{'name': 'A', 'ps': 8}, {'name': 'B', 'ps': 15},
                              {'name': 'C', 'ps': 60}], target_ps=38.0)
        assert (r['low'], r['high'], r['median']) == (8.0, 60.0, 15.0)
        assert r['position'] == 'within'

    def test_高于区间须给理由(self):
        r = uc.peer_ps_table([{'name': 'A', 'ps': 8}, {'name': 'B', 'ps': 15}],
                             target_ps=40.0)
        assert r['position'] == 'above' and '溢价需要理由' in r['position_cn']

    def test_低于区间二选一解释(self):
        r = uc.peer_ps_table([{'name': 'A', 'ps': 20}, {'name': 'B', 'ps': 30}],
                             target_ps=9.0)
        assert r['position'] == 'below' and '二选一' in r['position_cn']

    def test_样本不足不猜(self):
        r = uc.peer_ps_table([{'name': 'A', 'ps': 10}])
        assert r['ok'] is False and '不足 2 家' in r['reason']

    def test_表格含锚点行(self):
        md = uc.peer_ps_table([{'name': 'A', 'ps': 10}, {'name': 'B', 'ps': 20}],
                              target_ps=15.0)['table_md']
        assert '锚点' in md and '**15**' in md


class Test六段式模板:

    def test_六段齐全且顺序正确(self):
        md = uc.unlisted_template('智谱AI')
        names = [s[0] for s in uc.SECTIONS]
        assert names == ['行业', '竞争', '商业模式', '政策技术', '风险催化', '估值锚点']
        for n in names:
            assert f'## {names.index(n) + 1}. {n}' in md

    def test_模板自带证据等级口径(self):
        assert 'C/D 级数字不得支撑核心结论' in uc.unlisted_template('X')

    def test_已填段落不显示提示(self):
        md = uc.unlisted_template('智谱AI', filled={'行业': '大模型赛道，规模 3000 亿。'})
        assert '大模型赛道' in md
        assert '**应回答**：赛道规模' not in md


# ---------- 复核队列新类别 ----------

class Test复核队列新类别:

    def test_风控误报与数字解析错误(self, tmp_path):
        q = rq.ReviewQueue(path=str(tmp_path / 'q.jsonl'))
        a = q.submit(reason='报告明明有风险与催化，却报缺少风险段落', category='risk_false_positive')
        b = q.submit(reason='把 S800 的 800 当成财务数字核验了', category='number_parse_error')
        assert a['category_cn'] == '风控误报' and a['severity'] == 'mid'
        assert b['category_cn'] == '数字解析错误' and b['severity'] == 'mid'

    def test_类别可流转到修复(self, tmp_path):
        q = rq.ReviewQueue(path=str(tmp_path / 'q.jsonl'))
        rid = q.submit(reason='误报', category='risk_false_positive')['id']
        q.transition(rid, 'triaged', actor='alice')
        c = q.transition(rid, 'confirmed', actor='alice', note='确认规则漏了风险与催化')
        assert c['status'] == 'confirmed'
